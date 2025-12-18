"""DevRev data source using MCP (Model Context Protocol) server.

This module provides integration with DevRev's MCP server for:
- Searching work items (tickets, issues, bugs)
- Fetching ticket details
- Listing work items by status, assignee, etc.
"""

import logging
import time
from typing import Dict, Any, List, Optional

from config import settings
from services.data_sources.base import DataSource
from services.data_sources.mcp_client import MCPClient

logger = logging.getLogger(__name__)


class DevRevMCPError(Exception):
    """Custom exception for DevRev MCP errors."""
    
    def __init__(self, message: str):
        super().__init__(message)


class DevRevMCPDataSource(DataSource):
    """DevRev data source using MCP server.
    
    This class provides methods to interact with the DevRev MCP server
    for searching and fetching work items, tickets, and issues.
    
    Examples:
        >>> source = DevRevMCPDataSource()
        >>> results = await source.fetch_data({'query': 'login bug'})
    """
    
    def __init__(self):
        """Initialize the DevRev MCP data source."""
        self.api_key = settings.devrev_api_key
        self.max_results = settings.devrev_max_results
        # MCP server URL - using the remote MCP endpoint
        self.mcp_server_url = "https://api.devrev.ai/mcp/v1"
        self._mcp_client: Optional[MCPClient] = None
        self._user_cache: Dict[str, str] = {}  # name -> user_id cache
        
    @property
    def mcp_client(self) -> MCPClient:
        """Get or create the MCP client."""
        if self._mcp_client is None:
            self._mcp_client = MCPClient(self.mcp_server_url, self.api_key)
        return self._mcp_client
        
    async def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate input parameters for data fetching.
        
        Args:
            inputs: Dictionary of input parameters
            
        Returns:
            bool: True if inputs are valid
            
        Raises:
            ValueError: If inputs are invalid
        """
        if not self.api_key:
            raise ValueError("DevRev API key not configured")
        return True
        
    async def fetch_data(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch work items from DevRev using MCP server.
        
        Args:
            params: Dictionary containing:
                - query: Search query string (optional)
                - status: Filter by status (optional)
                - priority: Filter by priority (optional)
                - assignee: Filter by assignee name (optional)
                - owned_by: Filter by owner user ID (optional)
                - type: Work item type (ticket, issue, etc.) (optional)
                - limit: Maximum results to return (optional)
                - use_search: Force semantic search (optional)
                
        Returns:
            Dict containing work items and metadata
        """
        start_time = time.time()
        
        try:
            query = params.get("query", "")
            limit = params.get("limit", self.max_results)
            work_type = params.get("type")  # ticket, issue, etc.
            assignee = params.get("assignee")  # Assignee name to filter by
            owned_by = params.get("owned_by")  # Direct user ID if known
            status = params.get("status")  # Status filter
            priority = params.get("priority")  # Priority filter
            use_search = params.get("use_search", False)  # Force semantic search
            
            # Resolve assignee name to user ID if provided
            owner_ids = []
            if owned_by:
                owner_ids = [owned_by] if isinstance(owned_by, str) else owned_by
            elif assignee:
                user_id = await self._resolve_user_by_name(assignee)
                if user_id:
                    owner_ids = [user_id]
                    logger.info(f"Resolved assignee '{assignee}' to user ID: {user_id}")
            
            # Determine fetch strategy
            # Priority 1: Owner filter (most reliable) - DO NOT add type filter here
            #             The DevRev API returns 0 results when combining owned_by + type
            #             if user has no items of that specific type
            # Priority 2: Semantic search for keyword-based queries
            # Priority 3: List all with filters
            
            if owner_ids:
                # List issues with owner filter (existing working logic)
                # NOTE: Do NOT pass work_type here - it breaks the query when user
                # has work items but not of the specific type requested
                results = await self._list_issues_mcp(
                    owned_by=owner_ids,
                    limit=limit
                    # work_type intentionally NOT passed - see comment above
                )
            elif use_search or (query and len(query.split()) > 2):
                # Use hybrid search for descriptive queries
                results = await self._hybrid_search_mcp(
                    query=query,
                    limit=limit
                )
            elif query:
                # Try search first, fallback to list
                results = await self._hybrid_search_mcp(
                    query=query,
                    limit=limit
                )
                # If no results, try listing
                if not results.get("works"):
                    results = await self._list_issues_mcp(
                        limit=limit,
                        work_type=work_type
                    )
            else:
                # List all issues with optional filters
                results = await self._list_issues_mcp(
                    limit=limit,
                    work_type=work_type
                )
            
            # Extract works from results - DevRev API returns "works" not "issues"
            works = results.get("works", [])
            if not works and "issues" in results:
                works = results["issues"]
            
            processing_time = time.time() - start_time
            
            # Format results - DevRev API returns "works"
            formatted_items = [self._format_work_item(item) for item in works]
            
            return {
                "work_items": formatted_items,
                "_metadata": {
                    "total_count": len(formatted_items),
                    "query": query,
                    "assignee_filter": assignee,
                    "owner_ids": owner_ids,
                    "status_filter": status,
                    "priority_filter": priority,
                    "type_filter": work_type,
                    "processing_time_seconds": processing_time,
                    "source": "devrev_mcp",
                }
            }
            
        except Exception as e:
            logger.error(f"DevRev MCP fetch error: {e}", exc_info=True)
            return {
                "error": str(e),
                "work_items": [],
                "_metadata": {
                    "processing_time_seconds": time.time() - start_time,
                    "source": "devrev_mcp",
                }
            }
    
    async def _hybrid_search_mcp(
        self,
        query: str,
        limit: int = 50
    ) -> Dict[str, Any]:
        """Search for work items using MCP hybrid_search tool.
        
        Args:
            query: Search query string
            limit: Maximum results
            
        Returns:
            Dict containing search results
        """
        try:
            result = await self.mcp_client.call_tool(
                tool_name="hybrid_search",
                arguments={
                    "namespace": "work",  # Valid namespaces: work, issue, ticket, dev_user, etc.
                    "query": query,
                    "limit": limit
                }
            )
            
            # Extract works from hybrid search response
            works = []
            for item in result.get("results", []):
                if "work" in item:
                    works.append(item["work"])
                elif "issue" in item:
                    works.append(item["issue"])
                elif isinstance(item, dict) and "id" in item:
                    # Direct work item
                    works.append(item)
                    
            return {"works": works}
            
        except Exception as e:
            logger.error(f"MCP hybrid_search failed: {e}")
            # Fallback to list_issues
            return await self._list_issues_mcp(limit=limit)
            
    async def _list_issues_mcp(
        self,
        limit: int = 50,
        owned_by: Optional[List[str]] = None,
        work_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """List issues using MCP list_issues tool.
        
        Args:
            limit: Maximum results
            owned_by: Optional list of owner user IDs
            work_type: Optional work type filter (ticket, issue, task)
            
        Returns:
            Dict containing issues
        """
        try:
            arguments = {
                "limit": limit
            }
            
            if owned_by:
                arguments["owned_by"] = owned_by
            
            if work_type:
                arguments["type"] = work_type
                
            result = await self.mcp_client.call_tool(
                tool_name="list_issues",
                arguments=arguments
            )
            
            # DevRev API returns works in result.works
            works = result.get("works", [])
            return {"works": works}
            
        except Exception as e:
            logger.error(f"MCP list_issues failed: {e}")
            raise DevRevMCPError(f"Failed to list issues: {str(e)}")
            
    async def _resolve_user_by_name(self, name: str) -> Optional[str]:
        """Resolve a user name to their DevRev user ID using MCP.
        
        Args:
            name: User's display name or email
            
        Returns:
            User ID if found, None otherwise
        """
        # Check cache first
        name_lower = name.lower()
        if name_lower in self._user_cache:
            return self._user_cache[name_lower]
        
        try:
            # First, check if this might be referring to the current user
            try:
                result = await self.mcp_client.call_tool(
                    tool_name="get_self",
                    arguments={}
                )
                
                if result and result.get("dev_user"):
                    current_user = result["dev_user"]
                    current_name = current_user.get("display_name", "").lower()
                    current_email = current_user.get("email", "").lower()
                    name_parts = name_lower.split()
                    
                    # Check if searching for current user
                    if (all(part in current_name for part in name_parts) or 
                        name_lower in current_email.split('@')[0] or
                        all(part in current_email for part in name_parts)):
                        user_id = current_user.get("id")
                        self._user_cache[name_lower] = user_id
                        self._user_cache[current_name] = user_id
                        logger.info(f"Found current user match: {current_user.get('display_name')} ({user_id})")
                        return user_id
            except Exception:
                pass  # Continue to search other users
            
            # Use hybrid_search to find user by name/email
            try:
                result = await self.mcp_client.call_tool(
                    tool_name="hybrid_search",
                    arguments={
                        "namespace": "dev_user",
                        "query": name,
                        "limit": 50
                    }
                )
                
                # Find matching user
                for item in result.get("results", []):
                    user = item.get("dev_user") or item.get("user")
                    if not user:
                        continue
                        
                    display_name = user.get("display_name", "").lower()
                    email = user.get("email", "").lower()
                    name_parts = name_lower.split()
                    
                    # Check for matches
                    if (name_lower == display_name or
                        all(part in display_name for part in name_parts) or
                        name_lower in email.split('@')[0] or
                        all(part in email for part in name_parts)):
                        user_id = user.get("id")
                        self._user_cache[name_lower] = user_id
                        self._user_cache[display_name] = user_id
                        logger.info(f"Found DevRev user: {user.get('display_name')} ({user_id})")
                        return user_id
                        
            except Exception as e:
                logger.warning(f"Error searching for user '{name}': {e}")
                
            logger.warning(f"Could not find DevRev user matching: {name}")
            return None
            
        except Exception as e:
            logger.warning(f"Error resolving user '{name}': {e}")
            return None
            
    async def get_work_item(self, work_id: str) -> Dict[str, Any]:
        """Get a specific work item by ID using MCP.
        
        Args:
            work_id: DevRev work item ID or display ID (e.g., TKT-123, ISS-456)
            
        Returns:
            Dict containing the work item details
        """
        try:
            result = await self.mcp_client.call_tool(
                tool_name="get_issue",
                arguments={"id": work_id}
            )
            
            # DevRev API returns work item in "work" key
            work = result.get("work", result.get("issue", result.get("ticket", {})))
            
            if not work:
                logger.warning(f"No work item found in response for {work_id}: {result.keys()}")
                # If result has expected fields directly, use it
                if "id" in result or "display_id" in result:
                    work = result
            
            formatted = self._format_work_item(work)
            logger.info(f"Fetched work item {work_id}: {formatted.get('display_id')} - {formatted.get('title')[:50]}")
            return formatted
            
        except Exception as e:
            logger.error(f"Failed to get work item {work_id}: {e}")
            # Try get_ticket as fallback
            try:
                result = await self.mcp_client.call_tool(
                    tool_name="get_ticket",
                    arguments={"id": work_id}
                )
                work = result.get("work", result.get("ticket", {}))
                return self._format_work_item(work)
            except Exception:
                raise DevRevMCPError(f"Failed to get work item: {str(e)}")
        
    def _format_work_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Format a work item for consistent output.
        
        Args:
            item: Raw work item from MCP API
            
        Returns:
            Formatted work item dict
        """
        # Extract common fields
        display_id = item.get("display_id", "")
        title = item.get("title", "No title")
        body = item.get("body", "")
        
        # Get stage/status info
        stage = item.get("stage", {})
        stage_name = stage.get("name", "unknown") if isinstance(stage, dict) else str(stage)
        
        # Get priority
        priority = item.get("priority", "unknown")
        if isinstance(priority, dict):
            priority = priority.get("name", "unknown")
        elif isinstance(priority, str):
            priority = priority.lower()
            
        # Get assignees
        owned_by = item.get("owned_by", [])
        assignees = []
        for owner in owned_by:
            if isinstance(owner, dict):
                assignees.append(owner.get("display_name", owner.get("id", "")))
            else:
                assignees.append(str(owner))
                
        # Get type
        work_type = item.get("type", "work")
        
        # Get timestamps
        created_date = item.get("created_date", "")
        modified_date = item.get("modified_date", "")
        
        # Get part/component info
        applies_to_part = item.get("applies_to_part", {})
        part_name = ""
        if isinstance(applies_to_part, dict):
            part_name = applies_to_part.get("display_name", applies_to_part.get("name", ""))
            
        # Get tags
        tags = item.get("tags", [])
        tag_names = []
        for tag in tags:
            if isinstance(tag, dict):
                tag_names.append(tag.get("name", ""))
            else:
                tag_names.append(str(tag))
                
        return {
            "id": item.get("id", ""),
            "display_id": display_id,
            "title": title,
            "description": body[:500] if body else "",  # Truncate long descriptions
            "type": work_type,
            "status": stage_name,
            "priority": priority,
            "assignees": assignees,
            "part": part_name,
            "tags": tag_names,
            "created_at": created_date,
            "updated_at": modified_date,
            "url": f"https://app.devrev.ai/works/{display_id}" if display_id else None,
        }
        
    def format_for_llm(self, data: Dict[str, Any]) -> str:
        """Format the fetched data for LLM consumption.
        
        Args:
            data: The raw data fetched from DevRev
            
        Returns:
            str: Formatted string suitable for LLM processing
        """
        work_items = data.get("work_items", [])
        
        if not work_items:
            return "No work items found in DevRev."
            
        lines = [f"Found {len(work_items)} work items in DevRev:\n"]
        
        for item in work_items:
            lines.append(f"- [{item.get('display_id', 'N/A')}] {item.get('title', 'No title')}")
            lines.append(f"  Type: {item.get('type', 'unknown')} | Status: {item.get('status', 'unknown')} | Priority: {item.get('priority', 'unknown')}")
            
            if item.get("assignees"):
                lines.append(f"  Assigned to: {', '.join(item['assignees'])}")
                
            if item.get("part"):
                lines.append(f"  Part: {item['part']}")
                
            if item.get("description"):
                desc = item["description"][:200]
                lines.append(f"  Description: {desc}...")
                
            lines.append("")
            
        return "\n".join(lines)
    
    # =========================================================================
    # BOARDS (VISTAS) METHODS
    # =========================================================================
    
    async def list_vistas(self, limit: int = 100) -> Dict[str, Any]:
        """List all boards/vistas.
        
        Args:
            limit: Maximum number of vistas to return
            
        Returns:
            Dict containing vistas list and metadata
        """
        start_time = time.time()
        
        try:
            result = await self.mcp_client.call_tool(
                tool_name="list_vistas",
                arguments={"limit": limit}
            )
            
            vistas = result.get("vistas", [])
            formatted = [self._format_vista(v) for v in vistas]
            
            return {
                "vistas": formatted,
                "_metadata": {
                    "total_count": len(formatted),
                    "processing_time_seconds": time.time() - start_time,
                    "source": "devrev_mcp",
                }
            }
        except Exception as e:
            logger.error(f"Failed to list vistas: {e}")
            return {"error": str(e), "vistas": []}
    
    async def search_vistas_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Search for a vista by name.
        
        Args:
            name: Name to search for (partial match)
            
        Returns:
            Vista dict if found, None otherwise
        """
        try:
            result = await self.list_vistas(limit=100)
            vistas = result.get("vistas", [])
            
            name_lower = name.lower()
            for vista in vistas:
                if name_lower in vista.get("name", "").lower():
                    return vista
            return None
        except Exception as e:
            logger.error(f"Failed to search vistas: {e}")
            return None
    
    def _format_vista(self, vista: Dict[str, Any]) -> Dict[str, Any]:
        """Format a vista for consistent output."""
        return {
            "id": vista.get("id", ""),
            "display_id": vista.get("display_id", ""),
            "name": vista.get("name", "Unnamed Board"),
            "type": vista.get("type", "unknown"),
            "is_default": vista.get("is_default", False),
            "created_at": vista.get("created_date", ""),
            "modified_at": vista.get("modified_date", ""),
        }
    
    # =========================================================================
    # PARTS (PODS/COMPONENTS) METHODS
    # =========================================================================
    
    async def list_parts(self, limit: int = 100) -> Dict[str, Any]:
        """List all parts/pods.
        
        Args:
            limit: Maximum number of parts to return
            
        Returns:
            Dict containing parts list and metadata
        """
        start_time = time.time()
        
        try:
            result = await self.mcp_client.call_tool(
                tool_name="list_parts",
                arguments={"limit": limit}
            )
            
            parts = result.get("parts", [])
            formatted = [self._format_part(p) for p in parts]
            
            return {
                "parts": formatted,
                "_metadata": {
                    "total_count": len(formatted),
                    "processing_time_seconds": time.time() - start_time,
                    "source": "devrev_mcp",
                }
            }
        except Exception as e:
            logger.error(f"Failed to list parts: {e}")
            return {"error": str(e), "parts": []}
    
    async def search_parts_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Search for a part by name using hybrid search.
        
        Args:
            name: Name to search for
            
        Returns:
            Part dict if found, None otherwise
        """
        try:
            result = await self.mcp_client.call_tool(
                tool_name="hybrid_search",
                arguments={
                    "query": name,
                    "namespace": "part",
                    "limit": 10
                }
            )
            
            for item in result.get("results", []):
                part = item.get("part", item)
                if name.lower() in part.get("name", "").lower():
                    return self._format_part(part)
            
            return None
        except Exception as e:
            logger.error(f"Failed to search parts: {e}")
            return None
    
    async def get_works_for_part(
        self, 
        part_id: str, 
        limit: int = 50
    ) -> Dict[str, Any]:
        """Get work items for a specific part.
        
        Args:
            part_id: The part's ID
            limit: Maximum results
            
        Returns:
            Dict containing work items
        """
        start_time = time.time()
        
        try:
            result = await self.mcp_client.call_tool(
                tool_name="list_works",
                arguments={
                    "limit": limit,
                    "applies_to_part": [part_id]
                }
            )
            
            works = result.get("works", [])
            formatted = [self._format_work_item(w) for w in works]
            
            return {
                "work_items": formatted,
                "_metadata": {
                    "total_count": len(formatted),
                    "part_id": part_id,
                    "processing_time_seconds": time.time() - start_time,
                    "source": "devrev_mcp",
                }
            }
        except Exception as e:
            logger.error(f"Failed to get works for part: {e}")
            return {"error": str(e), "work_items": []}
    
    def _format_part(self, part: Dict[str, Any]) -> Dict[str, Any]:
        """Format a part for consistent output."""
        owned_by = part.get("owned_by", [])
        owners = []
        for owner in owned_by:
            if isinstance(owner, dict):
                owners.append(owner.get("display_name", owner.get("id", "")))
            else:
                owners.append(str(owner))
        
        return {
            "id": part.get("id", ""),
            "display_id": part.get("display_id", ""),
            "name": part.get("name", "Unnamed Part"),
            "type": part.get("type", "unknown"),
            "description": part.get("description", ""),
            "owners": owners,
            "created_at": part.get("created_date", ""),
            "modified_at": part.get("modified_date", ""),
        }
        
    async def health_check(self) -> Dict[str, Any]:
        """Check if the DevRev MCP data source is healthy.
        
        Returns:
            Dict with status and message
        """
        try:
            # Try to get self to verify connection
            result = await self.mcp_client.call_tool(
                tool_name="get_self",
                arguments={}
            )
            
            if result and result.get("dev_user"):
                return {
                    "status": "healthy",
                    "message": "DevRev MCP connection successful",
                    "user": result["dev_user"].get("display_name", "Unknown")
                }
            else:
                return {
                    "status": "unhealthy",
                    "message": "DevRev MCP connection failed - no user data"
                }
                
        except Exception as e:
            return {
                "status": "unhealthy",
                "message": f"DevRev MCP connection failed: {str(e)}"
            }
            
    async def close(self):
        """Close the MCP client connection."""
        if self._mcp_client:
            await self._mcp_client.close()
            
    def get_form_fields(self) -> Dict[str, Any]:
        """Get form fields for UI configuration.
        
        Returns:
            Dict containing form field definitions
        """
        return {
            "fields": [
                {
                    "name": "query",
                    "label": "Search Query",
                    "type": "text",
                    "required": False,
                    "placeholder": "Enter search terms..."
                },
                {
                    "name": "assignee",
                    "label": "Assignee",
                    "type": "text",
                    "required": False,
                    "placeholder": "Email or name"
                },
                {
                    "name": "limit",
                    "label": "Max Results",
                    "type": "number",
                    "required": False,
                    "default": 50
                }
            ]
        }

