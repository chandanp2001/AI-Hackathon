"""Request and response models for query handling."""

from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """Request model for submitting a natural language query.
    
    Args:
        query: The natural language query from the user
        user_id: Unique identifier for the user making the request
        threshold_override: Optional override for relevance threshold
        
    Examples:
        >>> request = QueryRequest(
        ...     query="What meetings do I have tomorrow?",
        ...     user_id="user_123"
        ... )
    """
    
    query: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural language query"
    )
    user_id: str = Field(
        ...,
        description="User identifier for credential lookup"
    )
    threshold_override: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Override default relevance threshold"
    )


class AgentContribution(BaseModel):
    """Details of an agent's contribution to the response.
    
    Args:
        agent_name: Name of the contributing agent
        relevance_score: The agent's relevance score for the query
        justification: Why the agent determined it was relevant
        data_count: Number of data items retrieved
        execution_time_ms: Time taken to fetch data in milliseconds
    """
    
    agent_name: str = Field(..., description="Name of the agent")
    relevance_score: float = Field(..., ge=0.0, le=1.0)
    justification: str = Field(..., description="Why agent was relevant")
    data_count: int = Field(..., ge=0, description="Items retrieved")
    execution_time_ms: float = Field(..., description="Execution time")


class QueryResponse(BaseModel):
    """Response model for query results.
    
    Args:
        query: Original query submitted
        response: Synthesized natural language response
        agents_triggered: List of agents that contributed
        total_execution_time_ms: Total time to process query
        timestamp: When the response was generated
        
    Examples:
        >>> response = QueryResponse(
        ...     query="What meetings do I have tomorrow?",
        ...     response="You have 3 meetings scheduled for tomorrow...",
        ...     agents_triggered=[...],
        ...     total_execution_time_ms=1234.5
        ... )
    """
    
    query: str = Field(..., description="Original query")
    response: str = Field(..., description="Synthesized response")
    agents_triggered: list[AgentContribution] = Field(
        default_factory=list,
        description="Agents that contributed to response"
    )
    raw_data: Optional[dict] = Field(
        default=None,
        description="Raw data from agents (for debugging)"
    )
    total_execution_time_ms: float = Field(
        ...,
        description="Total processing time"
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Response timestamp"
    )


class ClarificationResponse(BaseModel):
    """Response model when the query needs clarification.
    
    Returned when the LLM cannot determine the user's intent
    or when no agents meet the relevance threshold.
    
    Args:
        type: Always "clarification"
        query: Original query submitted
        needs_clarification: Always True
        reason: Why clarification is needed
        suggested_questions: Questions to help the user clarify
        likely_sources: Data sources that might be relevant
        agent_scores: Relevance scores from each agent
    """
    
    type: str = Field(default="clarification", description="Response type")
    query: str = Field(..., description="Original query")
    needs_clarification: bool = Field(default=True, description="Clarification needed flag")
    reason: str = Field(..., description="Why clarification is needed")
    suggested_questions: List[str] = Field(
        default_factory=list,
        description="Questions to help clarify the query"
    )
    likely_sources: List[str] = Field(
        default_factory=list,
        description="Data sources that might be relevant"
    )
    agent_scores: Dict[str, float] = Field(
        default_factory=dict,
        description="Relevance scores from each agent"
    )


# =============================================================================
# Slack Search Models
# =============================================================================

class SlackSearchRequest(BaseModel):
    """Request model for Slack search queries.
    
    Supports both channel-specific and global (channel-free) searches.
    When no channel is specified, the search is performed across all
    accessible channels.
    
    Args:
        query: The search query string (required)
        user_id: User identifier for credential lookup (required)
        channel: Optional specific channel to search
        channels: Optional list of channels to search
        time_range: Optional time range filter ('today', 'last_week', etc.)
        limit: Maximum results per channel (default: 10)
        max_channels: Maximum channels to return in global search (default: 10)
        include_dms: Whether to include DMs in global search (default: False)
        deep_search: Enable thorough search mode (default: False)
        
    Examples:
        >>> # Global search across all channels
        >>> request = SlackSearchRequest(
        ...     query="deployment issues",
        ...     user_id="user_123"
        ... )
        >>> 
        >>> # Channel-specific search
        >>> request = SlackSearchRequest(
        ...     query="deployment issues",
        ...     user_id="user_123",
        ...     channel="engineering"
        ... )
    """
    
    query: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Search query string"
    )
    user_id: str = Field(
        ...,
        description="User identifier for credential lookup"
    )
    channel: Optional[str] = Field(
        default=None,
        description="Specific channel name or ID to search"
    )
    channels: Optional[List[str]] = Field(
        default=None,
        description="List of channel names or IDs to search"
    )
    time_range: Optional[str] = Field(
        default=None,
        description="Time range filter: 'today', 'yesterday', 'last_week', 'last_month'"
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum results per channel"
    )
    max_channels: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum channels to return in global search"
    )
    include_dms: bool = Field(
        default=False,
        description="Include DMs in global search"
    )
    deep_search: bool = Field(
        default=False,
        description="Enable thorough search mode"
    )


