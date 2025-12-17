"""DevRev data source for fetching tickets, issues, and work items.

This module provides integration with DevRev's API for:
- Searching work items (tickets, issues, bugs)
- Fetching ticket details
- Listing work items by status, assignee, etc.
"""

import logging
import time
import aiohttp
from typing import Dict, Any, List, Optional
from datetime import datetime

from config import settings
from services.data_sources.base import DataSource, GlobalSearchResult, ChannelSummary, FollowUpSuggestion

logger = logging.getLogger(__name__)


class DevRevError(Exception):
    """Custom exception for DevRev API errors."""
    
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class DevRevDataSource(DataSource):
    """DevRev data source for tickets and work items.
    
    This class provides methods to interact with the DevRev API
    for searching and fetching work items, tickets, and issues.
    
    Attributes:
        api_url: Base URL for DevRev API
        api_key: Personal Access Token for authentication
        
    Examples:
        >>> source = DevRevDataSource()
        >>> results = await source.fetch_data({'query': 'login bug'})
    """
    
    def __init__(self):
        """Initialize the DevRev data source."""
        self.api_url = settings.devrev_api_url
        self.api_key = settings.devrev_api_key
        self.max_results = settings.devrev_max_results
        self._session: Optional[aiohttp.ClientSession] = None
        self._user_cache: Dict[str, str] = {}  # name -> user_id cache
        
    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests.
        
        Returns:
            Dict with authorization and content-type headers
        """
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session.
        
        Returns:
            aiohttp.ClientSession instance
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._get_headers(),
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session
        
    async def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Make an API request to DevRev.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            data: Request body data
            
        Returns:
            Dict containing the API response
            
        Raises:
            DevRevError: If API request fails
        """
        if not self.api_key:
            raise DevRevError("DevRev API key not configured. Set DEV_REV in .env file.")
            
        url = f"{self.api_url}{endpoint}"
        session = await self._get_session()
        
        try:
            async with session.request(method, url, json=data) as response:
                response_data = await response.json()
                
                if response.status >= 400:
                    error_message = response_data.get("message", "Unknown error")
                    logger.error(f"DevRev API error: {response.status} - {error_message}")
                    raise DevRevError(error_message, response.status)
                    
                return response_data
                
        except aiohttp.ClientError as e:
            logger.error(f"DevRev connection error: {e}")
            raise DevRevError(f"Connection error: {str(e)}")
            
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
            
        # Query is optional for listing all work items
        return True
        
    async def fetch_data(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch work items from DevRev based on parameters.
        
        Args:
            params: Dictionary containing:
                - query: Search query string (optional)
                - status: Filter by status (optional)
                - assignee: Filter by assignee name (optional)
                - owned_by: Filter by owner user ID (optional)
                - type: Work item type (ticket, issue, etc.) (optional)
                - limit: Maximum results to return (optional)
                
        Returns:
            Dict containing work items and metadata
            
        Raises:
            DevRevError: If API request fails
        """
        start_time = time.time()
        
        try:
            query = params.get("query", "")
            limit = params.get("limit", self.max_results)
            work_type = params.get("type")  # ticket, issue, etc.
            status = params.get("status")
            assignee = params.get("assignee")  # Assignee name to filter by
            owned_by = params.get("owned_by")  # Direct user ID if known
            
            # Resolve assignee name to user ID if provided
            owner_ids = []
            if owned_by:
                owner_ids = [owned_by] if isinstance(owned_by, str) else owned_by
            elif assignee:
                user_id = await self._resolve_user_by_name(assignee)
                if user_id:
                    owner_ids = [user_id]
                    logger.info(f"Resolved assignee '{assignee}' to user ID: {user_id}")
            
            # Use search if query provided, otherwise list
            if query and not owner_ids:
                # Pure search without owner filter
                results = await self._search_works(query, limit, work_type, status)
            else:
                # List with filters (including owner)
                results = await self._list_works(
                    limit=limit, 
                    work_type=work_type, 
                    status=status, 
                    query=query,
                    owned_by=owner_ids if owner_ids else None
                )
                
            processing_time = time.time() - start_time
            
            # Format results
            work_items = results.get("works", [])
            formatted_items = [self._format_work_item(item) for item in work_items]
            
            return {
                "work_items": formatted_items,
                "_metadata": {
                    "total_count": len(formatted_items),
                    "query": query,
                    "assignee_filter": assignee,
                    "owner_ids": owner_ids,
                    "processing_time_seconds": processing_time,
                    "source": "devrev",
                }
            }
            
        except DevRevError as e:
            logger.error(f"DevRev fetch error: {e}")
            return {
                "error": str(e),
                "work_items": [],
                "_metadata": {
                    "processing_time_seconds": time.time() - start_time,
                    "source": "devrev",
                }
            }
        except Exception as e:
            logger.error(f"Unexpected error fetching DevRev data: {e}", exc_info=True)
            return {
                "error": f"Unexpected error: {str(e)}",
                "work_items": [],
                "_metadata": {
                    "processing_time_seconds": time.time() - start_time,
                    "source": "devrev",
                }
            }
    
    async def _resolve_user_by_name(self, name: str) -> Optional[str]:
        """Resolve a user name to their DevRev user ID.
        
        Args:
            name: User's display name (partial match supported)
            
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
                current_user = await self._make_request("GET", "/dev-users.self", None)
                if current_user and current_user.get("dev_user"):
                    current_name = current_user["dev_user"].get("display_name", "").lower()
                    current_email = current_user["dev_user"].get("email", "").lower()
                    name_parts = name_lower.split()
                    
                    # Check if searching for current user
                    if (all(part in current_name for part in name_parts) or 
                        name_lower in current_email.split('@')[0] or
                        all(part in current_email for part in name_parts)):
                        user_id = current_user["dev_user"].get("id")
                        self._user_cache[name_lower] = user_id
                        self._user_cache[current_name] = user_id
                        logger.info(f"Found current user match: {current_user['dev_user'].get('display_name')} ({user_id})")
                        return user_id
            except DevRevError:
                pass  # Continue to search other users
            
            # List dev users and filter by name (with pagination)
            all_users = []
            cursor = None
            
            for _ in range(5):  # Max 5 pages (500 users)
                params = {"limit": 100}
                if cursor:
                    params["cursor"] = cursor
                    
                result = await self._make_request("POST", "/dev-users.list", params)
                users = result.get("dev_users", [])
                all_users.extend(users)
                
                cursor = result.get("next_cursor")
                if not cursor or not users:
                    break
            
            # Search for name matches
            name_parts = name_lower.split()
            
            for user in all_users:
                display_name = user.get("display_name", "")
                display_name_lower = display_name.lower()
                email = user.get("email", "").lower()
                
                # Check for exact match
                if name_lower == display_name_lower:
                    user_id = user.get("id")
                    self._user_cache[name_lower] = user_id
                    self._user_cache[display_name_lower] = user_id
                    logger.info(f"Found DevRev user (exact match): {display_name} ({user_id})")
                    return user_id
                
                # Check if all name parts are in display name
                if all(part in display_name_lower for part in name_parts):
                    user_id = user.get("id")
                    self._user_cache[name_lower] = user_id
                    self._user_cache[display_name_lower] = user_id
                    logger.info(f"Found DevRev user (partial match): {display_name} ({user_id})")
                    return user_id
                
                # Check email prefix
                email_prefix = email.split('@')[0].replace('.', ' ')
                if name_lower in email_prefix or all(part in email_prefix for part in name_parts):
                    user_id = user.get("id")
                    self._user_cache[name_lower] = user_id
                    self._user_cache[display_name_lower] = user_id
                    logger.info(f"Found DevRev user (email match): {display_name} ({user_id})")
                    return user_id
                    
            logger.warning(f"Could not find DevRev user matching: {name}")
            return None
            
        except DevRevError as e:
            logger.warning(f"Error resolving user '{name}': {e}")
            return None
            
    async def _search_works(
        self,
        query: str,
        limit: int,
        work_type: Optional[str] = None,
        status: Optional[str] = None
    ) -> Dict[str, Any]:
        """Search for work items using DevRev's search API.
        
        Args:
            query: Search query string
            limit: Maximum results
            work_type: Filter by work type
            status: Filter by status
            
        Returns:
            Dict containing search results
        """
        # Try hybrid search first for semantic matching
        try:
            search_data = {
                "query": query,
                "limit": limit,
                "namespace": "work",
            }
            
            result = await self._make_request("POST", "/search.hybrid", search_data)
            
            # Extract works from search results
            works = []
            for item in result.get("results", []):
                if "work" in item:
                    works.append(item["work"])
                    
            return {"works": works}
            
        except DevRevError as e:
            # Fallback to works.list with filter if search fails
            logger.warning(f"Hybrid search failed, falling back to list: {e}")
            return await self._list_works(limit, work_type, status, query)
            
    async def _list_works(
        self,
        limit: int,
        work_type: Optional[str] = None,
        status: Optional[str] = None,
        query: Optional[str] = None,
        owned_by: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """List work items from DevRev.
        
        Args:
            limit: Maximum results
            work_type: Filter by work type
            status: Filter by status
            query: Optional query for filtering
            owned_by: Optional list of owner user IDs to filter by
            
        Returns:
            Dict containing work items
        """
        list_data: Dict[str, Any] = {
            "limit": limit,
        }
        
        # Add filters if provided
        if work_type:
            list_data["type"] = [work_type]
            
        if status:
            list_data["stage.name"] = [status]
            
        if owned_by:
            list_data["owned_by"] = owned_by
            
        result = await self._make_request("POST", "/works.list", list_data)
        return result
        
    async def get_work_item(self, work_id: str) -> Dict[str, Any]:
        """Get a specific work item by ID.
        
        Args:
            work_id: DevRev work item ID
            
        Returns:
            Dict containing the work item details
        """
        result = await self._make_request("POST", "/works.get", {"id": work_id})
        return self._format_work_item(result.get("work", {}))
        
    def _format_work_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Format a work item for consistent output.
        
        Args:
            item: Raw work item from API
            
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
                
            if item.get("description"):
                desc_preview = item["description"][:200]
                if len(item["description"]) > 200:
                    desc_preview += "..."
                lines.append(f"  Description: {desc_preview}")
                
            if item.get("url"):
                lines.append(f"  Link: {item['url']}")
                
            lines.append("")  # Empty line between items
            
        return "\n".join(lines)
        
    def get_form_fields(self) -> Dict[str, Any]:
        """Return form field definitions for UI configuration.
        
        Returns:
            Dict containing form field specifications
        """
        return {
            "fields": [
                {
                    "name": "query",
                    "type": "text",
                    "label": "Search Query",
                    "placeholder": "Search for tickets, issues, bugs...",
                    "required": False,
                },
                {
                    "name": "type",
                    "type": "select",
                    "label": "Work Item Type",
                    "options": [
                        {"value": "", "label": "All Types"},
                        {"value": "ticket", "label": "Tickets"},
                        {"value": "issue", "label": "Issues"},
                        {"value": "task", "label": "Tasks"},
                    ],
                    "required": False,
                },
                {
                    "name": "status",
                    "type": "select",
                    "label": "Status",
                    "options": [
                        {"value": "", "label": "All Statuses"},
                        {"value": "open", "label": "Open"},
                        {"value": "in_progress", "label": "In Progress"},
                        {"value": "resolved", "label": "Resolved"},
                        {"value": "closed", "label": "Closed"},
                    ],
                    "required": False,
                },
            ]
        }
        
    async def health_check(self) -> Dict[str, Any]:
        """Check if the DevRev connection is healthy.
        
        Returns:
            Dict with health status information
        """
        if not self.api_key:
            return {
                "status": "error",
                "message": "DevRev API key not configured"
            }
            
        try:
            # Try to get current user info to verify credentials
            await self._make_request("GET", "/dev-users.self", None)
            return {
                "status": "healthy",
                "message": "DevRev connection successful"
            }
        except DevRevError as e:
            return {
                "status": "error",
                "message": f"DevRev connection failed: {str(e)}"
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Unexpected error: {str(e)}"
            }
            
    def get_metrics(self) -> Dict[str, Any]:
        """Get metrics about the data source usage.
        
        Returns:
            Dict with usage metrics
        """
        return {
            "source": "devrev",
            "api_url": self.api_url,
            "configured": bool(self.api_key),
            "max_results": self.max_results,
        }
        
    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

