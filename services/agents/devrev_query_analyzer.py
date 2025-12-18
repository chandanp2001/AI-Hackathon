"""DevRev Query Analyzer for intelligent query understanding.

This module provides:
- Query type detection (boards, sprints, tickets, status, etc.)
- Entity extraction (ticket IDs, user names, board names, statuses)
- Context accumulation from follow-up conversations
- API call planning based on query intent
"""

import logging
import re
from typing import Optional, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class DevRevQueryType(str, Enum):
    """Types of DevRev queries."""
    BOARD_LIST = "board_list"          # List all boards/vistas
    BOARD_DETAIL = "board_detail"      # Get specific board details
    TICKET_LIST = "ticket_list"        # List tickets with filters
    TICKET_DETAIL = "ticket_detail"    # Get specific ticket details
    TICKET_STATUS = "ticket_status"    # Query about ticket status
    SPRINT_QUERY = "sprint_query"      # Sprint-related queries
    PART_LIST = "part_list"            # List parts/pods
    PART_DETAIL = "part_detail"        # Get specific part details
    USER_QUERY = "user_query"          # Query about users/assignees
    SEARCH = "search"                  # General semantic search
    TIMELINE = "timeline"              # Get ticket timeline/comments
    STATS = "stats"                    # Statistics/counts query
    UNKNOWN = "unknown"                # Cannot determine type


@dataclass
class ExtractedEntities:
    """Entities extracted from a DevRev query."""
    ticket_ids: list[str] = field(default_factory=list)
    board_names: list[str] = field(default_factory=list)
    part_names: list[str] = field(default_factory=list)
    user_names: list[str] = field(default_factory=list)
    user_emails: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)  # ticket, issue, bug, task
    search_terms: list[str] = field(default_factory=list)
    is_self_reference: bool = False  # "my tickets", "assigned to me"
    sprint_reference: Optional[str] = None  # "current sprint", "sprint 5"


@dataclass
class QueryAnalysisResult:
    """Result of analyzing a DevRev query."""
    query_type: DevRevQueryType
    entities: ExtractedEntities
    needs_clarification: bool = False
    clarification_questions: list[str] = field(default_factory=list)
    suggested_api_calls: list[str] = field(default_factory=list)
    confidence: float = 1.0
    original_query: str = ""


# Patterns for extracting entities
TICKET_ID_PATTERNS = [
    r'\b(ISS-\d+)\b',           # ISS-1234
    r'\b(TKT-\d+)\b',           # TKT-5678
    r'\b(BUG-\d+)\b',           # BUG-999
    r'\b(TASK-\d+)\b',          # TASK-123
    r'\b(don:core:[^:\s]+:devo/[^\s]+/work/[^\s]+)\b',  # Full work ID
]

EMAIL_PATTERN = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

STATUS_KEYWORDS = {
    "open": ["open", "new", "created", "to do", "todo", "backlog"],
    "in_progress": ["in progress", "in-progress", "working", "active", "started", "doing"],
    "in_review": ["in review", "review", "reviewing", "pending review"],
    "resolved": ["resolved", "fixed", "done", "complete", "completed", "finished"],
    "closed": ["closed", "closed out"],
    "blocked": ["blocked", "stuck", "waiting"],
    "queued": ["queued", "triage", "triaged"],
}

PRIORITY_KEYWORDS = {
    "p0": ["p0", "critical", "blocker", "urgent", "emergency"],
    "p1": ["p1", "high", "important"],
    "p2": ["p2", "medium", "normal"],
    "p3": ["p3", "low", "minor"],
}

TYPE_KEYWORDS = {
    "ticket": ["ticket", "tickets", "support ticket"],
    "issue": ["issue", "issues", "bug", "bugs", "defect", "defects"],
    "task": ["task", "tasks"],
    "feature": ["feature", "features", "enhancement", "enhancements"],
}

SELF_REFERENCE_PATTERNS = [
    r'\bmy\s+(tickets?|issues?|bugs?|tasks?|work\s*items?)\b',
    r'\bassigned\s+to\s+me\b',
    r'\bowned\s+by\s+me\b',
    r'\bbelonging\s+to\s+me\b',
    r'\bfor\s+me\b',
    r'\bwhat\s+(am\s+i|i\s+am)\s+working\s+on\b',
]

