"""Action models for the action-capable multi-agent system.

This module defines data structures for:
- Action intents and classifications
- Action plans with steps
- Action execution results
- Confirmation flows
"""

from datetime import datetime
from typing import Optional, Any, Literal
from pydantic import BaseModel, Field
from enum import Enum


class ActionType(str, Enum):
    """Types of actions that can be performed."""
    # Calendar actions
    CREATE_EVENT = "create_event"
    UPDATE_EVENT = "update_event"
    DELETE_EVENT = "delete_event"
    CHECK_AVAILABILITY = "check_availability"
    
    # Gmail actions
    SEND_EMAIL = "send_email"
    CREATE_DRAFT = "create_draft"
    REPLY_EMAIL = "reply_email"
    FORWARD_EMAIL = "forward_email"
    
    # Drive actions
    CREATE_DOCUMENT = "create_document"
    CREATE_SPREADSHEET = "create_spreadsheet"
    SHARE_FILE = "share_file"
    CREATE_FOLDER = "create_folder"


class RiskLevel(str, Enum):
    """Risk level for actions determining confirmation requirements."""
    LOW = "low"          # No confirmation needed (drafts, reads)
    MEDIUM = "medium"    # Confirmation recommended
    HIGH = "high"        # Confirmation required (sends, deletes)


class QueryType(str, Enum):
    """Classification of query type."""
    READ = "read"           # Data retrieval only
    ACTION = "action"       # Single action to perform
    WORKFLOW = "workflow"   # Multi-step workflow


class ActionIntent(BaseModel):
    """Parsed action intent from user query.
    
    Args:
        query_type: Whether this is a read, action, or workflow
        action_type: Specific action to perform (if action/workflow)
        requires_confirmation: Whether user confirmation is needed
        parameters: Extracted parameters from the query
        confidence: How confident the classification is
        
    Examples:
        >>> intent = ActionIntent(
        ...     query_type=QueryType.ACTION,
        ...     action_type=ActionType.CREATE_EVENT,
        ...     parameters={"title": "Team Meeting", "time": "tomorrow 2pm"},
        ...     requires_confirmation=True
        ... )
    """
    
    query_type: QueryType = Field(..., description="Type of query")
    action_type: Optional[ActionType] = Field(None, description="Specific action")
    requires_confirmation: bool = Field(True, description="Needs user approval")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Extracted params")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="Classification confidence")
    missing_parameters: list[str] = Field(default_factory=list, description="Required but missing params")
    clarification_needed: Optional[str] = Field(None, description="Question to ask user")


class ActionStep(BaseModel):
    """A single step in an action plan.
    
    Args:
        step_number: Order of execution
        action_type: What action to perform
        agent: Which agent handles this step
        description: Human-readable description
        parameters: Parameters for the action
        depends_on: Steps this depends on
    """
    
    step_number: int = Field(..., ge=1, description="Execution order")
    action_type: ActionType = Field(..., description="Action to perform")
    agent: str = Field(..., description="Agent handling this step")
    description: str = Field(..., description="Human-readable description")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Action parameters")
    depends_on: list[int] = Field(default_factory=list, description="Dependent step numbers")


class ActionPlan(BaseModel):
    """Complete action plan with preview for user confirmation.
    
    Args:
        plan_id: Unique identifier for this plan
        query: Original user query
        summary: Brief summary of what will happen
        steps: List of steps to execute
        risk_level: Overall risk level
        preview: Human-readable preview for confirmation
        estimated_duration: Estimated time to complete
    """
    
    plan_id: str = Field(..., description="Unique plan ID")
    query: str = Field(..., description="Original query")
    summary: str = Field(..., description="Brief summary")
    steps: list[ActionStep] = Field(..., description="Steps to execute")
    risk_level: RiskLevel = Field(RiskLevel.MEDIUM, description="Risk level")
    preview: str = Field(..., description="Human-readable preview")
    estimated_duration: str = Field("< 1 minute", description="Time estimate")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ActionStepResult(BaseModel):
    """Result of executing a single action step.
    
    Args:
        step_number: Which step this is
        success: Whether step succeeded
        result_data: Data returned from the action
        error_message: Error if failed
        can_rollback: Whether this can be undone
        rollback_info: Info needed to undo
    """
    
    step_number: int = Field(..., description="Step number")
    success: bool = Field(..., description="Step succeeded")
    result_data: Optional[dict[str, Any]] = Field(None, description="Result data")
    error_message: Optional[str] = Field(None, description="Error if failed")
    can_rollback: bool = Field(False, description="Can be undone")
    rollback_info: Optional[dict[str, Any]] = Field(None, description="Rollback data")
    execution_time_ms: float = Field(0.0, description="Execution time")


