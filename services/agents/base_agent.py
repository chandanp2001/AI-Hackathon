"""Base agent interface for all data connector agents.

This module defines the abstract base class that all data connector agents
must implement. It provides the contract for relevance scoring and data
retrieval operations.
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from google.oauth2.credentials import Credentials

from models.agent_response import RelevanceScore, AgentResult, AgentType
from services.llm.openai_service import OpenAIService

logger = logging.getLogger(__name__)


class BaseDataAgent(ABC):
    """Abstract base class for data connector agents.
    
    Each data connector agent is responsible for:
    - Evaluating query relevance to its data source
    - Fetching data from its respective API
    - Returning structured, factual results
    
    Args:
        llm_service: Shared LLM service instance for OpenAI operations
        
    Examples:
        >>> class MyAgent(BaseDataAgent):
        ...     @property
        ...     def agent_name(self) -> str:
        ...         return "my_agent"
        ...     
        ...     async def fetch_data(self, query, credentials):
        ...         # Implementation
        ...         pass
    """
    
    def __init__(self, llm_service: Optional[OpenAIService] = None):
        self._llm_service = llm_service or OpenAIService()
        self._initialized = False
        
    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Unique identifier for this agent.
        
        Returns:
            str: Agent name (e.g., 'calendar', 'gmail', 'drive')
        """
        pass
    
    @property
    @abstractmethod
    def agent_type(self) -> AgentType:
        """Type of this agent.
        
        Returns:
            AgentType: Enum value for agent type
        """
        pass
    
    @property
    @abstractmethod
    def data_source_description(self) -> str:
        """Description of what this agent's data source contains.
        
        This is used by the LLM to evaluate relevance.
        
        Returns:
            str: Human-readable description of the data source
        """
        pass
    
    async def initialize(self) -> None:
        """Initialize the agent and any required resources.
        
        Override this method to perform setup tasks like
        connection pooling or cache initialization.
        """
        self._initialized = True
        logger.info(f"Agent {self.agent_name} initialized")
        
    async def evaluate_relevance(self, query: str) -> RelevanceScore:
        """Evaluate if this agent is relevant to the given query.
        
        Uses the LLM to determine relevance score based on the query content
        and the agent's data source description. Also extracts date/time
        references from the query for dynamic date filtering.
        
        Args:
            query: Natural language query from the user
            
        Returns:
            RelevanceScore: Score (0.0-1.0) with justification and optional date_range
            
        Raises:
            Exception: If LLM evaluation fails
            
        Examples:
            >>> score = await agent.evaluate_relevance("What meetings do I have on Dec 22?")
            >>> print(f"Score: {score.score}, Date range: {score.date_range}")
        """
        start_time = time.time()
        
        try:
            result = await self._llm_service.evaluate_relevance(
                query=query,
                agent_name=self.agent_name,
                data_source_description=self.data_source_description
            )
            
            score = RelevanceScore(
                agent_name=self.agent_name,
                score=float(result.get("score", 0.0)),
                justification=result.get("justification", "No justification provided"),
                suggested_search_terms=result.get("suggested_search_terms", []),
                date_range=result.get("date_range")
            )
            
            elapsed = (time.time() - start_time) * 1000
            
            # Log with date range info if present
            date_info = ""
            if score.date_range:
                date_info = f", date_range: {score.date_range.get('type', 'unknown')}"
            
            logger.info(
                f"Agent {self.agent_name} relevance score: {score.score:.2f}{date_info} "
                f"(took {elapsed:.1f}ms)"
            )
            
            return score
            
        except Exception as e:
            logger.error(f"Failed to evaluate relevance for {self.agent_name}: {e}")
            # Return a safe default score
            return RelevanceScore(
                agent_name=self.agent_name,
                score=0.0,
                justification=f"Error evaluating relevance: {str(e)}",
                suggested_search_terms=[],
                date_range=None
            )
    
    @abstractmethod
    async def fetch_data(
        self,
        query: str,
        credentials: Credentials,
        search_terms: Optional[list[str]] = None
    ) -> AgentResult:
        """Fetch data from this agent's data source.
        
        This method is called only when the agent's relevance score
        exceeds the threshold. It should retrieve factual data from
        the external API and return it in a structured format.
        
        Args:
            query: Original user query
            credentials: Google OAuth credentials for API access
            search_terms: Optional suggested search terms from relevance eval
            
        Returns:
            AgentResult: Structured result containing retrieved data
            
        Raises:
            Exception: If data retrieval fails
        """
        pass
    
    async def health_check(self) -> bool:
        """Check if the agent is healthy and ready to process queries.
        
        Returns:
            bool: True if agent is operational
        """
        return self._initialized
    
    async def shutdown(self) -> None:
        """Clean up agent resources.
        
        Override this method to perform cleanup tasks like
        closing connections or flushing caches.
        """
        self._initialized = False
        logger.info(f"Agent {self.agent_name} shut down")
        
    def get_metrics(self) -> dict[str, Any]:
        """Get agent metrics and status.
        
        Returns:
            dict: Metrics including agent name, type, and status
        """
        return {
            "agent_name": self.agent_name,
            "agent_type": self.agent_type.value,
            "initialized": self._initialized,
            "data_source": self.data_source_description[:100]
        }
        
    def _build_api_service(self, credentials: Credentials, service_name: str, version: str):
        """Build a Google API service client.
        
        Args:
            credentials: OAuth credentials
            service_name: Google API service name
            version: API version
            
        Returns:
            Google API service resource
        """
        from googleapiclient.discovery import build
        return build(service_name, version, credentials=credentials)