SPRINT_PATTERNS = [
    r'\bcurrent\s+sprint\b',
    r'\bactive\s+sprint\b',
    r'\bthis\s+sprint\b',
    r'\bsprint\s+(\d+)\b',
    r'\bsprint\s+([a-zA-Z]+\s*\d*)\b',  # sprint alpha, sprint Q4
]

BOARD_KEYWORDS = ["board", "boards", "vista", "vistas", "view", "views"]
PART_KEYWORDS = ["part", "parts", "pod", "pods", "component", "components", "module", "modules"]


class DevRevQueryAnalyzer:
    """Analyzes DevRev queries to determine intent and extract entities.
    
    This class processes natural language queries to:
    1. Determine the query type (board list, ticket detail, sprint query, etc.)
    2. Extract relevant entities (ticket IDs, user names, statuses, etc.)
    3. Identify if clarification is needed
    4. Suggest appropriate API calls
    
    Examples:
        >>> analyzer = DevRevQueryAnalyzer()
        >>> result = analyzer.analyze("What's the status of ISS-1234?")
        >>> result.query_type
        DevRevQueryType.TICKET_STATUS
        >>> result.entities.ticket_ids
        ['ISS-1234']
    """
    
    def __init__(self):
        """Initialize the query analyzer."""
        self._context: dict[str, Any] = {}
        
    def analyze(
        self, 
        query: str, 
        conversation_context: Optional[list[dict[str, str]]] = None
    ) -> QueryAnalysisResult:
        """Analyze a DevRev query to understand intent and extract entities.
        
        Args:
            query: The user's natural language query
            conversation_context: Optional previous conversation for context
            
        Returns:
            QueryAnalysisResult with query type, entities, and API suggestions
        """
        query_lower = query.lower().strip()
        
        # Extract entities
        entities = self._extract_entities(query)
        
        # Merge with conversation context if available
        if conversation_context:
            entities = self._merge_context(entities, conversation_context)
        
        # Determine query type
        query_type = self._determine_query_type(query_lower, entities)
        
        # Check if clarification is needed
        needs_clarification, questions = self._check_clarification_needed(
            query_type, entities, query_lower
        )
        
        # Suggest API calls based on query type and entities
        api_calls = self._suggest_api_calls(query_type, entities)
        
        # Calculate confidence
        confidence = self._calculate_confidence(query_type, entities)
        
        return QueryAnalysisResult(
            query_type=query_type,
            entities=entities,
            needs_clarification=needs_clarification,
            clarification_questions=questions,
            suggested_api_calls=api_calls,
            confidence=confidence,
            original_query=query
        )
    
    def _extract_entities(self, query: str) -> ExtractedEntities:
        """Extract all relevant entities from the query."""
        entities = ExtractedEntities()
        query_lower = query.lower()
        
        # Extract ticket IDs
        for pattern in TICKET_ID_PATTERNS:
            matches = re.findall(pattern, query, re.IGNORECASE)
            entities.ticket_ids.extend(matches)
        
        # Extract email addresses
        emails = re.findall(EMAIL_PATTERN, query)
        entities.user_emails.extend(emails)
        
        # Check for self-reference
        for pattern in SELF_REFERENCE_PATTERNS:
            if re.search(pattern, query_lower):
                entities.is_self_reference = True
                break
        
        # Extract statuses
        for status, keywords in STATUS_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    if status not in entities.statuses:
                        entities.statuses.append(status)
                    break
        
        # Extract priorities
        for priority, keywords in PRIORITY_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    if priority not in entities.priorities:
                        entities.priorities.append(priority)
                    break
        
        # Extract work item types
        for work_type, keywords in TYPE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    if work_type not in entities.types:
                        entities.types.append(work_type)
                    break
        
        # Extract sprint references
        for pattern in SPRINT_PATTERNS:
            match = re.search(pattern, query_lower)
            if match:
                entities.sprint_reference = match.group(0)
                break
        
        # Extract user names (pattern: "for [Name]", "assigned to [Name]", "owned by [Name]")
        user_patterns = [
            r'(?:assigned\s+to|owned\s+by|for)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)'s\s+(?:tickets?|issues?|tasks?)",
        ]
        for pattern in user_patterns:
            matches = re.findall(pattern, query)
            for match in matches:
                name = match.strip()
                if name.lower() not in ['me', 'my', 'i']:
                    entities.user_names.append(name)
        
        # Extract quoted search terms
        quoted = re.findall(r'"([^"]+)"', query)
        entities.search_terms.extend(quoted)
        
        return entities
    
    def _merge_context(
        self, 
        entities: ExtractedEntities,
        conversation_context: list[dict[str, str]]
    ) -> ExtractedEntities:
        """Merge entities with information from conversation context."""
        # Look through recent conversation for entity mentions
        for msg in reversed(conversation_context[-6:]):
            content = msg.get("content", "")
            
            # Check for previous ticket IDs
            for pattern in TICKET_ID_PATTERNS:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    if match not in entities.ticket_ids:
                        entities.ticket_ids.append(match)
            
            # Check for board/part names mentioned in assistant responses
            if msg.get("role") == "assistant":
                # Look for board names in context
                board_matches = re.findall(r'board[:\s]+["\']?([^"\']+)["\']?', content, re.IGNORECASE)
                for match in board_matches:
                    if match not in entities.board_names:
                        entities.board_names.append(match.strip())
        
        return entities
    
    def _determine_query_type(
        self, 
        query_lower: str, 
        entities: ExtractedEntities
    ) -> DevRevQueryType:
        """Determine the type of DevRev query."""
        
        # Specific ticket detail/status query
        if entities.ticket_ids:
            if any(word in query_lower for word in ["status", "state", "progress", "update"]):
                return DevRevQueryType.TICKET_STATUS
            if any(word in query_lower for word in ["detail", "info", "information", "describe", "show", "what is", "what's"]):
                return DevRevQueryType.TICKET_DETAIL
            if any(word in query_lower for word in ["timeline", "comments", "history", "activity", "updates"]):
                return DevRevQueryType.TIMELINE
            # Default for ticket IDs: detail
            return DevRevQueryType.TICKET_DETAIL
        
        # Board queries
        if any(keyword in query_lower for keyword in BOARD_KEYWORDS):
            if any(word in query_lower for word in ["list", "all", "show", "what", "which", "how many"]):
                return DevRevQueryType.BOARD_LIST
            return DevRevQueryType.BOARD_DETAIL
        
        # Part/pod queries
        if any(keyword in query_lower for keyword in PART_KEYWORDS):
            if any(word in query_lower for word in ["list", "all", "show", "what", "which", "how many"]):
                return DevRevQueryType.PART_LIST
            return DevRevQueryType.PART_DETAIL
        
        # Sprint queries
        if entities.sprint_reference or "sprint" in query_lower:
            return DevRevQueryType.SPRINT_QUERY
        
        # User queries
        if any(word in query_lower for word in ["who", "team", "members", "developers", "engineers"]):
            return DevRevQueryType.USER_QUERY
        
        # Statistics queries
        if any(word in query_lower for word in ["how many", "count", "total", "statistics", "stats", "summary"]):
            return DevRevQueryType.STATS
        
        # Ticket list queries
        if (entities.is_self_reference or 
            entities.statuses or 
            entities.priorities or
            entities.types or
            entities.user_names or
            entities.user_emails):
            return DevRevQueryType.TICKET_LIST
        
        # General ticket list keywords
        ticket_list_keywords = ["tickets", "issues", "bugs", "tasks", "work items", "list", "show"]
        if any(keyword in query_lower for keyword in ticket_list_keywords):
            return DevRevQueryType.TICKET_LIST
        
        # Search fallback for descriptive queries
        if len(query_lower.split()) > 2:
            return DevRevQueryType.SEARCH
        
        return DevRevQueryType.UNKNOWN
    
    def _check_clarification_needed(
        self,
        query_type: DevRevQueryType,
        entities: ExtractedEntities,
        query_lower: str
    ) -> tuple[bool, list[str]]:
        """Check if clarification is needed and generate questions."""
        questions = []
        
        # Unknown query type needs clarification
        if query_type == DevRevQueryType.UNKNOWN:
            questions.append("Could you please clarify what DevRev information you're looking for?")
            questions.append("Are you looking for: tickets/issues, boards, parts/pods, or team members?")
            return True, questions
        
        # Ticket list without filters
        if query_type == DevRevQueryType.TICKET_LIST:
            has_filter = (
                entities.is_self_reference or
                entities.statuses or
                entities.priorities or
                entities.user_names or
                entities.user_emails or
                entities.types or
                entities.part_names or
                entities.board_names
            )
            if not has_filter:
                questions.append("Would you like to see tickets assigned to you, or for a specific person?")
                questions.append("Should I filter by status (open, in progress, resolved)?")
                questions.append("Would you like to filter by a specific board or part?")
                return True, questions
        
        # Board detail without board name
        if query_type == DevRevQueryType.BOARD_DETAIL and not entities.board_names:
            questions.append("Which board would you like to see details for?")
            return True, questions
        
        # Part detail without part name
        if query_type == DevRevQueryType.PART_DETAIL and not entities.part_names:
            questions.append("Which part/pod would you like to see details for?")
            return True, questions
        
        # Sprint query without board context
        if query_type == DevRevQueryType.SPRINT_QUERY and not entities.board_names:
            questions.append("Which board's sprint would you like to view?")
            return True, questions
        
        return False, questions
    
    def _suggest_api_calls(
        self,
        query_type: DevRevQueryType,
        entities: ExtractedEntities
    ) -> list[str]:
        """Suggest API calls based on query type and entities."""
        calls = []
        
        if query_type == DevRevQueryType.BOARD_LIST:
            calls.append("list_vistas")
        
        elif query_type == DevRevQueryType.BOARD_DETAIL:
            if entities.board_names:
                calls.append("list_vistas")  # To find the board ID
                calls.append("get_vista")
            else:
                calls.append("list_vistas")
        
        elif query_type == DevRevQueryType.TICKET_DETAIL:
            if entities.ticket_ids:
                calls.append("get_work")
            else:
                calls.append("list_works")
        
        elif query_type == DevRevQueryType.TICKET_STATUS:
            calls.append("get_work")
            calls.append("list_timeline_entries")  # For recent activity
        
        elif query_type == DevRevQueryType.TICKET_LIST:
            if entities.is_self_reference:
                calls.append("get_self")  # Get current user ID
            calls.append("list_works")
        
        elif query_type == DevRevQueryType.SPRINT_QUERY:
            calls.append("list_vistas")
            calls.append("list_works")  # With sprint/board filter
        
        elif query_type == DevRevQueryType.PART_LIST:
            calls.append("list_parts")
        
        elif query_type == DevRevQueryType.PART_DETAIL:
            calls.append("list_parts")  # To find part ID
            calls.append("get_part")
            calls.append("list_works")  # Works in this part
        
        elif query_type == DevRevQueryType.USER_QUERY:
            calls.append("list_dev_users")
        
        elif query_type == DevRevQueryType.TIMELINE:
            if entities.ticket_ids:
                calls.append("get_work")
                calls.append("list_timeline_entries")
        
        elif query_type == DevRevQueryType.STATS:
            calls.append("list_works")  # With count
            calls.append("list_vistas")
            calls.append("list_parts")
        
        elif query_type == DevRevQueryType.SEARCH:
            calls.append("hybrid_search")
        
        return calls
    
    def _calculate_confidence(
        self,
        query_type: DevRevQueryType,
        entities: ExtractedEntities
    ) -> float:
        """Calculate confidence score for the analysis."""
        if query_type == DevRevQueryType.UNKNOWN:
            return 0.2
        
        confidence = 0.5
        
        # Specific ticket ID = high confidence
        if entities.ticket_ids:
            confidence += 0.4
        
        # Clear filters increase confidence
        if entities.is_self_reference:
            confidence += 0.2
        if entities.statuses:
            confidence += 0.1
        if entities.user_names or entities.user_emails:
            confidence += 0.1
        if entities.board_names:
            confidence += 0.1
        if entities.part_names:
            confidence += 0.1
        
        return min(1.0, confidence)
    
    def update_context(self, key: str, value: Any) -> None:
        """Update the analyzer's context for follow-up queries.
        
        Args:
            key: Context key (e.g., 'selected_board', 'last_ticket')
            value: Context value
        """
        self._context[key] = value
        logger.debug(f"Updated analyzer context: {key} = {value}")
    
    def get_context(self, key: str, default: Any = None) -> Any:
        """Get a value from the analyzer's context.
        
        Args:
            key: Context key
            default: Default value if key not found
            
        Returns:
            Context value or default
        """
        return self._context.get(key, default)
    
    def clear_context(self) -> None:
        """Clear all stored context."""
        self._context.clear()

