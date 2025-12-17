"""DevRev MCP (Model Context Protocol) client for AI agent integration.

This module provides an MCP-based interface to DevRev's API, offering:
- Standardized tool interface for AI agents
- Better structured responses
- Tool discovery and execution
- Fallback to REST API when MCP is unavailable
"""

import logging
import aiohttp
import json
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum

from config import settings

logger = logging.getLogger(__name__)


class MCPToolType(Enum):
    """Available MCP tools from DevRev."""
    SEARCH_WORKS = "search_works"
    GET_WORK = "get_work"
    LIST_WORKS = "list_works"
    SEARCH_CONVERSATIONS = "search_conversations"
    SEARCH_ARTICLES = "search_articles"
    LIST_PARTS = "list_parts"
    LIST_ACCOUNTS = "list_accounts"
    GET_USER = "get_user"
    UNIVERSAL_SEARCH = "universal_search"


@dataclass
class MCPToolResult:
    """Result from an MCP tool execution."""
    success: bool
    data: Any = None
    error: Optional[str] = None
    tool_name: str = ""
    execution_time_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPTool:
    """Definition of an MCP tool."""
    name: str
    description: str
    parameters: Dict[str, Any]
    required_params: List[str] = field(default_factory=list)


class DevRevMCPClient:
    """DevRev MCP client for Model Context Protocol integration.
    
    This client connects to DevRev's MCP server to provide
    AI-optimized access to DevRev data.
    
    Attributes:
        mcp_url: MCP server URL (https://api.devrev.ai/mcp/v1)
        api_key: DevRev PAT for authentication
        
    Examples:
        >>> client = DevRevMCPClient()
        >>> await client.initialize()
        >>> result = await client.execute_tool("search_works", {"query": "login bug"})
    """
    
    def __init__(self):
        """Initialize the MCP client."""
        self.mcp_url = settings.devrev_mcp_url
        self.api_key = settings.devrev_api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._tools: Dict[str, MCPTool] = {}
        self._initialized = False
        self._available = False
        
    def _get_headers(self) -> Dict[str, str]:
        """Get headers for MCP requests.
        
        Returns:
            Dict with authorization headers for MCP server
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
        
    async def initialize(self) -> bool:
        """Initialize the MCP client and discover available tools.
        
        Returns:
            bool: True if initialization successful
        """
        if not self.api_key:
            logger.warning("DevRev API key not configured - MCP client unavailable")
            self._available = False
            return False
            
        try:
            # Try to discover tools from MCP server
            tools = await self._discover_tools()
            if tools:
                self._tools = {t.name: t for t in tools}
                self._available = True
                self._initialized = True
                logger.info(f"DevRev MCP client initialized with {len(tools)} tools")
                return True
            else:
                # Register default tools if discovery fails
                self._register_default_tools()
                self._available = True
                self._initialized = True
                logger.info("DevRev MCP client initialized with default tools")
                return True
                
        except Exception as e:
            logger.warning(f"MCP initialization failed, will use REST fallback: {e}")
            self._available = False
            return False
            
    async def _discover_tools(self) -> List[MCPTool]:
        """Discover available tools from the MCP server.
        
        Returns:
            List of available MCP tools
        """
        try:
            session = await self._get_session()
            
            # MCP tool discovery endpoint
            async with session.post(
                f"{self.mcp_url}/tools/list",
                json={}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    tools = []
                    for tool_def in data.get("tools", []):
                        tools.append(MCPTool(
                            name=tool_def.get("name", ""),
                            description=tool_def.get("description", ""),
                            parameters=tool_def.get("inputSchema", {}).get("properties", {}),
                            required_params=tool_def.get("inputSchema", {}).get("required", [])
                        ))
                    return tools
                else:
                    logger.warning(f"MCP tool discovery returned {response.status}")
                    return []
                    
        except Exception as e:
            logger.debug(f"MCP tool discovery failed: {e}")
            return []
            
    def _register_default_tools(self) -> None:
        """Register default MCP tools based on DevRev API capabilities."""
        default_tools = [
            MCPTool(
                name="search_works",
                description="Search for work items (tickets, issues, tasks) in DevRev",
                parameters={
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results"},
                    "type": {"type": "string", "description": "Work type filter"},
                    "status": {"type": "string", "description": "Status filter"},
                },
                required_params=["query"]
            ),
            MCPTool(
                name="get_work",
                description="Get a specific work item by ID",
                parameters={
                    "id": {"type": "string", "description": "Work item ID (e.g., ISS-1234)"},
                },
                required_params=["id"]
            ),
            MCPTool(
                name="list_works",
                description="List work items with filters",
                parameters={
                    "owned_by": {"type": "array", "description": "Owner user IDs"},
                    "applies_to_part": {"type": "array", "description": "Part IDs"},
                    "stage": {"type": "string", "description": "Stage filter"},
                    "type": {"type": "array", "description": "Work types"},
                    "limit": {"type": "integer", "description": "Max results"},
                },
                required_params=[]
            ),
            MCPTool(
                name="search_conversations",
                description="Search customer conversations",
                parameters={
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results"},
                },
                required_params=["query"]
            ),
            MCPTool(
                name="search_articles",
                description="Search knowledge base articles",
                parameters={
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results"},
                },
                required_params=[]
            ),
            MCPTool(
                name="list_parts",
                description="List product parts/components",
                parameters={
                    "limit": {"type": "integer", "description": "Max results"},
                },
                required_params=[]
            ),
            MCPTool(
                name="universal_search",
                description="Search across all DevRev data types",
                parameters={
                    "query": {"type": "string", "description": "Search query"},
                    "namespaces": {"type": "array", "description": "Namespaces to search"},
                    "limit": {"type": "integer", "description": "Max results per namespace"},
                },
                required_params=["query"]
            ),
        ]
        
        self._tools = {t.name: t for t in default_tools}
        
    async def execute_tool(
        self,
        tool_name: str,
        parameters: Dict[str, Any]
    ) -> MCPToolResult:
        """Execute an MCP tool.
        
        Args:
            tool_name: Name of the tool to execute
            parameters: Tool parameters
            
        Returns:
            MCPToolResult with execution results
        """
        import time
        start_time = time.time()
        
        if not self._initialized:
            await self.initialize()
            
        if not self._available:
            return MCPToolResult(
                success=False,
                error="MCP client not available",
                tool_name=tool_name
            )
            
        tool = self._tools.get(tool_name)
        if not tool:
            return MCPToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}",
                tool_name=tool_name
            )
            
        # Validate required parameters
        for param in tool.required_params:
            if param not in parameters:
                return MCPToolResult(
                    success=False,
                    error=f"Missing required parameter: {param}",
                    tool_name=tool_name
                )
                
        try:
            session = await self._get_session()
            
            # Execute tool via MCP
            async with session.post(
                f"{self.mcp_url}/tools/call",
                json={
                    "name": tool_name,
                    "arguments": parameters
                }
            ) as response:
                execution_time = (time.time() - start_time) * 1000
                
                if response.status == 200:
                    data = await response.json()
                    
                    # MCP returns content array
                    content = data.get("content", [])
                    result_data = None
                    
                    for item in content:
                        if item.get("type") == "text":
                            # Parse JSON from text content
                            try:
                                result_data = json.loads(item.get("text", "{}"))
                            except json.JSONDecodeError:
                                result_data = {"text": item.get("text")}
                        elif item.get("type") == "resource":
                            result_data = item.get("resource", {})
                            
                    return MCPToolResult(
                        success=True,
                        data=result_data,
                        tool_name=tool_name,
                        execution_time_ms=execution_time,
                        metadata={"mcp_response": data}
                    )
                else:
                    error_data = await response.json()
                    return MCPToolResult(
                        success=False,
                        error=error_data.get("message", f"MCP error: {response.status}"),
                        tool_name=tool_name,
                        execution_time_ms=execution_time
                    )
                    
        except aiohttp.ClientError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"MCP connection error: {e}")
            return MCPToolResult(
                success=False,
                error=f"Connection error: {str(e)}",
                tool_name=tool_name,
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"MCP execution error: {e}")
            return MCPToolResult(
                success=False,
                error=f"Execution error: {str(e)}",
                tool_name=tool_name,
                execution_time_ms=execution_time
            )
            
    # =========================================================================
    # CONVENIENCE METHODS FOR COMMON OPERATIONS
    # =========================================================================
    
    async def search_works(
        self,
        query: str,
        limit: int = 20,
        work_type: Optional[str] = None,
        status: Optional[str] = None
    ) -> MCPToolResult:
        """Search for work items.
        
        Args:
            query: Search query
            limit: Maximum results
            work_type: Optional type filter
            status: Optional status filter
            
        Returns:
            MCPToolResult with work items
        """
        params = {"query": query, "limit": limit}
        if work_type:
            params["type"] = work_type
        if status:
            params["status"] = status
            
        return await self.execute_tool("search_works", params)
        
    async def get_work(self, work_id: str) -> MCPToolResult:
        """Get a specific work item by ID.
        
        Args:
            work_id: Work item ID (e.g., ISS-1234, TKT-567)
            
        Returns:
            MCPToolResult with work item details
        """
        return await self.execute_tool("get_work", {"id": work_id})
        
    async def list_works(
        self,
        owned_by: Optional[List[str]] = None,
        part_ids: Optional[List[str]] = None,
        status: Optional[str] = None,
        work_types: Optional[List[str]] = None,
        limit: int = 50
    ) -> MCPToolResult:
        """List work items with filters.
        
        Args:
            owned_by: Owner user IDs
            part_ids: Part IDs to filter by
            status: Status filter
            work_types: Work type filters
            limit: Maximum results
            
        Returns:
            MCPToolResult with work items
        """
        params: Dict[str, Any] = {"limit": limit}
        if owned_by:
            params["owned_by"] = owned_by
        if part_ids:
            params["applies_to_part"] = part_ids
        if status:
            params["stage"] = status
        if work_types:
            params["type"] = work_types
            
        return await self.execute_tool("list_works", params)
        
    async def search_conversations(
        self,
        query: str,
        limit: int = 20
    ) -> MCPToolResult:
        """Search customer conversations.
        
        Args:
            query: Search query
            limit: Maximum results
            
        Returns:
            MCPToolResult with conversations
        """
        return await self.execute_tool("search_conversations", {
            "query": query,
            "limit": limit
        })
        
    async def search_articles(
        self,
        query: Optional[str] = None,
        limit: int = 20
    ) -> MCPToolResult:
        """Search knowledge base articles.
        
        Args:
            query: Optional search query
            limit: Maximum results
            
        Returns:
            MCPToolResult with articles
        """
        params = {"limit": limit}
        if query:
            params["query"] = query
        return await self.execute_tool("search_articles", params)
        
    async def list_parts(self, limit: int = 50) -> MCPToolResult:
        """List product parts/components.
        
        Args:
            limit: Maximum results
            
        Returns:
            MCPToolResult with parts
        """
        return await self.execute_tool("list_parts", {"limit": limit})
        
    async def universal_search(
        self,
        query: str,
        namespaces: Optional[List[str]] = None,
        limit: int = 20
    ) -> MCPToolResult:
        """Search across all DevRev data types.
        
        Args:
            query: Search query
            namespaces: Optional list of namespaces to search
            limit: Maximum results per namespace
            
        Returns:
            MCPToolResult with combined results
        """
        params = {"query": query, "limit": limit}
        if namespaces:
            params["namespaces"] = namespaces
        else:
            params["namespaces"] = ["work", "conversation", "article", "part", "account"]
            
        return await self.execute_tool("universal_search", params)
        
    def get_available_tools(self) -> List[str]:
        """Get list of available tool names.
        
        Returns:
            List of tool names
        """
        return list(self._tools.keys())
        
    def get_tool_info(self, tool_name: str) -> Optional[MCPTool]:
        """Get information about a specific tool.
        
        Args:
            tool_name: Name of the tool
            
        Returns:
            MCPTool definition or None
        """
        return self._tools.get(tool_name)
        
    def is_available(self) -> bool:
        """Check if MCP client is available.
        
        Returns:
            bool: True if MCP is available
        """
        return self._available
        
    async def health_check(self) -> Dict[str, Any]:
        """Check MCP client health.
        
        Returns:
            Dict with health status
        """
        if not self._initialized:
            await self.initialize()
            
        return {
            "status": "healthy" if self._available else "unavailable",
            "initialized": self._initialized,
            "available": self._available,
            "tool_count": len(self._tools),
            "mcp_url": self.mcp_url
        }
        
    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            
        self._initialized = False
        self._available = False


# Singleton instance
_mcp_client: Optional[DevRevMCPClient] = None


async def get_mcp_client() -> DevRevMCPClient:
    """Get or create the MCP client singleton.
    
    Returns:
        DevRevMCPClient instance
    """
    global _mcp_client
    
    if _mcp_client is None:
        _mcp_client = DevRevMCPClient()
        await _mcp_client.initialize()
        
    return _mcp_client

