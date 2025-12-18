"""DevRev data connector agent.

This agent handles:
- Reading: DevRev tickets, issues, bugs, and work items
- Queries about issue tracking and ticket management
"""

import logging
import re
import time
from typing import Optional, Any, List

from google.oauth2.credentials import Credentials

from config import settings
from models.agent_response import (
    AgentResult,
    AgentType,
    RelevanceScore,
)
from services.agents.base_agent import BaseDataAgent
from services.llm.openai_service import OpenAIService
from services.data_sources.devrev_mcp_source import DevRevMCPDataSource

logger = logging.getLogger(__name__)

# Patterns for extracting assignee from query
ASSIGNEE_PATTERNS = [
    r"on\s+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",  # Email addresses after "on"
    r"for\s+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",  # Email addresses after "for"
    r"assigned to\s+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",  # Email after "assigned to"
    r"owned by\s+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",  # Email after "owned by"
    r"assigned to\s+([A-Za-z][A-Za-z\s\.]+?)(?:\s*$|\s+(?:in|on|for|with|that|which|and|,))",
    r"owned by\s+([A-Za-z][A-Za-z\s\.]+?)(?:\s*$|\s+(?:in|on|for|with|that|which|and|,))",
    r"belonging to\s+([A-Za-z][A-Za-z\s\.]+?)(?:\s*$|\s+(?:in|on|for|with|that|which|and|,))",
    r"for\s+([A-Za-z][A-Za-z\s\.]+?)(?:'s\s+tickets|\s+tickets)",
    r"([A-Za-z][A-Za-z\s\.]+?)'s\s+(?:tickets|issues|bugs|tasks)",
]

# Patterns for extracting ticket/issue IDs from query
TICKET_ID_PATTERNS = [
    r'\b(ISS-\d+)\b',           # ISS-1234
    r'\b(TKT-\d+)\b',           # TKT-5678
    r'\b(BUG-\d+)\b',           # BUG-999
    r'\b(TASK-\d+)\b',          # TASK-123
    r'\b(issue-\d+)\b',         # issue-123 (case insensitive)
    r'\b(ticket-\d+)\b',        # ticket-123 (case insensitive)
]

# Status keywords mapping
STATUS_KEYWORDS = {
    "open": ["open", "new", "created", "to do", "todo", "backlog"],
    "in_progress": ["in progress", "in-progress", "working", "active", "started", "doing", "in development"],
    "in_review": ["in review", "review", "reviewing", "pending review"],
    "resolved": ["resolved", "fixed", "done", "complete", "completed", "finished"],
    "closed": ["closed", "closed out"],
    "blocked": ["blocked", "stuck", "waiting", "on hold"],
    "queued": ["queued", "triage", "triaged"],
}

# Priority keywords mapping
PRIORITY_KEYWORDS = {
    "p0": ["p0", "critical", "blocker", "urgent", "emergency", "highest"],
    "p1": ["p1", "high", "important"],
    "p2": ["p2", "medium", "normal", "moderate"],
    "p3": ["p3", "low", "minor"],
}

# Type keywords mapping
TYPE_KEYWORDS = {
    "ticket": ["ticket", "tickets", "support ticket", "support tickets"],
    "issue": ["issue", "issues", "bug", "bugs", "defect", "defects"],
    "task": ["task", "tasks"],
    "feature": ["feature", "features", "enhancement", "enhancements"],
}

# Patterns for extracting board/part names
BOARD_PATTERNS = [
    r"in\s+(?:the\s+)?([A-Za-z][A-Za-z0-9\s\-_]+?)\s+board",
    r"from\s+(?:the\s+)?([A-Za-z][A-Za-z0-9\s\-_]+?)\s+board",
    r"([A-Za-z][A-Za-z0-9\s\-_]+?)\s+board(?:\s|$)",
    r"board\s+(?:called\s+|named\s+)?([A-Za-z][A-Za-z0-9\s\-_]+?)(?:\s|$)",
]