class SlackFollowUpSuggestion(BaseModel):
    """A follow-up suggestion for narrowing down search results.
    
    Args:
        type: Type of suggestion (channel, time_range, keyword, context)
        value: The value to use for narrowing
        label: Human-readable label for display
        description: Optional detailed description
    """
    
    type: str = Field(..., description="Suggestion type")
    value: str = Field(..., description="Value for the suggestion")
    label: str = Field(..., description="Human-readable label")
    description: Optional[str] = Field(None, description="Detailed description")


class SlackChannelSummary(BaseModel):
    """Summary of search results for a single channel.
    
    Args:
        channel_id: Slack channel ID
        channel_name: Human-readable channel name
        message_count: Number of matching messages
        relevance_score: Relevance score from Slack API (can be > 1.0)
        top_message_preview: Preview of the most relevant message
    """
    
    channel_id: str = Field(..., description="Slack channel ID")
    channel_name: str = Field(..., description="Channel name")
    message_count: int = Field(..., ge=0, description="Number of matches")
    relevance_score: float = Field(..., ge=0.0, description="Relevance score from Slack")
    top_message_preview: Optional[str] = Field(None, description="Preview of top message")


class SlackMessage(BaseModel):
    """A Slack message with metadata.
    
    Args:
        text: Message content
        user: User ID who sent the message
        timestamp: Message timestamp
        thread_ts: Thread timestamp if part of a thread
        relevance_score: Relevance score for the search
        permalink: Direct link to the message
        replies: Optional list of thread replies
    """
    
    text: str = Field(..., description="Message content")
    user: str = Field(..., description="User ID")
    timestamp: str = Field(..., description="Message timestamp")
    thread_ts: Optional[str] = Field(None, description="Thread timestamp")
    relevance_score: float = Field(default=0.0, description="Relevance score")
    permalink: Optional[str] = Field(None, description="Message permalink")
    replies: Optional[List[Dict[str, Any]]] = Field(None, description="Thread replies")


class SlackChannelResult(BaseModel):
    """Search results for a single channel.
    
    Args:
        channel_name: Name of the channel
        channel_id: Slack channel ID
        messages: List of matching messages
        relevance: Overall relevance score for this channel
        message_count: Total number of messages found
    """
    
    channel_name: str = Field(..., description="Channel name")
    channel_id: Optional[str] = Field(None, description="Channel ID")
    messages: List[SlackMessage] = Field(default_factory=list, description="Messages")
    relevance: float = Field(default=0.5, description="Channel relevance")
    message_count: int = Field(default=0, description="Total message count")


class SlackSearchResponse(BaseModel):
    """Response model for Slack search results.
    
    Contains results grouped by channel, with summaries and follow-up
    suggestions for narrowing down the search.
    
    Args:
        query: The original search query
        results: Dict mapping channel names to their results
        channel_summaries: List of channel summaries with match counts
        total_message_count: Total messages found across all channels
        channels_searched: Number of channels that were searched
        follow_up_suggestions: Suggestions for narrowing down results
        summary: Human-readable summary of findings
        is_global_search: Whether this was a global (channel-free) search
        processing_time_ms: Time taken to process the search
        timestamp: When the response was generated
        
    Examples:
        >>> response = SlackSearchResponse(
        ...     query="deployment issues",
        ...     results={"engineering": {...}, "devops": {...}},
        ...     channel_summaries=[...],
        ...     total_message_count=20,
        ...     channels_searched=2,
        ...     follow_up_suggestions=[...],
        ...     summary="Found 20 messages across 2 channels..."
        ... )
    """
    
    query: str = Field(..., description="Original search query")
    results: Dict[str, Any] = Field(
        default_factory=dict,
        description="Results by channel"
    )
    channel_summaries: List[SlackChannelSummary] = Field(
        default_factory=list,
        description="Summary per channel"
    )
    total_message_count: int = Field(
        default=0,
        ge=0,
        description="Total messages found"
    )
    channels_searched: int = Field(
        default=0,
        ge=0,
        description="Number of channels searched"
    )
    follow_up_suggestions: List[SlackFollowUpSuggestion] = Field(
        default_factory=list,
        description="Suggestions for narrowing results"
    )
    summary: str = Field(
        default="",
        description="Human-readable summary"
    )
    is_global_search: bool = Field(
        default=False,
        description="Whether this was a global search"
    )
    processing_time_ms: float = Field(
        default=0.0,
        description="Processing time in milliseconds"
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Response timestamp"
    )

