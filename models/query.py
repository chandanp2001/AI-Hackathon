"""Request and response models for query handling."""

from datetime import datetime
from typing import Optional
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