PART_PATTERNS = [
    # "in Issuance part/pod/component"
    r"in\s+(?:the\s+)?([A-Za-z][A-Za-z0-9\s\-_]+?)\s+(?:part|pod|component)",
    r"from\s+(?:the\s+)?([A-Za-z][A-Za-z0-9\s\-_]+?)\s+(?:part|pod|component)",
    r"for\s+(?:the\s+)?([A-Za-z][A-Za-z0-9\s\-_]+?)\s+(?:part|pod|component|team)",
    r"([A-Za-z][A-Za-z0-9\s\-_]+?)\s+(?:part|pod|component)(?:\s|$)",
    r"(?:part|pod|component)\s+(?:called\s+|named\s+)?([A-Za-z][A-Za-z0-9\s\-_]+?)(?:\s|$)",
    # "from Issuance issues" - preposition before part name, work item type after
    r"(?:in|from|for)\s+(?:the\s+)?([A-Za-z][A-Za-z0-9\s\-_]+?)\s+(?:tickets?|issues?|tasks?|works?|items?)",
    # "tickets in Issuance" or "recent tickets in Issuance" - work items IN part_name at end
    r"(?:tickets?|issues?|tasks?|works?|items?)\s+(?:in|from|for)\s+(?:the\s+)?([A-Za-z][A-Za-z0-9]+)$",
    # "Issuance tickets" - part name followed by work item type
    r"^([A-Za-z][A-Za-z0-9]+)\s+(?:tickets?|issues?|tasks?|works?|items?)",
    # "in Issuance board" - for board-style queries referencing a part
    r"in\s+(?:the\s+)?([A-Za-z][A-Za-z0-9]+)\s+board",
]

# Singleton instance for DevRevMCPDataSource
_devrev_source: Optional[DevRevMCPDataSource] = None


def get_devrev_data_source() -> DevRevMCPDataSource:
    """Get or create the DevRev MCP data source instance.
    
    Returns:
        DevRevMCPDataSource: Singleton instance
    """
    global _devrev_source
    
    if _devrev_source is None:
        _devrev_source = DevRevMCPDataSource()
        logger.info("Initialized DevRev MCP data source for agent")
    
    return _devrev_source


