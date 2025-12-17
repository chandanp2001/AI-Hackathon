"""Agent response models and data structures."""

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field
from enum import Enum


class AgentType(str, Enum):
    """Enumeration of available agent types."""
    CALENDAR = "calendar"
    GMAIL = "gmail"
    DRIVE = "drive"
    SLACK = "slack"
    DEVREV = "devrev"


class ConfidenceLevel(str, Enum):
    """Confidence level for agent responses."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RelevanceScore(BaseModel):
    """Relevance score returned by an agent after query evaluation.
    
    Args:
        agent_name: Name of the agent providing the score
        score: Relevance score from 0.0 to 1.0
        justification: Brief explanation for the score
        suggested_search_terms: Terms the agent would use to search
        date_range: LLM-extracted date range for time-based queries
        confidence_level: Agent's confidence in the relevance assessment
        
    Examples:
        >>> score = RelevanceScore(
        ...     agent_name="calendar",
        ...     score=0.95,
        ...     justification="Query explicitly asks about meetings",
        ...     suggested_search_terms=["meeting", "tomorrow"],
        ...     date_range={"start": "2024-12-22T00:00:00", "end": "2024-12-23T00:00:00", "type": "specific_date"},
        ...     confidence_level=ConfidenceLevel.HIGH
        ... )
    """
    
    agent_name: str = Field(..., description="Agent identifier")
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Relevance score"
    )
    justification: str = Field(
        ...,
        max_length=500,
        description="Explanation for the score"
    )
    suggested_search_terms: list[str] = Field(
        default_factory=list,
        description="Suggested search terms for data retrieval"
    )
    date_range: Optional[dict[str, Any]] = Field(
        default=None,
        description="LLM-extracted date range with start, end (ISO format), and type"
    )
    confidence_level: ConfidenceLevel = Field(
        default=ConfidenceLevel.MEDIUM,
        description="Agent's confidence in the relevance assessment (high, medium, low)"
    )


class CalendarEvent(BaseModel):
    """Structured representation of a calendar event.
    
    Args:
        event_id: Unique identifier for the event
        title: Event title/summary
        start_time: Event start datetime
        end_time: Event end datetime
        location: Event location (if any)
        description: Event description/notes
        attendees: List of attendee emails
        organizer: Event organizer email
        meeting_link: Video conferencing link (if any)
        status: Event status (confirmed, tentative, cancelled)
    """
    
    event_id: str = Field(..., description="Unique event ID")
    title: str = Field(..., description="Event title")
    start_time: datetime = Field(..., description="Start time")
    end_time: datetime = Field(..., description="End time")
    location: Optional[str] = Field(None, description="Location")
    description: Optional[str] = Field(None, description="Description")
    attendees: list[str] = Field(default_factory=list, description="Attendee emails")
    organizer: Optional[str] = Field(None, description="Organizer email")
    meeting_link: Optional[str] = Field(None, description="Video meeting URL")
    status: str = Field(default="confirmed", description="Event status")
    is_all_day: bool = Field(default=False, description="All-day event flag")


class EmailMessage(BaseModel):
    """Structured representation of an email message.
    
    Args:
        message_id: Unique identifier for the message
        thread_id: Thread/conversation ID
        subject: Email subject line
        sender: Sender email address
        recipients: List of recipient addresses
        date: When the email was sent
        snippet: Preview snippet of content
        body_preview: First portion of email body
        labels: Gmail labels applied
        has_attachments: Whether email has attachments
        is_unread: Whether email is unread
    """
    
    message_id: str = Field(..., description="Unique message ID")
    thread_id: str = Field(..., description="Thread ID")
    subject: str = Field(..., description="Email subject")
    sender: str = Field(..., description="Sender email")
    recipients: list[str] = Field(default_factory=list, description="Recipients")
    date: datetime = Field(..., description="Send date")
    snippet: str = Field(..., description="Content preview")
    body_preview: Optional[str] = Field(None, description="Body preview")
    labels: list[str] = Field(default_factory=list, description="Labels")
    has_attachments: bool = Field(default=False, description="Has attachments")
    is_unread: bool = Field(default=False, description="Unread status")


class DriveFile(BaseModel):
    """Structured representation of a Google Drive file.
    
    Args:
        file_id: Unique identifier for the file
        name: File name
        mime_type: MIME type of the file
        created_time: When file was created
        modified_time: When file was last modified
        size_bytes: File size in bytes
        web_view_link: Link to view file
        owners: List of owner emails
        shared: Whether file is shared
        parent_folders: Parent folder names
        content_preview: Preview of file content (for docs)
    """
    
    file_id: str = Field(..., description="Unique file ID")
    name: str = Field(..., description="File name")
    mime_type: str = Field(..., description="MIME type")
    created_time: Optional[datetime] = Field(None, description="Creation time")
    modified_time: Optional[datetime] = Field(None, description="Last modified")
    size_bytes: Optional[int] = Field(None, description="Size in bytes")
    web_view_link: Optional[str] = Field(None, description="View URL")
    owners: list[str] = Field(default_factory=list, description="Owner emails")
    shared: bool = Field(default=False, description="Is shared")
    parent_folders: list[str] = Field(default_factory=list, description="Parent folders")
    content_preview: Optional[str] = Field(None, description="Content preview")


class AgentResult(BaseModel):
    """Result returned by an agent after data retrieval.
    
    Args:
        agent_name: Name of the agent
        agent_type: Type of agent (calendar, gmail, drive)
        success: Whether retrieval was successful
        error_message: Error details if failed
        data: Retrieved data items
        metadata: Additional context about the retrieval
        execution_time_ms: Time taken to retrieve data
        
    Examples:
        >>> result = AgentResult(
        ...     agent_name="calendar",
        ...     agent_type=AgentType.CALENDAR,
        ...     success=True,
        ...     data=[CalendarEvent(...)],
        ...     execution_time_ms=523.4
        ... )
    """
    
    agent_name: str = Field(..., description="Agent identifier")
    agent_type: AgentType = Field(..., description="Agent type")
    success: bool = Field(..., description="Retrieval success")
    error_message: Optional[str] = Field(None, description="Error if failed")
    data: list[Any] = Field(default_factory=list, description="Retrieved items")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context"
    )
    execution_time_ms: float = Field(..., description="Execution time")
    query_used: Optional[str] = Field(None, description="Actual query executed")

