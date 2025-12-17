"""FastAPI application for the Multi-Agent Data Connector system.

This module provides the REST API endpoints for:
- Query processing via the orchestrator
- Action planning, confirmation, and execution
- Google OAuth2 authentication flow
- Health checks and metrics
"""

import logging
import time
from contextlib import asynccontextmanager
from typing import Optional, Any, Union

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from config import settings
from models.query import (
    QueryRequest, 
    QueryResponse, 
    AgentContribution,
    SlackSearchRequest,
    SlackSearchResponse,
    SlackChannelSummary,
    SlackFollowUpSuggestion,
    SlackThreadSummarizeRequest,
    SlackThreadSummarizeResponse,
    SlackThreadMetadata,
    SlackThreadParticipant,
)
from models.action import ActionResult, ActionPlan
from models.session import (
    Session,
    SessionCreate,
    SessionWithMessages,
    SessionListResponse,
    QueryRequestWithSession,
)
from services.orchestrator import Orchestrator, get_orchestrator
from services.auth.google_auth import GoogleAuthService, GoogleAuthError, get_auth_service
from services.conversation_manager import ConversationManager
from storage.session_store import SessionStore

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Global service instances
orchestrator: Optional[Orchestrator] = None
auth_service: Optional[GoogleAuthService] = None
session_store: Optional[SessionStore] = None
conversation_manager: Optional[ConversationManager] = None
slack_data_source = None  # Will be initialized on first use
slack_thread_service = None  # Will be initialized on first use


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown."""
    global orchestrator, auth_service, session_store, conversation_manager
    
    logger.info("Starting Multi-Agent Data Connector...")
    
    # Initialize session store (MySQL)
    session_store = SessionStore()
    await session_store.initialize()
    
    # Initialize orchestrator
    orchestrator = Orchestrator()
    await orchestrator.initialize()
    
    # Initialize conversation manager with LLM service from orchestrator
    conversation_manager = ConversationManager(
        session_store=session_store,
        llm_service=orchestrator._llm_service
    )
    
    # Initialize auth service
    auth_service = GoogleAuthService()
    await auth_service.initialize()
    
    logger.info("Application started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    if orchestrator:
        await orchestrator.shutdown()
    if auth_service:
        await auth_service.shutdown()
    if session_store:
        await session_store.shutdown()
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Multi-Agent Data Connector",
    description="""
    An agent-based LLM system where each external data source (Google Calendar, 
    Gmail, Google Drive) is represented by a dedicated AI agent. Each agent 
    evaluates query relevance and retrieves data only when appropriate.
    """,
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------
# Request/Response Models
# -----------------

class AuthUrlResponse(BaseModel):
    """Response containing OAuth authorization URL."""
    authorization_url: str
    state: str


class AuthStatusResponse(BaseModel):
    """Response containing authentication status."""
    connected: bool
    scopes: list[str] = Field(default_factory=list)
    expires_at: Optional[str] = None
    error: Optional[str] = None


class HealthResponse(BaseModel):
    """Response containing health check status."""
    status: str
    orchestrator: dict
    auth_service: bool
    agents: dict


class MetricsResponse(BaseModel):
    """Response containing system metrics."""
    orchestrator: dict
    auth: dict


# -----------------
# Action Request/Response Models
# -----------------

class ActionConfirmRequest(BaseModel):
    """Request to confirm and execute an action plan."""
    plan_id: str = Field(..., description="ID of the action plan to execute")
    user_id: str = Field(..., description="User identifier")


class ActionCancelRequest(BaseModel):
    """Request to cancel a pending action plan."""
    plan_id: str = Field(..., description="ID of the action plan to cancel")


class ActionPlanResponse(BaseModel):
    """Response containing an action plan for confirmation."""
    type: str = Field(default="action_plan", description="Response type")
    plan_id: str = Field(..., description="Unique plan ID")
    query: str = Field(..., description="Original query")
    summary: str = Field(..., description="Action summary")
    preview: str = Field(..., description="Human-readable preview")
    steps: list[dict] = Field(..., description="Steps to execute")
    risk_level: str = Field(..., description="Risk level (low, medium, high)")
    requires_confirmation: bool = Field(..., description="Whether confirmation is needed")
    estimated_duration: str = Field(..., description="Time estimate")
    execution_time_ms: float = Field(..., description="Planning time")


class ActionExecutionResponse(BaseModel):
    """Response containing action execution results."""
    plan_id: str = Field(..., description="Executed plan ID")
    success: bool = Field(..., description="Overall success")
    summary: str = Field(..., description="Result summary")
    step_results: list[dict] = Field(..., description="Results of each step")
    links: dict[str, str] = Field(default_factory=dict, description="Result links")
    rollback_available: bool = Field(False, description="Can undo")


# -----------------
# Query Endpoints
# -----------------

@app.post("/api/query", tags=["Query"])
async def process_query(request: QueryRequest) -> Union[QueryResponse, dict]:
    """Process a natural language query through the agent system.
    
    This endpoint handles both read queries and action requests:
    
    For READ queries:
    1. Broadcasts the query to relevant data connector agents
    2. Collects relevance scores from each agent
    3. Triggers data retrieval for agents above the threshold
    4. Synthesizes a coherent response from the collected data
    
    For ACTION queries (e.g., "Schedule a meeting", "Send an email"):
    1. Classifies the action type
    2. Extracts parameters from the query
    3. Creates an action plan
    4. Returns the plan for user confirmation
    
    Args:
        request: Query request with user_id and query text
        
    Returns:
        QueryResponse: For read queries - synthesized response with agent contributions
        ActionPlanResponse: For action queries - action plan awaiting confirmation
        
    Raises:
        HTTPException: 401 if user not authenticated
        HTTPException: 500 if query processing fails
    """
    global orchestrator, auth_service
    
    if not orchestrator or not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Service not initialized"
        )
        
    # Get user credentials
    try:
        credentials = await auth_service.get_credentials(request.user_id)
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=401,
            detail=f"Authentication error: {str(e)}"
        )
        
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="User not authenticated. Please connect your Google account."
        )
        
    # Process query
    try:
        response = await orchestrator.process_query(request, credentials)
        return response
    except Exception as e:
        logger.error(f"Query processing failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Query processing failed: {str(e)}"
        )


@app.post("/api/query/session", tags=["Query"])
async def process_query_with_session(request: QueryRequestWithSession) -> Union[QueryResponse, dict]:
    """Process a query within a session context with conversation memory.
    
    This endpoint extends the standard query with session support:
    - Maintains conversation history within the session
    - Automatically summarizes older messages when approaching token limit
    - Auto-generates session title after first message
    
    Args:
        request: Query request with user_id, query, and optional session_id
        
    Returns:
        QueryResponse with session_id included
        
    Raises:
        HTTPException: 401 if user not authenticated
        HTTPException: 500 if query processing fails
    """
    global orchestrator, auth_service, session_store, conversation_manager
    
    if not orchestrator or not auth_service or not session_store or not conversation_manager:
        raise HTTPException(
            status_code=503,
            detail="Service not initialized"
        )
        
    # Get user credentials
    try:
        credentials = await auth_service.get_credentials(request.user_id)
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=401,
            detail=f"Authentication error: {str(e)}"
        )
        
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="User not authenticated. Please connect your Google account."
        )
        
    try:
        # Create or get session
        session_id = request.session_id
        is_new_session = False
        
        if not session_id:
            # Create new session
            session = await session_store.create_session(request.user_id)
            session_id = session.session_id
            is_new_session = True
        else:
            # Verify session exists
            session = await session_store.get_session(session_id)
            if not session:
                raise HTTPException(
                    status_code=404,
                    detail=f"Session '{session_id}' not found"
                )
                
        # Add user message to session
        await conversation_manager.add_message_to_session(
            session_id=session_id,
            role="user",
            content=request.query
        )
        
        # Get conversation history for context
        conversation_history = await conversation_manager.get_conversation_context(session_id)
        
        # Process query with context
        # Create a standard QueryRequest for the orchestrator
        query_request = QueryRequest(
            query=request.query,
            user_id=request.user_id,
            threshold_override=request.threshold_override,
            skip_cache=request.skip_cache
        )
        
        response = await orchestrator.process_query(
            query_request, 
            credentials,
            conversation_history=conversation_history
        )
        
        # Store assistant response in session (non-blocking - don't let failures affect response)
        try:
            if hasattr(response, 'response'):
                await conversation_manager.add_message_to_session(
                    session_id=session_id,
                    role="assistant",
                    content=response.response,
                    metadata={
                        "agents_triggered": [
                            {"agent_name": a.agent_name, "relevance_score": a.relevance_score}
                            for a in response.agents_triggered
                        ] if hasattr(response, 'agents_triggered') else None
                    }
                )
                
                # Generate title for new session
                if is_new_session:
                    await conversation_manager.generate_session_title(session_id, request.query)
            elif isinstance(response, dict) and response.get("type") == "action_plan":
                # Store action plan response
                await conversation_manager.add_message_to_session(
                    session_id=session_id,
                    role="assistant",
                    content=response.get("preview", "Action plan created"),
                    metadata={"action_plan_id": response.get("plan_id")}
                )
                if is_new_session:
                    await conversation_manager.generate_session_title(session_id, request.query)
        except Exception as session_error:
            logger.warning(f"Failed to store response in session: {session_error}")
        
        # Add session_id to response
        if isinstance(response, dict):
            response["session_id"] = session_id
            return response
        else:
            # Use mode='json' to ensure proper datetime serialization
            response_dict = response.model_dump(mode='json')
            response_dict["session_id"] = session_id
            return response_dict
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Session query processing failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Query processing failed: {str(e)}"
        )


# -----------------
# Slack Search Endpoints
# -----------------

def get_slack_data_source():
    """Get or create the Slack data source instance."""
    global slack_data_source
    
    if slack_data_source is None:
        from services.data_sources.slack_source import SlackDataSource
        slack_data_source = SlackDataSource()
        logger.info("Initialized Slack data source successfully")
    
    return slack_data_source


@app.post("/api/slack/search", response_model=SlackSearchResponse, tags=["Slack"])
async def search_slack(request: SlackSearchRequest):
    """Search Slack messages with optional channel specification.
    
    This endpoint supports two modes:
    
    1. **Global Search** (no channel specified):
       - Searches across ALL accessible channels
       - Returns results grouped by channel
       - Includes follow-up suggestions to narrow results
       
    2. **Channel-Specific Search** (channel provided):
       - Searches within the specified channel(s)
       - Returns more detailed results
    
    Args:
        request: SlackSearchRequest with query and optional filters
        
    Returns:
        SlackSearchResponse: Search results with suggestions
        
    Examples:
        Global search:
        ```json
        {
            "query": "deployment issues",
            "user_id": "user_123"
        }
        ```
        
        Channel-specific search:
        ```json
        {
            "query": "deployment issues",
            "user_id": "user_123",
            "channel": "engineering"
        }
        ```
        
    Raises:
        HTTPException: 400 if query is empty
        HTTPException: 500 if search fails
    """
    start_time = time.time()
    
    try:
        # Get the Slack data source
        slack_source = get_slack_data_source()
        
        # Build params for fetch_data
        params = {
            'query': request.query,
            'user_id': request.user_id,
            'limit': request.limit,
            'max_channels': request.max_channels,
            'include_dms': request.include_dms,
            'deep_search': request.deep_search,
        }
        
        # Add channel(s) if specified
        if request.channel:
            params['channel'] = request.channel
        if request.channels:
            params['channels'] = request.channels
            
        # Add time range if specified
        if request.time_range:
            params['time_range'] = request.time_range
        
        # Validate inputs
        is_valid = await slack_source.validate_inputs(params)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail="Invalid search parameters. Please provide a query."
            )
        
        # Perform the search
        results = await slack_source.fetch_data(params)
        
        # Check for errors
        if 'error' in results:
            raise HTTPException(
                status_code=500,
                detail=f"Slack search failed: {results['error']}"
            )
        
        # Extract metadata
        metadata = results.get('_metadata', {})
        is_global = metadata.get('global_search', False)
        
        # Build channel summaries
        channel_summaries = []
        for summary_data in metadata.get('channel_summaries', []):
            channel_summaries.append(SlackChannelSummary(
                channel_id=summary_data.get('channel_id', ''),
                channel_name=summary_data.get('channel_name', ''),
                message_count=summary_data.get('message_count', 0),
                relevance_score=summary_data.get('relevance_score', 0.0),
                top_message_preview=summary_data.get('top_message_preview')
            ))
        
        # Build follow-up suggestions
        follow_up_suggestions = []
        for suggestion_data in metadata.get('follow_up_suggestions', []):
            follow_up_suggestions.append(SlackFollowUpSuggestion(
                type=suggestion_data.get('type', ''),
                value=suggestion_data.get('value', ''),
                label=suggestion_data.get('label', ''),
                description=suggestion_data.get('description')
            ))
        
        # Build results dict (exclude metadata)
        search_results = {
            k: v for k, v in results.items() 
            if k != '_metadata'
        }
        
        processing_time = (time.time() - start_time) * 1000
        
        return SlackSearchResponse(
            query=request.query,
            results=search_results,
            channel_summaries=channel_summaries,
            total_message_count=metadata.get('total_messages', 0),
            channels_searched=metadata.get('channels_processed', 0),
            follow_up_suggestions=follow_up_suggestions,
            summary=metadata.get('summary', ''),
            is_global_search=is_global,
            processing_time_ms=processing_time
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Slack search failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Slack search failed: {str(e)}"
        )


@app.get("/api/slack/channels", tags=["Slack"])
async def list_slack_channels(
    limit: int = Query(default=50, ge=1, le=200, description="Maximum channels to return"),
    include_private: bool = Query(default=True, description="Include private channels")
):
    """List accessible Slack channels.
    
    Returns a list of channels the bot has access to, useful for
    providing autocomplete suggestions in the UI.
    
    Args:
        limit: Maximum number of channels to return
        include_private: Whether to include private channels
        
    Returns:
        List of channel names and IDs
    """
    try:
        slack_source = get_slack_data_source()
        
        # Trigger channel cache refresh if needed
        await slack_source._fetch_all_channels()
        
        # Get channels from cache
        channels = []
        for name, channel_id in list(slack_source.channel_cache.items())[:limit]:
            # Skip DM entries (they have dm_ prefix)
            if name.startswith('dm_'):
                continue
            channels.append({
                'name': name,
                'id': channel_id,
                'is_private': name.startswith('team-')  # Simple heuristic
            })
        
        return {
            'channels': channels,
            'total': len(channels),
            'cache_age_seconds': int(time.time() - slack_source.last_cache_update) if slack_source.last_cache_update else None
        }
        
    except Exception as e:
        logger.error(f"Failed to list Slack channels: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list channels: {str(e)}"
        )


def get_slack_thread_service():
    """Get or create the Slack thread service instance."""
    global slack_thread_service, orchestrator
    
    if slack_thread_service is None:
        from services.slack_service import SlackThreadService
        slack_thread_service = SlackThreadService()
        
        # Set LLM service if orchestrator is available
        if orchestrator and orchestrator._llm_service:
            slack_thread_service.set_llm_service(orchestrator._llm_service)
            
        logger.info("Initialized Slack thread service")
    
    return slack_thread_service


@app.post("/api/slack/thread/summarize", response_model=SlackThreadSummarizeResponse, tags=["Slack"])
async def summarize_slack_thread(request: SlackThreadSummarizeRequest):
    """Summarize a Slack thread from its URL.
    
    Takes a Slack thread URL and returns a summarized version of the thread
    in the standard QueryResponse format for easy integration with the chat UI.
    
    URL Format: https://{workspace}.slack.com/archives/{channel_id}/p{timestamp}
    
    Args:
        request: SlackThreadSummarizeRequest with URL and optional query
        
    Returns:
        SlackThreadSummarizeResponse: Summary in QueryResponse-compatible format
        
    Examples:
        Request:
        ```json
        {
            "url": "https://razorpay.slack.com/archives/C07Q18XM674/p1759741808347769",
            "user_id": "user_123",
            "query": "What was the main decision?"
        }
        ```
        
    Raises:
        HTTPException: 400 if URL is invalid
        HTTPException: 500 if summarization fails
    """
    start_time = time.time()
    
    try:
        # Get the thread service
        thread_service = get_slack_thread_service()
        
        # Summarize the thread
        result = await thread_service.summarize_thread_url(
            url=request.url,
            query=request.query
        )
        
        processing_time = (time.time() - start_time) * 1000
        
        if not result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=result.get("error", "Failed to process thread URL")
            )
        
        # Build metadata
        metadata = None
        if result.get("metadata"):
            meta = result["metadata"]
            participants = []
            for p in meta.get("participants", []):
                if isinstance(p, dict):
                    participants.append(SlackThreadParticipant(
                        id=p.get("id", ""),
                        name=p.get("name", ""),
                        display_name=p.get("display_name")
                    ))
            
            metadata = SlackThreadMetadata(
                channel_id=meta.get("channel_id", ""),
                channel_name=meta.get("channel_name"),
                thread_ts=meta.get("thread_ts", ""),
                workspace=meta.get("workspace"),
                message_count=meta.get("message_count", 0),
                participants=participants,
                source_url=request.url
            )
        
        # Build response in QueryResponse-compatible format
        return SlackThreadSummarizeResponse(
            query=request.url,
            response=result.get("response", "No summary available"),
            agents_triggered=[
                AgentContribution(
                    agent_name="slack",
                    relevance_score=1.0,
                    justification="Thread summarization from URL",
                    data_count=metadata.message_count if metadata else 0,
                    execution_time_ms=processing_time
                )
            ],
            raw_data=result.get("raw_data"),
            metadata=metadata,
            total_execution_time_ms=processing_time
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Thread summarization failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Thread summarization failed: {str(e)}"
        )


# -----------------
# Action Endpoints
# -----------------

@app.post("/api/action/execute", response_model=ActionExecutionResponse, tags=["Actions"])
async def execute_action(request: ActionConfirmRequest):
    """Execute a confirmed action plan.
    
    After receiving an action plan from the /api/query endpoint,
    call this endpoint with the plan_id to execute the action.
    
    Args:
        request: Action confirmation with plan_id and user_id
        
    Returns:
        ActionExecutionResponse: Results of the action execution
        
    Raises:
        HTTPException: 401 if user not authenticated
        HTTPException: 404 if plan not found
        HTTPException: 500 if execution fails
    """
    global orchestrator, auth_service
    
    if not orchestrator or not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Service not initialized"
        )
        
    # Get user credentials
    try:
        credentials = await auth_service.get_credentials(request.user_id)
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=401,
            detail=f"Authentication error: {str(e)}"
        )
        
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="User not authenticated. Please connect your Google account."
        )
        
    # Check if plan exists
    plan = orchestrator.get_pending_action_plan(request.plan_id)
    if not plan:
        raise HTTPException(
            status_code=404,
            detail=f"Action plan '{request.plan_id}' not found or expired"
        )
        
    # Execute the plan
    try:
        result = await orchestrator.execute_action_plan(request.plan_id, credentials)
        
        return ActionExecutionResponse(
            plan_id=result.plan_id,
            success=result.success,
            summary=result.summary,
            step_results=[
                {
                    "step_number": r.step_number,
                    "success": r.success,
                    "result_data": r.result_data,
                    "error_message": r.error_message,
                    "execution_time_ms": r.execution_time_ms
                }
                for r in result.step_results
            ],
            links=result.links,
            rollback_available=result.rollback_available
        )
    except Exception as e:
        logger.error(f"Action execution failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Action execution failed: {str(e)}"
        )


@app.post("/api/action/cancel", tags=["Actions"])
async def cancel_action(request: ActionCancelRequest):
    """Cancel a pending action plan.
    
    Use this to cancel an action plan if the user decides
    not to proceed after seeing the preview.
    
    Args:
        request: Cancel request with plan_id
        
    Returns:
        Success message
        
    Raises:
        HTTPException: 404 if plan not found
    """
    global orchestrator
    
    if not orchestrator:
        raise HTTPException(
            status_code=503,
            detail="Service not initialized"
        )
        
    cancelled = await orchestrator.cancel_action_plan(request.plan_id)
    
    if not cancelled:
        raise HTTPException(
            status_code=404,
            detail=f"Action plan '{request.plan_id}' not found"
        )
        
    return {
        "status": "success",
        "message": f"Action plan '{request.plan_id}' cancelled"
    }


@app.get("/api/action/{plan_id}", response_model=ActionPlanResponse, tags=["Actions"])
async def get_action_plan(plan_id: str):
    """Get details of a pending action plan.
    
    Retrieve the details of an action plan that's awaiting confirmation.
    
    Args:
        plan_id: ID of the action plan
        
    Returns:
        ActionPlanResponse: Plan details
        
    Raises:
        HTTPException: 404 if plan not found
    """
    global orchestrator
    
    if not orchestrator:
        raise HTTPException(
            status_code=503,
            detail="Service not initialized"
        )
        
    plan = orchestrator.get_pending_action_plan(plan_id)
    
    if not plan:
        raise HTTPException(
            status_code=404,
            detail=f"Action plan '{plan_id}' not found or expired"
        )
        
    return ActionPlanResponse(
        type="action_plan",
        plan_id=plan.plan_id,
        query=plan.query,
        summary=plan.summary,
        preview=plan.preview,
        steps=[
            {
                "step_number": step.step_number,
                "action": step.action_type.value,
                "description": step.description,
                "agent": step.agent
            }
            for step in plan.steps
        ],
        risk_level=plan.risk_level.value,
        requires_confirmation=True,
        estimated_duration=plan.estimated_duration,
        execution_time_ms=0.0
    )


# -----------------
# Session Endpoints
# -----------------

@app.get("/api/sessions", response_model=SessionListResponse, tags=["Sessions"])
async def list_sessions(
    user_id: str = Query(..., description="User identifier"),
    limit: int = Query(default=50, ge=1, le=100, description="Maximum sessions to return"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination")
):
    """List all sessions for a user.
    
    Returns sessions ordered by most recently updated.
    
    Args:
        user_id: User identifier
        limit: Maximum sessions to return
        offset: Offset for pagination
        
    Returns:
        SessionListResponse: List of sessions with total count
    """
    global session_store
    
    if not session_store:
        raise HTTPException(status_code=503, detail="Session store not initialized")
        
    sessions, total = await session_store.list_sessions(user_id, limit, offset)
    return SessionListResponse(sessions=sessions, total=total)


@app.post("/api/sessions", response_model=Session, tags=["Sessions"])
async def create_session(request: SessionCreate):
    """Create a new conversation session.
    
    Args:
        request: Session creation request with user_id
        
    Returns:
        Session: The created session
    """
    global session_store
    
    if not session_store:
        raise HTTPException(status_code=503, detail="Session store not initialized")
        
    session = await session_store.create_session(
        user_id=request.user_id,
        title=request.title
    )
    return session


@app.get("/api/sessions/{session_id}", response_model=SessionWithMessages, tags=["Sessions"])
async def get_session(session_id: str):
    """Get a session with all its messages.
    
    Args:
        session_id: Session identifier
        
    Returns:
        SessionWithMessages: Session with messages and summary
        
    Raises:
        HTTPException: 404 if session not found
    """
    global session_store
    
    if not session_store:
        raise HTTPException(status_code=503, detail="Session store not initialized")
        
    session = await session_store.get_session(session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        
    return session


@app.delete("/api/sessions/{session_id}", tags=["Sessions"])
async def delete_session(session_id: str):
    """Delete a session and all its messages.
    
    Args:
        session_id: Session identifier
        
    Returns:
        Success message
        
    Raises:
        HTTPException: 404 if session not found
    """
    global session_store
    
    if not session_store:
        raise HTTPException(status_code=503, detail="Session store not initialized")
        
    deleted = await session_store.delete_session(session_id)
    
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        
    return {"status": "success", "message": f"Session '{session_id}' deleted"}


@app.patch("/api/sessions/{session_id}/title", tags=["Sessions"])
async def update_session_title(
    session_id: str,
    title: str = Query(..., description="New session title")
):
    """Update a session's title.
    
    Args:
        session_id: Session identifier
        title: New title
        
    Returns:
        Success message
    """
    global session_store
    
    if not session_store:
        raise HTTPException(status_code=503, detail="Session store not initialized")
        
    updated = await session_store.update_session_title(session_id, title)
    
    if not updated:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        
    return {"status": "success", "message": "Title updated"}


# -----------------
# Authentication Endpoints
# -----------------

@app.get("/api/auth/google", response_model=AuthUrlResponse, tags=["Authentication"])
async def get_auth_url(
    user_id: str = Query(..., description="User identifier")
):
    """Get Google OAuth authorization URL.
    
    Generates a URL that the user should be redirected to for
    granting access to their Google Calendar, Gmail, and Drive.
    
    Args:
        user_id: Unique identifier for the user
        
    Returns:
        AuthUrlResponse: Authorization URL and state token
    """
    global auth_service
    
    if not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Auth service not initialized"
        )
        
    try:
        auth_url, state = auth_service.get_authorization_url(user_id)
        return AuthUrlResponse(authorization_url=auth_url, state=state)
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.get("/api/auth/callback", tags=["Authentication"])
async def auth_callback(
    code: str = Query(..., description="Authorization code from Google"),
    state: str = Query(..., description="State parameter for CSRF validation")
):
    """Handle OAuth callback from Google.
    
    Exchanges the authorization code for access tokens and stores
    them securely for future API calls.
    
    Args:
        code: Authorization code from Google
        state: State parameter containing user_id
        
    Returns:
        Redirect or success message
    """
    global auth_service
    
    if not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Auth service not initialized"
        )
        
    # Extract user_id from state (format: user_id:random_token)
    try:
        user_id = state.split(":")[0]
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=400,
            detail="Invalid state parameter"
        )
        
    try:
        await auth_service.exchange_code(code, user_id)
        return {
            "status": "success",
            "message": "Successfully connected Google account",
            "user_id": user_id
        }
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )


@app.get("/api/auth/status", response_model=AuthStatusResponse, tags=["Authentication"])
async def get_auth_status(
    user_id: str = Query(..., description="User identifier")
):
    """Check authentication status for a user.
    
    Returns whether the user has connected their Google account
    and what scopes are available.
    
    Args:
        user_id: User identifier
        
    Returns:
        AuthStatusResponse: Connection status and scopes
    """
    global auth_service
    
    if not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Auth service not initialized"
        )
        
    status = await auth_service.check_auth_status(user_id)
    return AuthStatusResponse(**status)


@app.delete("/api/auth/disconnect", tags=["Authentication"])
async def disconnect_google(
    user_id: str = Query(..., description="User identifier")
):
    """Disconnect user's Google account.
    
    Revokes access tokens and removes stored credentials.
    
    Args:
        user_id: User identifier
        
    Returns:
        Success message
    """
    global auth_service
    
    if not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Auth service not initialized"
        )
        
    try:
        await auth_service.revoke_access(user_id)
        return {
            "status": "success",
            "message": "Google account disconnected"
        }
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# -----------------
# Health & Metrics Endpoints
# -----------------

@app.get("/api/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Check health of all system components.
    
    Returns status of orchestrator, auth service, and all agents.
    
    Returns:
        HealthResponse: Health status of all components
    """
    global orchestrator, auth_service
    
    health = {
        "status": "healthy",
        "orchestrator": {},
        "auth_service": False,
        "agents": {}
    }
    
    if orchestrator:
        orch_health = await orchestrator.health_check()
        health["orchestrator"] = orch_health.get("orchestrator", {})
        health["agents"] = orch_health.get("agents", {})
        
    if auth_service:
        health["auth_service"] = await auth_service.health_check()
        
    # Determine overall status
    if not health["orchestrator"].get("initialized") or not health["auth_service"]:
        health["status"] = "degraded"
        
    return HealthResponse(**health)


@app.get("/api/metrics", response_model=MetricsResponse, tags=["System"])
async def get_metrics():
    """Get system metrics.
    
    Returns metrics from orchestrator and auth service.
    
    Returns:
        MetricsResponse: System metrics
    """
    global orchestrator, auth_service
    
    metrics = {
        "orchestrator": {},
        "auth": {}
    }
    
    if orchestrator:
        metrics["orchestrator"] = orchestrator.get_metrics()
        
    if auth_service:
        metrics["auth"] = auth_service.get_metrics()
        
    return MetricsResponse(**metrics)


@app.get("/", tags=["System"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": "Multi-Agent Data Connector",
        "version": "1.0.0",
        "documentation": "/docs",
        "health": "/api/health"
    }


# -----------------
# Run Application
# -----------------

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug
    )

