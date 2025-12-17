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

    async def fetch_data(
        self,
        query: str,
        credentials: Credentials,
        search_terms: Optional[list[str]] = None
    ) -> AgentResult:
        """Fetch DevRev work items based on the query.
        
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
            
            # Build search query
            search_query = query
            if search_terms:
                # Use search terms for more focused search
                search_query = " ".join(search_terms)
            
            # Build params for DevRev search
            params: dict[str, Any] = {
                'query': search_query,
                'limit': self._max_results,
            }
            
            # Extract assignee from query
            assignee = self._extract_assignee(query)
            if assignee:
                if assignee == "self":
                    # Get current user's tickets - use MCP to get self
                    try:
                        result = await self._devrev_source.mcp_client.call_tool("get_self", {})
                        if result and result.get("dev_user", {}).get("id"):
                            params['owned_by'] = result["dev_user"]["id"]
                            logger.info(f"Filtering by current user: {result['dev_user'].get('display_name')}")
                    except Exception as e:
                        logger.warning(f"Could not get current user: {e}")
                else:
                    params['assignee'] = assignee
            
            # Extract filters from query if present
            query_lower = query.lower()
            
            # Check for status mentions (DevRev uses various status names)
            # Don't filter by status - let the API return all and filter in synthesis
            # Status names vary: Open, Closed, queued, completed, triage, To Do, etc.
                
            # Check for type mentions
            if "ticket" in query_lower:
                params['type'] = "ticket"
            elif "bug" in query_lower:
                params['type'] = "issue"
            elif "task" in query_lower:
                params['type'] = "task"
            
            # Perform the search
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

