"""Slack thread summarization service.

This module provides utilities for:
- Parsing Slack thread URLs
- Fetching thread messages
- Summarizing thread content using LLM

URL Format: https://{workspace}.slack.com/archives/{channel_id}/p{timestamp}
"""

import logging
import re
from typing import Optional, Any
from datetime import datetime
from urllib.parse import urlparse

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import settings

logger = logging.getLogger(__name__)

# Initialize Slack clients
try:
    SLACK_BOT_TOKEN = settings.slack_bot_token
    SLACK_USER_TOKEN = settings.slack_user_token
except ImportError:
    import os
    SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
    SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")


class SlackThreadParser:
    """Parser for Slack thread URLs.
    
    Parses URLs like:
    - https://razorpay.slack.com/archives/C07Q18XM674/p1759741808347769
    - https://workspace.slack.com/archives/C12345678/p1234567890123456
    
    Args:
        url: Slack thread URL to parse
        
    Examples:
        >>> parser = SlackThreadParser("https://razorpay.slack.com/archives/C07Q18XM674/p1759741808347769")
        >>> parser.channel_id
        'C07Q18XM674'
        >>> parser.thread_ts
        '1759741808.347769'
    """
    
    # Pattern: https://{workspace}.slack.com/archives/{channel_id}/p{timestamp}
    URL_PATTERN = re.compile(
        r"https?://(?P<workspace>[\w-]+)\.slack\.com/archives/"
        r"(?P<channel_id>[A-Z0-9]+)/p(?P<timestamp>\d+)"
    )
    
    def __init__(self, url: str):
        self.url = url
        self.workspace: Optional[str] = None
        self.channel_id: Optional[str] = None
        self.thread_ts: Optional[str] = None
        self._parse()
        
    def _parse(self) -> None:
        """Parse the URL and extract components."""
        match = self.URL_PATTERN.match(self.url)
        if match:
            self.workspace = match.group("workspace")
            self.channel_id = match.group("channel_id")
            # Convert timestamp to Slack's ts format (seconds.microseconds)
            raw_ts = match.group("timestamp")
            # Slack timestamps are typically in format: 1234567890.123456
            # The URL format removes the dot, so we need to add it back
            if len(raw_ts) > 10:
                self.thread_ts = f"{raw_ts[:10]}.{raw_ts[10:]}"
            else:
                self.thread_ts = raw_ts
                
    @property
    def is_valid(self) -> bool:
        """Check if the URL was parsed successfully."""
        return all([self.workspace, self.channel_id, self.thread_ts])
        
    def __repr__(self) -> str:
        return (
            f"SlackThreadParser(workspace={self.workspace}, "
            f"channel_id={self.channel_id}, thread_ts={self.thread_ts})"
        )