class ActionResult(BaseModel):
    """Complete result of action execution.
    
    Args:
        plan_id: Plan that was executed
        success: Overall success
        step_results: Results of each step
        summary: Summary of what happened
        links: Relevant links (calendar event, email, etc.)
        rollback_available: Whether actions can be undone
    """
    
    plan_id: str = Field(..., description="Executed plan ID")
    success: bool = Field(..., description="Overall success")
    step_results: list[ActionStepResult] = Field(..., description="Step results")
    summary: str = Field(..., description="What happened")
    links: dict[str, str] = Field(default_factory=dict, description="Result links")
    rollback_available: bool = Field(False, description="Can undo")
    executed_at: datetime = Field(default_factory=datetime.utcnow)


# Calendar-specific action parameters
class CreateEventParams(BaseModel):
    """Parameters for creating a calendar event."""
    
    title: str = Field(..., description="Event title")
    start_time: datetime = Field(..., description="Start time")
    end_time: Optional[datetime] = Field(None, description="End time")
    duration_minutes: int = Field(60, description="Duration if no end_time")
    attendees: list[str] = Field(default_factory=list, description="Attendee emails")
    location: Optional[str] = Field(None, description="Location")
    description: Optional[str] = Field(None, description="Event description")
    add_meet_link: bool = Field(False, description="Add Google Meet link")
    send_invites: bool = Field(True, description="Send invite emails")


class UpdateEventParams(BaseModel):
    """Parameters for updating a calendar event."""
    
    event_id: str = Field(..., description="Event to update")
    title: Optional[str] = Field(None, description="New title")
    start_time: Optional[datetime] = Field(None, description="New start time")
    end_time: Optional[datetime] = Field(None, description="New end time")
    attendees_to_add: list[str] = Field(default_factory=list)
    attendees_to_remove: list[str] = Field(default_factory=list)
    location: Optional[str] = Field(None, description="New location")
    description: Optional[str] = Field(None, description="New description")


# Gmail-specific action parameters
class SendEmailParams(BaseModel):
    """Parameters for sending an email."""
    
    to: list[str] = Field(..., description="Recipient emails")
    subject: str = Field(..., description="Email subject")
    body: str = Field(..., description="Email body (plain text or HTML)")
    cc: list[str] = Field(default_factory=list, description="CC recipients")
    bcc: list[str] = Field(default_factory=list, description="BCC recipients")
    is_html: bool = Field(False, description="Body is HTML")
    reply_to_message_id: Optional[str] = Field(None, description="For replies")
    thread_id: Optional[str] = Field(None, description="Thread to reply in")


class CreateDraftParams(BaseModel):
    """Parameters for creating an email draft."""
    
    to: list[str] = Field(default_factory=list, description="Recipients")
    subject: str = Field("", description="Email subject")
    body: str = Field("", description="Email body")
    cc: list[str] = Field(default_factory=list)
    is_html: bool = Field(False)


# Drive-specific action parameters  
class CreateDocumentParams(BaseModel):
    """Parameters for creating a Google Doc."""
    
    title: str = Field(..., description="Document title")
    content: Optional[str] = Field(None, description="Initial content")
    folder_id: Optional[str] = Field(None, description="Parent folder")


class ShareFileParams(BaseModel):
    """Parameters for sharing a Drive file."""
    
    file_id: str = Field(..., description="File to share")
    email: str = Field(..., description="Email to share with")
    role: Literal["reader", "commenter", "writer"] = Field("reader")
    send_notification: bool = Field(True, description="Send email notification")
    message: Optional[str] = Field(None, description="Custom message")

