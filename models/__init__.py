"""Pydantic models for the Multi-Agent Data Connector system."""

from models.query import QueryRequest, QueryResponse, AgentContribution
from models.agent_response import (
    RelevanceScore,
    AgentResult,
    AgentType,
    CalendarEvent,
    EmailMessage,
    DriveFile,
)
from models.action import (
    ActionType,
    ActionIntent,
    ActionPlan,
    ActionStep,
    ActionResult,
    ActionStepResult,
    QueryType,
    RiskLevel,
    CreateEventParams,
    UpdateEventParams,
    SendEmailParams,
    CreateDraftParams,
    CreateDocumentParams,
    ShareFileParams,
)

__all__ = [
    # Query models
    "QueryRequest",
    "QueryResponse",
    "AgentContribution",
    # Agent response models
    "RelevanceScore",
    "AgentResult",
    "AgentType",
    "CalendarEvent",
    "EmailMessage",
    "DriveFile",
    # Action models
    "ActionType",
    "ActionIntent",
    "ActionPlan",
    "ActionStep",
    "ActionResult",
    "ActionStepResult",
    "QueryType",
    "RiskLevel",
    "CreateEventParams",
    "UpdateEventParams",
    "SendEmailParams",
    "CreateDraftParams",
    "CreateDocumentParams",
    "ShareFileParams",
]

