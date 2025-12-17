"""Slack data connector agent.

This agent handles:
- Reading: Slack message search across channels
- Queries about team communications and discussions
"""

import logging
import time
from typing import Optional, Any

from google.oauth2.credentials import Credentials

from config import settings
from models.agent_response import (
    AgentResult,
    AgentType,
    RelevanceScore,
)
from services.agents.base_agent import BaseDataAgent
from services.llm.openai_service import OpenAIService
from services.data_sources.slack_source import SlackDataSource

logger = logging.getLogger(__name__)

# Singleton instance for SlackDataSource
_slack_source = None


def get_slack_data_source():
    """Get or create the Slack data source instance."""
    global _slack_source
    
    if _slack_source is None:
        _slack_source = SlackDataSource()
        logger.info("Initialized Slack data source for agent")
    
    return _slack_source


class SlackAgent(BaseDataAgent):
    """Slack data connector agent.
    
    Handles queries about:
    - Team messages and discussions
    - Channel conversations
    - Slack message search
    - Communication history
    
    Args:
        llm_service: Shared LLM service for OpenAI operations
        
    Examples:
        >>> agent = SlackAgent()
        >>> score = await agent.evaluate_relevance("What did the team discuss about the deployment?")
        >>> if score.score >= 0.5:
        ...     result = await agent.fetch_data(query, credentials)
    """
    
    def __init__(self, llm_service: Optional[OpenAIService] = None):
        super().__init__(llm_service)
        # Use configurable limits from settings with sensible defaults
        self._max_results = settings.slack_max_results  # Default: 50 (increased from 20)
        self._max_channels = settings.slack_max_channels  # Default: 20 (increased from 10)
        self._messages_per_channel = settings.slack_messages_per_channel  # Default: 15 (increased from 5)
        self._slack_source = None
        
    @property
    def agent_name(self) -> str:
        """Return the agent identifier."""
        return "slack"
    
    @property
    def agent_type(self) -> AgentType:
        """Return the agent type."""
        return AgentType.SLACK
    
    @property
    def data_source_description(self) -> str:
        """Return description of the Slack data source."""
        return """Slack - Contains:
- Team messages and discussions across channels
- Channel conversations (public and private)
- Direct messages and group chats
- Message threads and replies
- User mentions and reactions
- Shared files and links in messages
- Channel topics and purposes
- Message timestamps and history
- Search across all accessible channels"""

    async def initialize(self) -> None:
        """Initialize the agent and Slack data source."""
        try:
            self._slack_source = get_slack_data_source()
            self._initialized = True
            logger.info(f"Agent {self.agent_name} initialized with Slack data source")
        except Exception as e:
            logger.error(f"Failed to initialize Slack agent: {e}")
            self._initialized = False
            raise

    async def fetch_data(
        self,
        query: str,
        credentials: Credentials,
        search_terms: Optional[list[str]] = None
    ) -> AgentResult:
        """Fetch Slack messages based on the query.
        
        Note: Slack uses its own authentication (bot/user tokens),
        not Google OAuth credentials. The credentials parameter is
        kept for interface compatibility but not used.
        
        Args:
            query: User's natural language query
            credentials: Not used for Slack (kept for interface compatibility)
            search_terms: Optional search terms from relevance evaluation
            
        Returns:
            AgentResult: Contains list of Slack messages
        """
        start_time = time.time()
        
        try:
            if not self._slack_source:
                self._slack_source = get_slack_data_source()
            
            # Build search query
            search_query = query
            if search_terms:
                search_query = " ".join(search_terms)
            
            # Build params for Slack search
            params = {
                'query': search_query,
                'user_id': 'default',
                'limit': self._max_results,
                'max_channels': self._max_channels,
                'include_dms': False,
                'deep_search': False,
            }
            
            # Perform the search
            results = await self._slack_source.fetch_data(params)
            
            # Check for errors
            if 'error' in results:
                execution_time = (time.time() - start_time) * 1000
                logger.error(f"Slack search error: {results['error']}")
                return AgentResult(
                    agent_name=self.agent_name,
                    agent_type=self.agent_type,
                    success=False,
                    error_message=f"Slack search failed: {results['error']}",
                    execution_time_ms=execution_time
                )
            
            # Extract metadata
            metadata = results.get('_metadata', {})
            
            # Format results for the response
            formatted_messages = []
            for channel_name, channel_data in results.items():
                if channel_name == '_metadata':
                    continue
                    
                messages = channel_data.get('messages', [])
                # Use configurable limit per channel (default: 15, increased from 5)
                for msg in messages[:self._messages_per_channel]:
                    formatted_messages.append({
                        'channel': channel_name,
                        'text': msg.get('text', ''),
                        'user': msg.get('user', 'unknown'),
                        'timestamp': msg.get('timestamp', ''),
                        'thread_ts': msg.get('thread_ts'),
                        'permalink': msg.get('permalink'),
                    })
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(
                f"Slack agent fetched {len(formatted_messages)} messages "
                f"from {metadata.get('channels_processed', 0)} channels "
                f"(took {execution_time:.1f}ms)"
            )
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=formatted_messages,
                metadata={
                    "search_query": search_query,
                    "total_messages": len(formatted_messages),
                    "channels_searched": metadata.get('channels_processed', 0),
                    "summary": metadata.get('summary', ''),
                },
                execution_time_ms=execution_time,
                query_used=search_query
            )
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error in Slack agent: {e}", exc_info=True)
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
    
    async def health_check(self) -> bool:
        """Check if the agent is healthy."""
        if not self._initialized:
            return False
        try:
            # Just check if we can access the slack source
            return self._slack_source is not None
        except Exception:
            return False

