"""Session and message models for conversation persistence.

Provides Pydantic models for:
- Session management (create, list, retrieve)
- Message storage within sessions
- Session summaries for conversation memory
"""

from datetime import datetime
from typing import Optional, Any
from enum import Enum
from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    """Role of a message in a conversation."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageCreate(BaseModel):
    """Request model for creating a new message."""
    role: MessageRole
    content: str
    metadata: Optional[dict[str, Any]] = None


class Message(BaseModel):
    """A message within a session."""
    message_id: str
    session_id: str
    role: MessageRole
    content: str
    metadata: Optional[dict[str, Any]] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class SessionCreate(BaseModel):
    """Request model for creating a new session."""
    user_id: str
    title: Optional[str] = None


class Session(BaseModel):
    """A conversation session."""
    session_id: str
    user_id: str
    title: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class SessionWithMessages(Session):
    """A session with its messages included."""
    messages: list[Message] = Field(default_factory=list)
    summary: Optional[str] = None


class SessionSummary(BaseModel):
    """Summary of older messages in a session for memory management."""
    session_id: str
    summary: str
    summarized_up_to: Optional[str] = None  # message_id of last summarized message
    token_count: int = 0
    updated_at: datetime
    
    class Config:
        from_attributes = True


class SessionListResponse(BaseModel):
    """Response for listing sessions."""
    sessions: list[Session]
    total: int


class QueryRequestWithSession(BaseModel):
    """Extended query request with session support."""
    query: str
    user_id: str
    session_id: Optional[str] = None
    threshold_override: Optional[float] = None
    skip_cache: bool = Field(
        default=True,
        description="Skip cache and fetch fresh data from agents"
    )