class DevRevAgent(BaseDataAgent):
    """DevRev data connector agent.
    
    Handles queries about:
    - Tickets and issues
    - Bugs and feature requests
    - Work items and tasks
    - Sprint and backlog items
    - Issue status and assignments
    
    Args:
        llm_service: Shared LLM service for OpenAI operations
        
    Examples:
        >>> agent = DevRevAgent()
        >>> score = await agent.evaluate_relevance("What tickets are assigned to me?")
        >>> if score.score >= 0.5:
        ...     result = await agent.fetch_data(query, credentials)
    """
    
    def __init__(self, llm_service: Optional[OpenAIService] = None):
        """Initialize the DevRev agent.
        
        Args:
            llm_service: Optional shared LLM service instance
        """
        super().__init__(llm_service)
        self._max_results = settings.devrev_max_results
        self._devrev_source: Optional[DevRevMCPDataSource] = None
        
    @property
    def agent_name(self) -> str:
        """Return the agent identifier.
        
        Returns:
            str: Agent name 'devrev'
        """
        return "devrev"
    
    @property
    def agent_type(self) -> AgentType:
        """Return the agent type.
        
        Returns:
            AgentType: DEVREV enum value
        """
        return AgentType.DEVREV
    
    @property
    def data_source_description(self) -> str:
        """Return description of the DevRev data source.
        
        Returns:
            str: Human-readable description for relevance evaluation
        """
        return """DevRev - Contains:
- Tickets and issues (bugs, feature requests, support tickets)
- Work items and tasks with status, priority, and assignments
- Sprint and backlog items
- Customer support conversations linked to tickets
- Issue descriptions, comments, and resolution details
- Assignees, reporters, and stakeholders
- Ticket stages (open, in_progress, resolved, closed)
- Part/component associations
- Tags and labels for categorization
- Timeline and activity history"""

    async def initialize(self) -> None:
        """Initialize the agent and DevRev data source.
        
        Raises:
            Exception: If initialization fails
        """
        try:
            # Check if DevRev is configured
            if not settings.devrev_api_key:
                logger.warning("DevRev API key not configured - agent will be unavailable")
                self._initialized = False
                return
                
            self._devrev_source = get_devrev_data_source()
            self._initialized = True
            logger.info(f"Agent {self.agent_name} initialized with DevRev MCP data source")
        except Exception as e:
            logger.error(f"Failed to initialize DevRev agent: {e}")
            self._initialized = False
            raise

    def _extract_assignee(self, query: str) -> Optional[str]:
        """Extract assignee name from the query.
        
        Args:
            query: User's natural language query
            
        Returns:
            Assignee name if found, None otherwise
        """
        for pattern in ASSIGNEE_PATTERNS:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                # Clean up the name
                name = re.sub(r'\s+', ' ', name)  # Normalize spaces
                # Skip if it's just common words
                if name.lower() not in ['me', 'my', 'i', 'the', 'a', 'an']:
                    logger.info(f"Extracted assignee from query: '{name}'")
                    return name
        
        # Check for "my" or "me" patterns (self-assigned)
        if re.search(r'\bmy\s+(?:tickets|issues|bugs|tasks)', query, re.IGNORECASE):
            return "self"
        if re.search(r'assigned to me\b', query, re.IGNORECASE):
            return "self"
            
        return None
    
    def _extract_ticket_ids(self, query: str) -> List[str]:
        """Extract ticket/issue IDs from the query.
        
        Args:
            query: User's natural language query
            
        Returns:
            List of ticket IDs found (e.g., ['ISS-1234', 'TKT-567'])
        """
        ticket_ids = []
        for pattern in TICKET_ID_PATTERNS:
            matches = re.findall(pattern, query, re.IGNORECASE)
            ticket_ids.extend([m.upper() for m in matches])
        
        if ticket_ids:
            logger.info(f"Extracted ticket IDs from query: {ticket_ids}")
        
        return list(set(ticket_ids))  # Remove duplicates
    
    def _extract_status(self, query: str) -> Optional[str]:
        """Extract status filter from the query.
        
        Args:
            query: User's natural language query
            
        Returns:
            Status name if found, None otherwise
        """
        query_lower = query.lower()
        
        for status, keywords in STATUS_KEYWORDS.items():
            for keyword in keywords:
                # Check for the keyword with word boundaries
                if re.search(rf'\b{re.escape(keyword)}\b', query_lower):
                    logger.info(f"Extracted status from query: '{status}' (matched '{keyword}')")
                    return status
        
        return None
    
    def _extract_priority(self, query: str) -> Optional[str]:
        """Extract priority filter from the query.
        
        Args:
            query: User's natural language query
            
        Returns:
            Priority name if found, None otherwise
        """
        query_lower = query.lower()
        
        for priority, keywords in PRIORITY_KEYWORDS.items():
            for keyword in keywords:
                if re.search(rf'\b{re.escape(keyword)}\b', query_lower):
                    logger.info(f"Extracted priority from query: '{priority}' (matched '{keyword}')")
                    return priority
        
        return None
    
    def _extract_type(self, query: str) -> Optional[str]:
        """Extract work item type from the query.
        
        Args:
            query: User's natural language query
            
        Returns:
            Work item type if found, None otherwise
        """
        query_lower = query.lower()
        
        for work_type, keywords in TYPE_KEYWORDS.items():
            for keyword in keywords:
                if re.search(rf'\b{re.escape(keyword)}\b', query_lower):
                    logger.info(f"Extracted type from query: '{work_type}' (matched '{keyword}')")
                    return work_type
        
        return None
    
    def _is_detail_query(self, query: str, ticket_ids: List[str]) -> bool:
        """Check if this is a query for ticket details.
        
        Args:
            query: User's natural language query
            ticket_ids: List of ticket IDs found
            
        Returns:
            True if this is a detail query
        """
        if not ticket_ids:
            return False
        
        query_lower = query.lower()
        detail_keywords = [
            "what is", "what's", "show me", "details", "detail",
            "describe", "description", "info", "information",
            "status of", "tell me about", "explain"
        ]
        
        for keyword in detail_keywords:
            if keyword in query_lower:
                return True
        
        # If just the ticket ID is mentioned, treat as detail query
        if len(query.split()) <= 3 and ticket_ids:
            return True
        
        return False
    
    def _is_search_query(self, query: str) -> bool:
        """Check if this is a general search query.
        
        Args:
            query: User's natural language query
            
        Returns:
            True if this should use semantic search
        """
        query_lower = query.lower()
        
        search_keywords = [
            "search", "find", "look for", "looking for",
            "related to", "about", "containing", "with keyword",
            "mentions", "mentioning"
        ]
        
        for keyword in search_keywords:
            if keyword in query_lower:
                return True
        
        return False
    
    def _is_board_query(self, query: str) -> bool:
        """Check if this is a query about boards/vistas.
        
        Args:
            query: User's natural language query
            
        Returns:
            True if asking about boards
        """
        query_lower = query.lower()
        board_keywords = [
            "board", "boards", "vista", "vistas", "views",
            "list boards", "show boards", "what boards"
        ]
        return any(kw in query_lower for kw in board_keywords)
    
    def _is_part_query(self, query: str) -> bool:
        """Check if this is a query about parts/pods.
        
        Args:
            query: User's natural language query
            
        Returns:
            True if asking about parts
        """
        query_lower = query.lower()
        part_keywords = [
            "part", "parts", "pod", "pods", "component", "components",
            "list parts", "show parts", "what parts", "list pods"
        ]
        return any(kw in query_lower for kw in part_keywords)
    
    def _extract_board_name(self, query: str) -> Optional[str]:
        """Extract board name from query.
        
        Args:
            query: User's natural language query
            
        Returns:
            Board name if found, None otherwise
        """
        for pattern in BOARD_PATTERNS:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                if name.lower() not in ['the', 'a', 'an', 'all', 'my', 'our']:
                    logger.info(f"Extracted board name from query: '{name}'")
                    return name
        return None
    
    def _extract_part_name(self, query: str) -> Optional[str]:
        """Extract part/pod name from query.
        
        Args:
            query: User's natural language query
            
        Returns:
            Part name if found, None otherwise
        """
        for pattern in PART_PATTERNS:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                if name.lower() not in ['the', 'a', 'an', 'all', 'my', 'our', 'recent', 'new', 'open']:
                    logger.info(f"Extracted part name from query: '{name}'")
                    return name
        return None

    async def fetch_data(
        self,
        query: str,
        credentials: Credentials,
        search_terms: Optional[list[str]] = None
    ) -> AgentResult:
        """Fetch DevRev work items based on the query.
        
        This method analyzes the query and routes to the appropriate fetch strategy:
        1. Specific ticket ID queries → fetch ticket details
        2. Assignee queries → filter by owner (existing logic)
        3. Status/priority/type filtered queries → list with filters
        4. Search queries → semantic search
        
        Note: DevRev uses its own authentication (API key),
        not Google OAuth credentials. The credentials parameter is
        kept for interface compatibility but not used.
        
        Args:
            query: User's natural language query
            credentials: Not used for DevRev (kept for interface compatibility)
            search_terms: Optional search terms from relevance evaluation
            
        Returns:
            AgentResult: Contains list of DevRev work items
        """
        start_time = time.time()
        
        try:
            if not self._devrev_source:
                self._devrev_source = get_devrev_data_source()
            
            # Extract all entities from query
            ticket_ids = self._extract_ticket_ids(query)
            assignee = self._extract_assignee(query)
            status = self._extract_status(query)
            priority = self._extract_priority(query)
            work_type = self._extract_type(query)
            board_name = self._extract_board_name(query)
            part_name = self._extract_part_name(query)
            
            logger.info(
                f"Query analysis: ticket_ids={ticket_ids}, assignee={assignee}, "
                f"status={status}, priority={priority}, type={work_type}, "
                f"board={board_name}, part={part_name}"
            )
            
            # Route 1: Specific ticket ID query - get ticket details
            if ticket_ids and self._is_detail_query(query, ticket_ids):
                return await self._fetch_ticket_details(ticket_ids, start_time)
            
            # Route 2: Board listing query
            if self._is_board_query(query) and not part_name and not assignee:
                return await self._fetch_boards(query, board_name, start_time)
            
            # Route 3: Part listing query (when asking about parts/pods, not tickets in a part)
            if self._is_part_query(query) and not part_name and not assignee:
                return await self._fetch_parts(query, start_time)
            
            # Route 4: Tickets for a specific part/pod
            if part_name:
                return await self._fetch_works_for_part(
                    query, part_name, status, priority, work_type, start_time
                )
            
            # Build params for DevRev search
            search_query = query
            if search_terms:
                search_query = " ".join(search_terms)
            
            params: dict[str, Any] = {
                'query': search_query,
                'limit': self._max_results,
            }
            
            # Route 2: Assignee filter (existing logic - keep as-is)
            if assignee:
                if assignee == "self":
                    try:
                        result = await self._devrev_source.mcp_client.call_tool("get_self", {})
                        if result and result.get("dev_user", {}).get("id"):
                            params['owned_by'] = result["dev_user"]["id"]
                            logger.info(f"Filtering by current user: {result['dev_user'].get('display_name')}")
                    except Exception as e:
                        logger.warning(f"Could not get current user: {e}")
                else:
                    params['assignee'] = assignee
            
            # Route 3: Add status filter if extracted
            if status:
                params['status'] = status
            
            # Route 4: Add priority filter if extracted
            if priority:
                params['priority'] = priority
            
            # Route 5: Add type filter
            if work_type:
                params['type'] = work_type
            
            # Route 6: If this is a search query, use semantic search
            if self._is_search_query(query) and not assignee:
                params['use_search'] = True
            
            # Perform the fetch
            results = await self._devrev_source.fetch_data(params)
            
            # Check for errors
            if 'error' in results:
                execution_time = (time.time() - start_time) * 1000
                logger.error(f"DevRev search error: {results['error']}")
                return AgentResult(
                    agent_name=self.agent_name,
                    agent_type=self.agent_type,
                    success=False,
                    error_message=f"DevRev search failed: {results['error']}",
                    execution_time_ms=execution_time
                )
            
            # Extract metadata
            metadata = results.get('_metadata', {})
            
            # Get work items
            work_items = results.get('work_items', [])
            
            # Post-filter by type if we have an assignee (API doesn't combine owned_by + type well)
            if work_type and work_items and assignee:
                work_items = self._filter_by_type(work_items, work_type)
            
            # Post-filter by status if needed (since API might not support all status names)
            if status and work_items:
                work_items = self._filter_by_status(work_items, status)
            
            # Post-filter by priority if needed
            if priority and work_items:
                work_items = self._filter_by_priority(work_items, priority)
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(
                f"DevRev agent fetched {len(work_items)} work items "
                f"(took {execution_time:.1f}ms)"
            )
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=work_items,
                metadata={
                    "search_query": search_query,
                    "total_items": len(work_items),
                    "assignee_filter": metadata.get('assignee_filter'),
                    "owner_ids": metadata.get('owner_ids'),
                    "status_filter": status,
                    "priority_filter": priority,
                    "type_filter": work_type,
                    "processing_time": metadata.get('processing_time_seconds', 0),
                },
                execution_time_ms=execution_time,
                query_used=search_query
            )
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error in DevRev agent: {e}", exc_info=True)
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
    
    async def _fetch_ticket_details(
        self, 
        ticket_ids: List[str], 
        start_time: float
    ) -> AgentResult:
        """Fetch detailed information for specific tickets.
        
        Args:
            ticket_ids: List of ticket IDs to fetch
            start_time: Start time for execution timing
            
        Returns:
            AgentResult with ticket details
        """
        work_items = []
        errors = []
        
        for ticket_id in ticket_ids:
            try:
                work_item = await self._devrev_source.get_work_item(ticket_id)
                if work_item:
                    work_items.append(work_item)
                    logger.info(f"Fetched details for ticket: {ticket_id}")
            except Exception as e:
                logger.warning(f"Failed to fetch ticket {ticket_id}: {e}")
                errors.append(f"{ticket_id}: {str(e)}")
        
        execution_time = (time.time() - start_time) * 1000
        
        if not work_items and errors:
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Failed to fetch tickets: {', '.join(errors)}",
                execution_time_ms=execution_time
            )
        
        return AgentResult(
            agent_name=self.agent_name,
            agent_type=self.agent_type,
            success=True,
            data=work_items,
            metadata={
                "query_type": "ticket_detail",
                "ticket_ids": ticket_ids,
                "total_items": len(work_items),
                "errors": errors if errors else None,
            },
            execution_time_ms=execution_time,
            query_used=f"ticket details: {', '.join(ticket_ids)}"
        )
    
    async def _fetch_boards(
        self,
        query: str,
        board_name: Optional[str],
        start_time: float
    ) -> AgentResult:
        """Fetch boards/vistas.
        
        Args:
            query: Original query
            board_name: Optional specific board name to find
            start_time: Start time for execution timing
            
        Returns:
            AgentResult with board data
        """
        try:
            if board_name:
                # Search for specific board
                board = await self._devrev_source.search_vistas_by_name(board_name)
                if board:
                    execution_time = (time.time() - start_time) * 1000
                    return AgentResult(
                        agent_name=self.agent_name,
                        agent_type=self.agent_type,
                        success=True,
                        data=[board],
                        metadata={
                            "query_type": "board_search",
                            "board_name": board_name,
                            "total_items": 1,
                        },
                        execution_time_ms=execution_time,
                        query_used=f"board: {board_name}"
                    )
            
            # List all boards
            results = await self._devrev_source.list_vistas(limit=50)
            vistas = results.get("vistas", [])
            
            # Filter by name if provided
            if board_name:
                name_lower = board_name.lower()
                vistas = [v for v in vistas if name_lower in v.get("name", "").lower()]
            
            execution_time = (time.time() - start_time) * 1000
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=vistas,
                metadata={
                    "query_type": "board_list",
                    "total_items": len(vistas),
                    "filter": board_name,
                },
                execution_time_ms=execution_time,
                query_used=f"list boards"
            )
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Failed to fetch boards: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Failed to fetch boards: {str(e)}",
                execution_time_ms=execution_time
            )
    
    async def _fetch_parts(
        self,
        query: str,
        start_time: float
    ) -> AgentResult:
        """Fetch parts/pods.
        
        Args:
            query: Original query
            start_time: Start time for execution timing
            
        Returns:
            AgentResult with part data
        """
        try:
            results = await self._devrev_source.list_parts(limit=50)
            parts = results.get("parts", [])
            
            execution_time = (time.time() - start_time) * 1000
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=parts,
                metadata={
                    "query_type": "parts_list",
                    "total_items": len(parts),
                },
                execution_time_ms=execution_time,
                query_used="list parts"
            )
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Failed to fetch parts: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Failed to fetch parts: {str(e)}",
                execution_time_ms=execution_time
            )
    
    async def _fetch_works_for_part(
        self,
        query: str,
        part_name: str,
        status: Optional[str],
        priority: Optional[str],
        work_type: Optional[str],
        start_time: float
    ) -> AgentResult:
        """Fetch work items for a specific part/pod.
        
        Args:
            query: Original query
            part_name: Part name to search for
            status: Optional status filter
            priority: Optional priority filter
            work_type: Optional work type filter
            start_time: Start time for execution timing
            
        Returns:
            AgentResult with work items
        """
        try:
            # First, find the part by name using hybrid search
            part = await self._devrev_source.search_parts_by_name(part_name)
            
            if not part:
                execution_time = (time.time() - start_time) * 1000
                return AgentResult(
                    agent_name=self.agent_name,
                    agent_type=self.agent_type,
                    success=True,
                    data=[],
                    metadata={
                        "query_type": "part_works",
                        "part_name": part_name,
                        "error": f"Could not find part named '{part_name}'",
                        "total_items": 0,
                    },
                    execution_time_ms=execution_time,
                    query_used=f"works for part: {part_name}"
                )
            
            part_id = part.get("id")
            logger.info(f"Found part '{part_name}' with ID: {part_id}")
            
            # Fetch works for this part
            results = await self._devrev_source.get_works_for_part(part_id, limit=50)
            work_items = results.get("work_items", [])
            
            # Apply post-filters
            if work_type and work_items:
                work_items = self._filter_by_type(work_items, work_type)
            if status and work_items:
                work_items = self._filter_by_status(work_items, status)
            if priority and work_items:
                work_items = self._filter_by_priority(work_items, priority)
            
            execution_time = (time.time() - start_time) * 1000
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=work_items,
                metadata={
                    "query_type": "part_works",
                    "part_name": part_name,
                    "part_id": part_id,
                    "status_filter": status,
                    "priority_filter": priority,
                    "type_filter": work_type,
                    "total_items": len(work_items),
                },
                execution_time_ms=execution_time,
                query_used=f"works for part: {part_name}"
            )
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Failed to fetch works for part: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Failed to fetch works for part: {str(e)}",
                execution_time_ms=execution_time
            )
    
    def _filter_by_status(
        self, 
        work_items: List[dict], 
        target_status: str
    ) -> List[dict]:
        """Filter work items by status.
        
        Args:
            work_items: List of work items
            target_status: Target status to filter by
            
        Returns:
            Filtered list of work items
        """
        keywords = STATUS_KEYWORDS.get(target_status, [target_status])
        filtered = []
        
        for item in work_items:
            item_status = item.get("status", "").lower()
            if any(kw in item_status for kw in keywords):
                filtered.append(item)
        
        logger.info(f"Filtered by status '{target_status}': {len(work_items)} -> {len(filtered)}")
        return filtered if filtered else work_items  # Return original if no matches
    
    def _filter_by_priority(
        self, 
        work_items: List[dict], 
        target_priority: str
    ) -> List[dict]:
        """Filter work items by priority.
        
        Args:
            work_items: List of work items
            target_priority: Target priority to filter by
            
        Returns:
            Filtered list of work items
        """
        keywords = PRIORITY_KEYWORDS.get(target_priority, [target_priority])
        filtered = []
        
        for item in work_items:
            item_priority = item.get("priority", "").lower()
            if any(kw in item_priority for kw in keywords):
                filtered.append(item)
        
        logger.info(f"Filtered by priority '{target_priority}': {len(work_items)} -> {len(filtered)}")
        return filtered if filtered else work_items  # Return original if no matches
    
    def _filter_by_type(
        self, 
        work_items: List[dict], 
        target_type: str
    ) -> List[dict]:
        """Filter work items by type.
        
        Args:
            work_items: List of work items
            target_type: Target type to filter by (ticket, issue, task)
            
        Returns:
            Filtered list of work items
        """
        keywords = TYPE_KEYWORDS.get(target_type, [target_type])
        filtered = []
        
        for item in work_items:
            item_type = item.get("type", "").lower()
            if any(kw in item_type for kw in keywords):
                filtered.append(item)
        
        logger.info(f"Filtered by type '{target_type}': {len(work_items)} -> {len(filtered)}")
        return filtered if filtered else work_items  # Return original if no matches
    
    async def health_check(self) -> bool:
        """Check if the agent is healthy.
        
        Returns:
            bool: True if agent is operational
        """
        if not self._initialized:
            return False
        try:
            if self._devrev_source:
                health = await self._devrev_source.health_check()
                return health.get("status") == "healthy"
            return False
        except Exception:
            return False
            
    async def shutdown(self) -> None:
        """Clean up agent resources."""
        if self._devrev_source:
            await self._devrev_source.close()
        self._initialized = False
        logger.info(f"Agent {self.agent_name} shut down")