class SlackThreadService:
    """Service for fetching and summarizing Slack threads.
    
    This service provides methods to:
    - Fetch all messages in a thread
    - Get thread metadata (channel name, participants)
    - Summarize thread content using LLM
    
    Args:
        llm_service: Optional LLM service for summarization
        
    Examples:
        >>> service = SlackThreadService(llm_service)
        >>> result = await service.summarize_thread_url(
        ...     "https://razorpay.slack.com/archives/C07Q18XM674/p1759741808347769"
        ... )
    """
    
    def __init__(self, llm_service: Optional[Any] = None):
        self._llm_service = llm_service
        
        # Initialize Slack clients - user token preferred for broader access
        self._user_client = None
        self._bot_client = None
        
        if SLACK_USER_TOKEN:
            clean_token = SLACK_USER_TOKEN.strip().strip('"\'')
            self._user_client = WebClient(token=clean_token)
            logger.info("Slack user token initialized for thread service")
            
        if SLACK_BOT_TOKEN:
            clean_token = SLACK_BOT_TOKEN.strip().strip('"\'')
            self._bot_client = WebClient(token=clean_token)
            logger.info("Slack bot token initialized for thread service")
            
        # Primary client is user token (has broader access), fallback to bot
        self._client = self._user_client or self._bot_client
        
        if not self._client:
            logger.warning("No Slack token available for thread service")
            
        # Cache for channel info
        self._channel_cache: dict[str, dict] = {}
        
    def set_llm_service(self, llm_service: Any) -> None:
        """Set the LLM service for summarization.
        
        Args:
            llm_service: OpenAI service instance
        """
        self._llm_service = llm_service
        
    async def fetch_thread_messages(
        self,
        channel_id: str,
        thread_ts: str,
        limit: int = 100
    ) -> dict[str, Any]:
        """Fetch all messages in a thread.
        
        Args:
            channel_id: Slack channel ID
            thread_ts: Thread timestamp (parent message ts)
            limit: Maximum messages to fetch
            
        Returns:
            dict: Contains messages, metadata, and any errors
            
        Raises:
            SlackApiError: If the API call fails
        """
        if not self._client and not self._user_client and not self._bot_client:
            return {
                "success": False,
                "error": "Slack client not initialized - missing token"
            }
            
        # Try clients in order: user token first (broader access), then bot token
        clients_to_try = []
        if self._user_client:
            clients_to_try.append(("user", self._user_client))
        if self._bot_client:
            clients_to_try.append(("bot", self._bot_client))
            
        response = None
        last_error = None
        successful_client = None
        
        for client_type, client in clients_to_try:
            try:
                logger.info(f"Trying {client_type} client for thread fetch")
                # Fetch thread replies
                response = client.conversations_replies(
                    channel=channel_id,
                    ts=thread_ts,
                    limit=limit
                )
                
                if response.get("ok"):
                    logger.info(f"Successfully fetched thread with {client_type} client")
                    successful_client = client
                    break
                else:
                    last_error = response.get("error", "Unknown error")
                    logger.warning(f"{client_type} client failed: {last_error}")
                    response = None
                    
            except SlackApiError as e:
                last_error = str(e.response.get("error", str(e)))
                logger.warning(f"{client_type} client error: {last_error}")
                response = None
                continue
        
        if not response or not response.get("ok"):
            return {
                "success": False,
                "error": last_error or "Failed to fetch thread with all available clients"
            }
                
        messages = response.get("messages", [])
        
        try:
            # Get channel info for context - use the client that worked
            channel_info = await self._get_channel_info(channel_id, successful_client)
            
            # Get unique users in thread - use the client that worked
            user_ids = set(msg.get("user", "") for msg in messages if msg.get("user"))
            users = await self._get_user_info(list(user_ids), successful_client)
            
            # Format messages with user names
            formatted_messages = []
            for msg in messages:
                user_id = msg.get("user", "")
                user_name = users.get(user_id, {}).get("name", user_id)
                
                # Parse timestamp
                ts = msg.get("ts", "")
                try:
                    timestamp = datetime.fromtimestamp(float(ts))
                    formatted_time = timestamp.strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    formatted_time = ts
                    
                formatted_messages.append({
                    "user": user_name,
                    "user_id": user_id,
                    "text": msg.get("text", ""),
                    "timestamp": formatted_time,
                    "ts": ts,
                    "is_parent": ts == thread_ts,
                    "reactions": msg.get("reactions", []),
                    "attachments": len(msg.get("attachments", [])),
                    "files": len(msg.get("files", []))
                })
                
            return {
                "success": True,
                "channel_id": channel_id,
                "channel_name": channel_info.get("name", channel_id),
                "thread_ts": thread_ts,
                "message_count": len(formatted_messages),
                "messages": formatted_messages,
                "participants": list(users.values()),
                "has_more": response.get("has_more", False)
            }
            
        except Exception as e:
            logger.error(f"Error processing thread data: {e}")
            return {
                "success": False,
                "error": str(e)
            }
            
    async def _get_channel_info(self, channel_id: str, client: Optional[WebClient] = None) -> dict:
        """Get channel information with caching.
        
        Args:
            channel_id: Slack channel ID
            client: Optional specific client to use
            
        Returns:
            dict: Channel information
        """
        if channel_id in self._channel_cache:
            return self._channel_cache[channel_id]
        
        use_client = client or self._client
        if not use_client:
            return {"id": channel_id, "name": channel_id}
            
        try:
            response = use_client.conversations_info(channel=channel_id)
            if response.get("ok"):
                channel = response.get("channel", {})
                self._channel_cache[channel_id] = channel
                return channel
        except SlackApiError as e:
            logger.warning(f"Could not get channel info: {e}")
        except Exception as e:
            logger.warning(f"Unexpected error getting channel info: {e}")
            
        return {"id": channel_id, "name": channel_id}
        
    async def _get_user_info(self, user_ids: list[str], client: Optional[WebClient] = None) -> dict[str, dict]:
        """Get user information for multiple users.
        
        Args:
            user_ids: List of Slack user IDs
            client: Optional specific client to use
            
        Returns:
            dict: Mapping of user_id to user info
        """
        users = {}
        use_client = client or self._client
        
        for user_id in user_ids:
            if not user_id:
                continue
            if not use_client:
                users[user_id] = {"id": user_id, "name": user_id}
                continue
            try:
                response = use_client.users_info(user=user_id)
                if response.get("ok"):
                    user = response.get("user", {})
                    users[user_id] = {
                        "id": user_id,
                        "name": user.get("real_name") or user.get("name", user_id),
                        "display_name": user.get("profile", {}).get("display_name", "")
                    }
            except SlackApiError:
                users[user_id] = {"id": user_id, "name": user_id}
            except Exception as e:
                logger.warning(f"Unexpected error getting user info for {user_id}: {e}")
                users[user_id] = {"id": user_id, "name": user_id}
                
        return users
        
    async def summarize_thread(
        self,
        thread_data: dict[str, Any],
        query: Optional[str] = None
    ) -> str:
        """Summarize a thread using LLM.
        
        Args:
            thread_data: Thread data from fetch_thread_messages
            query: Optional specific query about the thread
            
        Returns:
            str: Summarized thread content
        """
        if not self._llm_service:
            # Return formatted thread without LLM summarization
            return self._format_thread_simple(thread_data)
            
        messages = thread_data.get("messages", [])
        if not messages:
            return "No messages found in thread."
            
        # Build context for LLM
        channel_name = thread_data.get("channel_name", "unknown")
        message_count = thread_data.get("message_count", 0)
        
        # Format messages for LLM
        thread_text = f"Thread from #{channel_name} ({message_count} messages):\n\n"
        for msg in messages:
            prefix = "📌 [Thread Start]" if msg.get("is_parent") else "  └─"
            thread_text += f"{prefix} {msg['user']} ({msg['timestamp']}):\n"
            thread_text += f"     {msg['text']}\n\n"
            
        # Build prompt
        if query:
            prompt = f"""Summarize the following Slack thread, focusing on: {query}

{thread_text}

Provide a concise summary that:
1. Identifies the main topic/question
2. Lists key discussion points
3. Notes any decisions or action items
4. Highlights important participants

Use bullet points for clarity."""
        else:
            prompt = f"""Summarize the following Slack thread:

{thread_text}

Provide a concise summary that:
1. Identifies the main topic/question
2. Lists key discussion points
3. Notes any decisions or action items
4. Highlights important participants

Use bullet points for clarity."""

        try:
            summary = await self._llm_service.generate(
                prompt=prompt,
                max_tokens=1000,
                temperature=0.3
            )
            return summary.strip()
        except Exception as e:
            logger.error(f"Error generating thread summary: {e}")
            return self._format_thread_simple(thread_data)
            
    def _format_thread_simple(self, thread_data: dict[str, Any]) -> str:
        """Simple formatting of thread without LLM.
        
        Args:
            thread_data: Thread data from fetch_thread_messages
            
        Returns:
            str: Formatted thread content
        """
        messages = thread_data.get("messages", [])
        if not messages:
            return "No messages found in thread."
            
        channel_name = thread_data.get("channel_name", "unknown")
        parts = [f"## 💬 Thread from #{channel_name}\n"]
        parts.append(f"**{len(messages)} messages** from {len(thread_data.get('participants', []))} participants\n")
        
        for msg in messages:
            if msg.get("is_parent"):
                parts.append(f"\n### 📌 Original Message\n")
            else:
                parts.append(f"\n**{msg['user']}** ({msg['timestamp']}):\n")
            parts.append(f"> {msg['text']}\n")
            
        return "\n".join(parts)
        
    async def summarize_thread_url(
        self,
        url: str,
        query: Optional[str] = None
    ) -> dict[str, Any]:
        """Parse URL, fetch thread, and summarize.
        
        Args:
            url: Slack thread URL
            query: Optional specific query about the thread
            
        Returns:
            dict: Contains summary and metadata in QueryResponse format
        """
        # Parse URL
        parser = SlackThreadParser(url)
        if not parser.is_valid:
            return {
                "success": False,
                "error": f"Invalid Slack thread URL: {url}",
                "response": f"Could not parse Slack thread URL. Expected format: https://workspace.slack.com/archives/CHANNEL_ID/pTIMESTAMP"
            }
            
        # Fetch thread messages
        thread_data = await self.fetch_thread_messages(
            channel_id=parser.channel_id,
            thread_ts=parser.thread_ts
        )
        
        if not thread_data.get("success"):
            return {
                "success": False,
                "error": thread_data.get("error"),
                "response": f"Could not fetch thread: {thread_data.get('error')}"
            }
            
        # Generate summary
        summary = await self.summarize_thread(thread_data, query)
        
        # Build response in QueryResponse-compatible format
        return {
            "success": True,
            "response": summary,
            "raw_data": {
                "slack": thread_data.get("messages", [])
            },
            "metadata": {
                "channel_id": parser.channel_id,
                "channel_name": thread_data.get("channel_name"),
                "thread_ts": parser.thread_ts,
                "workspace": parser.workspace,
                "message_count": thread_data.get("message_count", 0),
                "participants": thread_data.get("participants", []),
                "source_url": url
            }
        }


# Standalone function for backward compatibility
def fetch_and_summarize_slack_thread(url: str, query: Optional[str] = None) -> str:
    """Synchronous wrapper for thread summarization.
    
    This function is used by the slack_source.py for processing thread URLs.
    
    Args:
        url: Slack thread URL
        query: Optional specific query
        
    Returns:
        str: Thread summary or error message
    """
    import asyncio
    
    service = SlackThreadService()
    
    # Parse URL
    parser = SlackThreadParser(url)
    if not parser.is_valid:
        return f"Invalid Slack thread URL: {url}"
        
    # Run async fetch in sync context
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    try:
        thread_data = loop.run_until_complete(
            service.fetch_thread_messages(parser.channel_id, parser.thread_ts)
        )
        
        if not thread_data.get("success"):
            return f"Error fetching thread: {thread_data.get('error')}"
            
        # Return simple formatted version (no async LLM call in sync context)
        return service._format_thread_simple(thread_data)
        
    except Exception as e:
        logger.error(f"Error in fetch_and_summarize_slack_thread: {e}")
        return f"Error processing thread: {str(e)}"

