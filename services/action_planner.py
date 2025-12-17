"""Action Planner service for planning and executing user actions.

This module provides:
- Action intent classification
- Action plan generation
- Parameter extraction and validation
- Confirmation flow management
- Action execution coordination
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, Any

from models.action import (
    ActionIntent,
    ActionPlan,
    ActionStep,
    ActionType,
    QueryType,
    RiskLevel,
    CreateEventParams,
    SendEmailParams,
    CreateDraftParams,
    CreateDocumentParams,
    ShareFileParams,
)
from services.llm.openai_service import OpenAIService
from services.llm.prompts import get_action_planning_prompt

logger = logging.getLogger(__name__)


class ActionPlanner:
    """Plans and validates actions before execution.
    
    Responsibilities:
    - Parse action intent from classified query
    - Extract and validate parameters
    - Generate action plans with previews
    - Manage confirmation flow
    - Coordinate multi-step workflows
    
    Args:
        llm_service: LLM service for parameter extraction
        
    Examples:
        >>> planner = ActionPlanner(llm_service)
        >>> intent = await planner.classify_action_intent(query)
        >>> if intent.query_type == QueryType.ACTION:
        ...     plan = await planner.create_action_plan(query, intent)
    """
    
    def __init__(self, llm_service: Optional[OpenAIService] = None):
        self._llm_service = llm_service or OpenAIService()
        self._pending_plans: dict[str, ActionPlan] = {}
        
        # Risk levels for action types
        self._action_risk_levels = {
            ActionType.CREATE_EVENT: RiskLevel.MEDIUM,
            ActionType.UPDATE_EVENT: RiskLevel.MEDIUM,
            ActionType.DELETE_EVENT: RiskLevel.HIGH,
            ActionType.CHECK_AVAILABILITY: RiskLevel.LOW,
            ActionType.SEND_EMAIL: RiskLevel.HIGH,
            ActionType.CREATE_DRAFT: RiskLevel.LOW,
            ActionType.REPLY_EMAIL: RiskLevel.HIGH,
            ActionType.FORWARD_EMAIL: RiskLevel.HIGH,
            ActionType.CREATE_DOCUMENT: RiskLevel.LOW,
            ActionType.CREATE_SPREADSHEET: RiskLevel.LOW,
            ActionType.SHARE_FILE: RiskLevel.MEDIUM,
            ActionType.CREATE_FOLDER: RiskLevel.LOW,
        }
        
        # Which agent handles each action
        self._action_to_agent = {
            ActionType.CREATE_EVENT: "calendar",
            ActionType.UPDATE_EVENT: "calendar",
            ActionType.DELETE_EVENT: "calendar",
            ActionType.CHECK_AVAILABILITY: "calendar",
            ActionType.SEND_EMAIL: "gmail",
            ActionType.CREATE_DRAFT: "gmail",
            ActionType.REPLY_EMAIL: "gmail",
            ActionType.FORWARD_EMAIL: "gmail",
            ActionType.CREATE_DOCUMENT: "drive",
            ActionType.CREATE_SPREADSHEET: "drive",
            ActionType.SHARE_FILE: "drive",
            ActionType.CREATE_FOLDER: "drive",
        }
        
    async def classify_action_intent(
        self,
        intent_data: dict[str, Any]
    ) -> ActionIntent:
        """Convert raw intent classification to ActionIntent.
        
        Args:
            intent_data: Raw intent from LLM classification
            
        Returns:
            ActionIntent: Structured action intent
        """
        query_type_str = intent_data.get("query_type", "read")
        action_type_str = intent_data.get("action_type")
        
        # Map query type
        query_type = QueryType.READ
        if query_type_str == "action":
            query_type = QueryType.ACTION
        elif query_type_str == "workflow":
            query_type = QueryType.WORKFLOW
            
        # Map action type
        action_type = None
        if action_type_str:
            try:
                action_type = ActionType(action_type_str)
            except ValueError:
                logger.warning(f"Unknown action type: {action_type_str}")
                
        # Determine if confirmation is needed
        requires_confirmation = intent_data.get("requires_confirmation", True)
        if action_type and self._action_risk_levels.get(action_type) == RiskLevel.LOW:
            requires_confirmation = False
            
        # Convert entities list to initial parameters dict
        entities = intent_data.get("entities", [])
        initial_params = {}
        if isinstance(entities, list):
            initial_params["entities"] = entities
        elif isinstance(entities, dict):
            initial_params = entities
            
        return ActionIntent(
            query_type=query_type,
            action_type=action_type,
            requires_confirmation=requires_confirmation,
            parameters=initial_params,
            confidence=0.9 if action_type else 0.5,
        )
        
    async def create_action_plan(
        self,
        query: str,
        intent: ActionIntent,
        conversation_history: Optional[list[dict[str, str]]] = None,
        context: Optional[dict[str, Any]] = None
    ) -> ActionPlan:
        """Create a detailed action plan from query and intent.
        
        Args:
            query: Original user query
            intent: Classified action intent
            conversation_history: Optional conversation history for context
            context: Optional context (previous messages, user preferences)
            
        Returns:
            ActionPlan: Complete action plan with preview
            
        Raises:
            ValueError: If action type is not set
        """
        if not intent.action_type:
            raise ValueError("Cannot create plan for query without action type")
            
        plan_id = str(uuid.uuid4())[:8]
        
        # Use LLM to extract detailed parameters with conversation context
        plan_data = await self._extract_action_parameters(query, intent, conversation_history)
        
        # Build action steps
        steps = self._build_action_steps(intent, plan_data)
        
        # Determine risk level
        risk_level = self._action_risk_levels.get(
            intent.action_type, RiskLevel.MEDIUM
        )
        
        # Generate preview
        preview = self._generate_preview(intent.action_type, plan_data)
        
        plan = ActionPlan(
            plan_id=plan_id,
            query=query,
            summary=plan_data.get("summary", f"Execute {intent.action_type.value}"),
            steps=steps,
            risk_level=risk_level,
            preview=preview,
            estimated_duration="< 1 minute"
        )
        
        # Store pending plan
        self._pending_plans[plan_id] = plan
        
        logger.info(f"Created action plan {plan_id}: {plan.summary}")
        return plan
        
    async def _extract_action_parameters(
        self,
        query: str,
        intent: ActionIntent,
        conversation_history: Optional[list[dict[str, str]]] = None
    ) -> dict[str, Any]:
        """Use LLM to extract detailed parameters from query.
        
        Args:
            query: User query
            intent: Action intent
            conversation_history: Optional conversation history for context
            
        Returns:
            dict: Extracted parameters
        """
        prompt = get_action_planning_prompt(
            query=query,
            intent={
                "query_type": intent.query_type.value,
                "action_type": intent.action_type.value if intent.action_type else None,
                "requires_confirmation": intent.requires_confirmation,
            },
            conversation_history=conversation_history
        )
        
        try:
            response = await self._llm_service.generate(
                prompt=prompt,
                temperature=0.2,
                max_tokens=1000,
                timeout=15.0
            )
            
            return self._llm_service._parse_json_response(response)
            
        except Exception as e:
            logger.warning(f"Parameter extraction failed: {e}")
            # Return minimal parameters
            return {
                "summary": f"Execute {intent.action_type.value if intent.action_type else 'action'}",
                "parameters": {},
                "missing_parameters": [],
                "requires_confirmation": True,
            }
            
    def _build_action_steps(
        self,
        intent: ActionIntent,
        plan_data: dict[str, Any]
    ) -> list[ActionStep]:
        """Build action steps from intent and extracted parameters.
        
        Args:
            intent: Action intent
            plan_data: Extracted plan data
            
        Returns:
            list[ActionStep]: Steps to execute
        """
        if not intent.action_type:
            return []
            
        agent = self._action_to_agent.get(intent.action_type, "unknown")
        parameters = plan_data.get("parameters", {})
        
        # For calendar events, always add Google Meet by default
        if intent.action_type == ActionType.CREATE_EVENT:
            parameters = self._enhance_meeting_params(parameters)
        
        return [
            ActionStep(
                step_number=1,
                action_type=intent.action_type,
                agent=agent,
                description=plan_data.get("summary", f"Execute {intent.action_type.value}"),
                parameters=parameters,
                depends_on=[]
            )
        ]
    
    def _enhance_meeting_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Enhance meeting parameters with defaults like Google Meet link.
        
        Only adds technical settings, does NOT invent user-facing content.
        
        Args:
            params: Original parameters
            
        Returns:
            Enhanced parameters with Google Meet enabled
        """
        enhanced = dict(params)
        
        # Always add Google Meet link for meetings
        enhanced["add_meet_link"] = True
        
        # Set default duration if not specified (but don't override null with a value
        # that would prevent user from editing in UI)
        if enhanced.get("duration_minutes") is None:
            enhanced["duration_minutes"] = 60
        
        # DO NOT auto-generate description - leave it as user specified (or null)
        # The Google Meet link will be added to the event by the calendar agent
        # after creation, and shown in the success message
        
        return enhanced
        
    def _generate_preview(
        self,
        action_type: ActionType,
        plan_data: dict[str, Any]
    ) -> str:
        """Generate a human-readable preview of the action.
        
        Args:
            action_type: Type of action
            plan_data: Extracted plan data
            
        Returns:
            str: Formatted preview
        """
        # Use preview from LLM if available
        if plan_data.get("preview"):
            return plan_data["preview"]
            
        params = plan_data.get("parameters", {})
        
        # Generate based on action type
        if action_type == ActionType.CREATE_EVENT:
            return self._preview_create_event(params)
        elif action_type == ActionType.SEND_EMAIL:
            return self._preview_send_email(params)
        elif action_type == ActionType.CREATE_DRAFT:
            return self._preview_create_draft(params)
        elif action_type == ActionType.SHARE_FILE:
            return self._preview_share_file(params)
        elif action_type == ActionType.CREATE_DOCUMENT:
            return self._preview_create_document(params)
        elif action_type == ActionType.DELETE_EVENT:
            return self._preview_delete_event(params)
        else:
            return f"Will execute: {action_type.value}"
            
    def _preview_create_event(self, params: dict) -> str:
        """Generate preview for event creation."""
        lines = ["📅 **Create Calendar Event**\n"]
        
        if params.get("title"):
            lines.append(f"**Title:** {params['title']}")
        if params.get("start_time"):
            lines.append(f"🕐 **When:** {params['start_time']}")
        if params.get("duration_minutes"):
            lines.append(f"⏱️ **Duration:** {params['duration_minutes']} minutes")
        if params.get("attendees"):
            attendees = params["attendees"]
            if isinstance(attendees, list):
                lines.append(f"👥 **Attendees:** {', '.join(attendees)}")
            else:
                lines.append(f"👥 **Attendees:** {attendees}")
        if params.get("location"):
            lines.append(f"📍 **Location:** {params['location']}")
            
        return "\n".join(lines)
        
    def _preview_send_email(self, params: dict) -> str:
        """Generate preview for email sending."""
        lines = ["📧 **Send Email**\n"]
        
        if params.get("to"):
            to = params["to"]
            if isinstance(to, list):
                lines.append(f"**To:** {', '.join(to)}")
            else:
                lines.append(f"**To:** {to}")
        if params.get("subject"):
            lines.append(f"**Subject:** {params['subject']}")
        if params.get("body"):
            body_preview = params["body"][:200]
            if len(params["body"]) > 200:
                body_preview += "..."
            lines.append(f"\n---\n{body_preview}\n---")
            
        return "\n".join(lines)
        
    def _preview_create_draft(self, params: dict) -> str:
        """Generate preview for draft creation."""
        lines = ["📝 **Create Email Draft**\n"]
        
        if params.get("to"):
            to = params["to"]
            if isinstance(to, list):
                lines.append(f"**To:** {', '.join(to)}")
            else:
                lines.append(f"**To:** {to}")
        if params.get("subject"):
            lines.append(f"**Subject:** {params['subject']}")
        lines.append("\n*Draft will be saved for review before sending*")
        
        return "\n".join(lines)
        
    def _preview_share_file(self, params: dict) -> str:
        """Generate preview for file sharing."""
        lines = ["📁 **Share File**\n"]
        
        if params.get("file_name") or params.get("file_id"):
            lines.append(f"**File:** {params.get('file_name', params.get('file_id', 'Unknown'))}")
        if params.get("email"):
            lines.append(f"**Share with:** {params['email']}")
        if params.get("role"):
            lines.append(f"**Permission:** {params['role']}")
            
        return "\n".join(lines)
        
    def _preview_create_document(self, params: dict) -> str:
        """Generate preview for document creation."""
        lines = ["📄 **Create Document**\n"]
        
        if params.get("title"):
            lines.append(f"**Title:** {params['title']}")
        if params.get("content"):
            content_preview = params["content"][:100]
            if len(params["content"]) > 100:
                content_preview += "..."
            lines.append(f"**Content:** {content_preview}")
            
        return "\n".join(lines)
        
    def _preview_delete_event(self, params: dict) -> str:
        """Generate preview for event deletion."""
        lines = ["🗑️ **Delete Calendar Event**\n"]
        
        if params.get("title") or params.get("event_id"):
            lines.append(f"**Event:** {params.get('title', params.get('event_id', 'Unknown'))}")
        lines.append("\n⚠️ *This action cannot be undone*")
        
        return "\n".join(lines)
        
    def get_pending_plan(self, plan_id: str) -> Optional[ActionPlan]:
        """Get a pending action plan by ID.
        
        Args:
            plan_id: Plan ID
            
        Returns:
            ActionPlan if found, None otherwise
        """
        return self._pending_plans.get(plan_id)
        
    def confirm_plan(self, plan_id: str) -> Optional[ActionPlan]:
        """Mark a plan as confirmed and ready for execution.
        
        Args:
            plan_id: Plan ID to confirm
            
        Returns:
            ActionPlan if found, None otherwise
        """
        plan = self._pending_plans.get(plan_id)
        if plan:
            logger.info(f"Plan {plan_id} confirmed for execution")
        return plan
        
    def cancel_plan(self, plan_id: str) -> bool:
        """Cancel a pending plan.
        
        Args:
            plan_id: Plan ID to cancel
            
        Returns:
            True if cancelled, False if not found
        """
        if plan_id in self._pending_plans:
            del self._pending_plans[plan_id]
            logger.info(f"Plan {plan_id} cancelled")
            return True
        return False
        
    def clear_expired_plans(self, max_age_minutes: int = 30) -> int:
        """Clear plans older than max_age_minutes.
        
        Args:
            max_age_minutes: Maximum age in minutes
            
        Returns:
            Number of plans cleared
        """
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        expired = [
            plan_id for plan_id, plan in self._pending_plans.items()
            if plan.created_at < cutoff
        ]
        
        for plan_id in expired:
            del self._pending_plans[plan_id]
            
        if expired:
            logger.info(f"Cleared {len(expired)} expired action plans")
            
        return len(expired)
        
    def get_metrics(self) -> dict[str, Any]:
        """Get planner metrics.
        
        Returns:
            dict: Planner metrics
        """
        return {
            "pending_plans": len(self._pending_plans),
            "supported_actions": len(self._action_to_agent),
        }

