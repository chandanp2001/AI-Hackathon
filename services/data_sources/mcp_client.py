"""MCP (Model Context Protocol) client for DevRev integration.

This module provides a Python client to communicate with the DevRev MCP server
using the MCP protocol over HTTP.
"""

import logging
import json
import aiohttp
from typing import Dict, Any, Optional, List
from config import settings

logger = logging.getLogger(__name__)


class MCPClient:
    """Client for communicating with MCP servers via HTTP.
    
    The MCP server is configured to run via `mcp-remote` which exposes
    an HTTP endpoint. This client makes JSON-RPC requests to the MCP server.
    """
    
    def __init__(self, mcp_server_url: str, api_key: str):
        """Initialize the MCP client.
        
        Args:
            mcp_server_url: URL of the MCP server (e.g., https://api.devrev.ai/mcp/v1)
            api_key: DevRev Personal Access Token
        """
        self.mcp_server_url = mcp_server_url
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session
        
    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call an MCP tool by mapping to DevRev API endpoints.
        
        Since we can't directly call MCP tools from Python, we map MCP tool names
        to their equivalent DevRev REST API endpoints.
        
        Args:
            tool_name: Name of the MCP tool to call (e.g., 'list_issues')
            arguments: Arguments to pass to the tool
            
        Returns:
            Dict containing the tool's response
            
        Raises:
            Exception: If the tool call fails
        """
        # Map MCP tool names to DevRev API endpoints
        endpoint_map = {
            # Works (tickets, issues, tasks)
            "list_issues": "/works.list",
            "list_works": "/works.list",
            "get_issue": "/works.get",
            "get_ticket": "/works.get",
            "get_work": "/works.get",
            "list_tickets": "/works.list",
            # Search
            "hybrid_search": "/search.hybrid",
            # Users
            "get_self": "/dev-users.self",
            # Boards (Vistas)
            "list_vistas": "/vistas.list",
            "get_vista": "/vistas.get",
            # Parts (Components/Pods)
            "list_parts": "/parts.list",
            "get_part": "/parts.get",
        }
        
        endpoint = endpoint_map.get(tool_name)
        if not endpoint:
            raise Exception(f"Unknown MCP tool: {tool_name}. Available: {list(endpoint_map.keys())}")
        
        # Build API URL
        api_url = "https://api.devrev.ai"
        if endpoint.startswith("/"):
            url = f"{api_url}{endpoint}"
        else:
            url = f"{api_url}/{endpoint}"
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        # Transform arguments for DevRev API format
        if tool_name in ("list_issues", "list_works", "list_tickets"):
            # Convert MCP arguments to DevRev API format
            api_params = {"limit": arguments.get("limit", 50)}
            if "owned_by" in arguments:
                api_params["owned_by"] = arguments["owned_by"]
            if "type" in arguments:
                api_params["type"] = [arguments["type"]] if isinstance(arguments["type"], str) else arguments["type"]
            if "applies_to_part" in arguments:
                api_params["applies_to_part"] = arguments["applies_to_part"]
            if "stage_name" in arguments:
                api_params["stage.name"] = arguments["stage_name"]
        elif tool_name in ("get_issue", "get_ticket", "get_work", "get_vista", "get_part"):
            api_params = {"id": arguments.get("id")}
        elif tool_name == "hybrid_search":
            api_params = {
                "query": arguments.get("query", ""),
                "limit": arguments.get("limit", 50),
                "namespace": arguments.get("namespace", "work")
            }
        elif tool_name == "get_self":
            api_params = None  # GET request
        elif tool_name in ("list_vistas", "list_parts"):
            api_params = {"limit": arguments.get("limit", 100)}
            if "name" in arguments:
                api_params["name"] = arguments["name"]
        else:
            api_params = arguments
        
        try:
            session = await self._get_session()
            if tool_name == "get_self":
                # GET request for self endpoint
                async with session.get(url, headers=headers) as response:
                    if response.status >= 400:
                        error_text = await response.text()
                        logger.error(f"DevRev API call failed: {response.status} - {error_text}")
                        raise Exception(f"API error: {error_text}")
                    return await response.json()
            else:
                # POST request for other endpoints
                async with session.post(
                    url,
                    json=api_params,
                    headers=headers
                ) as response:
                    if response.status >= 400:
                        error_text = await response.text()
                        logger.error(f"DevRev API call failed: {response.status} - {error_text}")
                        raise Exception(f"API error: {error_text}")
                    
                    result = await response.json()
                    return result
                
        except aiohttp.ClientError as e:
            logger.error(f"Connection error: {e}")
            raise Exception(f"Connection error: {str(e)}")
            
    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

