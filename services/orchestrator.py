"""Central orchestrator for routing queries to data connector agents.

The orchestrator is responsible for:
- Broadcasting queries to all registered agents
- Collecting and filtering relevance scores
- Triggering data retrieval for relevant agents
- Synthesizing final responses from agent results
"""

import asyncio
import logging
import time
from typing import Optional, Any

from google.oauth2.credentials import Credentials

from config import settings
from models.query import QueryRequest, QueryResponse, AgentContribution
from models.agent_response import RelevanceScore, AgentResult
from services.agents.base_agent import BaseDataAgent
from services.agents.calendar_agent import CalendarAgent
from services.agents.gmail_agent import GmailAgent
from services.agents.drive_agent import DriveAgent
from services.llm.openai_service import OpenAIService

logger = logging.getLogger(__name__)


class Orchestrator:
    """Central orchestrator for multi-agent query processing.
    
    Manages the lifecycle of data connector agents and coordinates
    query processing across all registered agents.
    
    Args:
        relevance_threshold: Minimum score for agent activation
        
    Examples:
        >>> orchestrator = Orchestrator()
        >>> await orchestrator.initialize()
        >>> response = await orchestrator.process_query(request, credentials)
        >>> print(response.response)
    """
    
    def __init__(
        self,
        relevance_threshold: Optional[float] = None
    ):
        self.relevance_threshold = relevance_threshold or settings.relevance_threshold
        self._llm_service = OpenAIService()
        self._agents: list[BaseDataAgent] = []
        self._initialized = False
        
    async def initialize(self) -> None:
        """Initialize the orchestrator and all agents.
        
        Creates and initializes all data connector agents with
        a shared Claude service instance.
        """
        logger.info("Initializing orchestrator...")
        
        # Create agents with shared LLM service
        self._agents = [
            CalendarAgent(self._llm_service),
            GmailAgent(self._llm_service),
            DriveAgent(self._llm_service),
        ]
        
        # Initialize all agents
        await asyncio.gather(*[agent.initialize() for agent in self._agents])
        
        self._initialized = True
        logger.info(f"Orchestrator initialized with {len(self._agents)} agents")
        
    async def process_query(
        self,
        request: QueryRequest,
        credentials: Credentials
    ) -> QueryResponse:
        """Process a natural language query through all relevant agents.
        
        Args:
            request: Query request containing the user's question
            credentials: Google OAuth credentials for API access
            
        Returns:
            QueryResponse: Synthesized response with agent contributions
            
        Raises:
            RuntimeError: If orchestrator not initialized
            
        Examples:
            >>> request = QueryRequest(query="What meetings do I have tomorrow?", user_id="123")
            >>> response = await orchestrator.process_query(request, credentials)
        """
        if not self._initialized:
            raise RuntimeError("Orchestrator not initialized. Call initialize() first.")
            
        start_time = time.time()
        query = request.query
        threshold = request.threshold_override or self.relevance_threshold
        
        logger.info(f"Processing query: {query[:100]}...")
        
        # Phase 1: Parallel relevance scoring
        logger.debug("Phase 1: Evaluating relevance across all agents")
        relevance_scores = await self._evaluate_all_agents(query)
        
        # Phase 2: Filter by threshold
        relevant_agents = self._filter_relevant_agents(relevance_scores, threshold)
        logger.info(
            f"Phase 2: {len(relevant_agents)} agents meet threshold ({threshold}): "
            f"{[a.agent_name for a in relevant_agents]}"
        )
        
        # Phase 3: Parallel data fetching
        agent_results: list[AgentResult] = []
        if relevant_agents:
            logger.debug("Phase 3: Fetching data from relevant agents")
            agent_results = await self._fetch_from_agents(
                query, relevant_agents, relevance_scores, credentials
            )
            
        # Phase 4: Synthesize response
        logger.debug("Phase 4: Synthesizing final response")
        synthesized_response = await self._synthesize_response(query, agent_results)
        
        # Build agent contributions
        contributions = self._build_contributions(relevance_scores, agent_results, threshold)
        
        total_time = (time.time() - start_time) * 1000
        
        logger.info(f"Query processed in {total_time:.1f}ms")
        
        return QueryResponse(
            query=query,
            response=synthesized_response,
            agents_triggered=contributions,
            raw_data={
                agent_result.agent_name: agent_result.data
                for agent_result in agent_results
                if agent_result.success
            } if agent_results else None,
            total_execution_time_ms=total_time
        )
        
    async def _evaluate_all_agents(
        self,
        query: str
    ) -> dict[str, RelevanceScore]:
        """Evaluate relevance across all agents in parallel.
        
        Args:
            query: User's natural language query
            
        Returns:
            dict: Mapping of agent_name to RelevanceScore
        """
        tasks = [agent.evaluate_relevance(query) for agent in self._agents]
        scores = await asyncio.gather(*tasks, return_exceptions=True)
        
        result = {}
        for agent, score in zip(self._agents, scores):
            if isinstance(score, Exception):
                logger.error(f"Error evaluating {agent.agent_name}: {score}")
                result[agent.agent_name] = RelevanceScore(
                    agent_name=agent.agent_name,
                    score=0.0,
                    justification=f"Error: {str(score)}",
                    suggested_search_terms=[]
                )
            else:
                result[agent.agent_name] = score
                
        return result
        
    def _filter_relevant_agents(
        self,
        scores: dict[str, RelevanceScore],
        threshold: float
    ) -> list[BaseDataAgent]:
        """Filter agents by relevance threshold.
        
        Args:
            scores: Mapping of agent_name to RelevanceScore
            threshold: Minimum score for activation
            
        Returns:
            list: Agents that meet the threshold
        """
        relevant = []
        for agent in self._agents:
            score = scores.get(agent.agent_name)
            if score and score.score >= threshold:
                relevant.append(agent)
                
        # Sort by score descending
        relevant.sort(
            key=lambda a: scores[a.agent_name].score,
            reverse=True
        )
        
        return relevant
        
    async def _fetch_from_agents(
        self,
        query: str,
        agents: list[BaseDataAgent],
        scores: dict[str, RelevanceScore],
        credentials: Credentials
    ) -> list[AgentResult]:
        """Fetch data from relevant agents in parallel.
        
        Args:
            query: User's natural language query
            agents: Agents to fetch data from
            scores: Relevance scores (for search terms)
            credentials: Google OAuth credentials
            
        Returns:
            list: AgentResult objects from each agent
        """
        tasks = []
        for agent in agents:
            score = scores.get(agent.agent_name)
            search_terms = score.suggested_search_terms if score else None
            tasks.append(agent.fetch_data(query, credentials, search_terms))
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        processed_results = []
        for agent, result in zip(agents, results):
            if isinstance(result, Exception):
                logger.error(f"Error fetching from {agent.agent_name}: {result}")
                processed_results.append(AgentResult(
                    agent_name=agent.agent_name,
                    agent_type=agent.agent_type,
                    success=False,
                    error_message=str(result),
                    execution_time_ms=0.0
                ))
            else:
                processed_results.append(result)
                
        return processed_results
        
    async def _synthesize_response(
        self,
        query: str,
        agent_results: list[AgentResult]
    ) -> str:
        """Synthesize a final response from agent results.
        
        Args:
            query: Original user query
            agent_results: Results from triggered agents
            
        Returns:
            str: Synthesized natural language response
        """
        if not agent_results:
            return (
                "I wasn't able to find relevant information for your query. "
                "The data sources (Calendar, Gmail, Drive) don't seem to contain "
                "information related to your question. Please try rephrasing your "
                "query or ask about calendar events, emails, or files."
            )
            
        # Filter successful results with data
        successful_results = [
            {
                "agent_name": r.agent_name,
                "data": r.data,
                "metadata": r.metadata
            }
            for r in agent_results
            if r.success and r.data
        ]
        
        if not successful_results:
            failed_agents = [r.agent_name for r in agent_results if not r.success]
            no_data_agents = [r.agent_name for r in agent_results if r.success and not r.data]
            
            response_parts = []
            if failed_agents:
                response_parts.append(
                    f"I encountered errors accessing: {', '.join(failed_agents)}."
                )
            if no_data_agents:
                response_parts.append(
                    f"No matching data found in: {', '.join(no_data_agents)}."
                )
            return " ".join(response_parts) or "No data was retrieved from any source."
            
        return await self._llm_service.synthesize_response(query, successful_results)
        
    def _build_contributions(
        self,
        scores: dict[str, RelevanceScore],
        results: list[AgentResult],
        threshold: float
    ) -> list[AgentContribution]:
        """Build agent contribution details for response.
        
        Args:
            scores: Relevance scores for all agents
            results: Results from triggered agents
            threshold: Relevance threshold used
            
        Returns:
            list: AgentContribution objects for triggered agents
        """
        # Map results by agent name
        results_map = {r.agent_name: r for r in results}
        
        contributions = []
        for agent_name, score in scores.items():
            if score.score >= threshold:
                result = results_map.get(agent_name)
                contributions.append(AgentContribution(
                    agent_name=agent_name,
                    relevance_score=score.score,
                    justification=score.justification,
                    data_count=len(result.data) if result and result.success else 0,
                    execution_time_ms=result.execution_time_ms if result else 0.0
                ))
                
        # Sort by relevance score descending
        contributions.sort(key=lambda c: c.relevance_score, reverse=True)
        
        return contributions
        
    async def health_check(self) -> dict[str, Any]:
        """Check health of orchestrator and all agents.
        
        Returns:
            dict: Health status for orchestrator and each agent
        """
        health = {
            "orchestrator": {
                "initialized": self._initialized,
                "threshold": self.relevance_threshold,
                "agent_count": len(self._agents)
            },
            "agents": {}
        }
        
        for agent in self._agents:
            health["agents"][agent.agent_name] = await agent.health_check()
            
        # Check LLM service
        health["llm"] = await self._llm_service.health_check()
        
        return health
        
    async def shutdown(self) -> None:
        """Shutdown orchestrator and all agents."""
        logger.info("Shutting down orchestrator...")
        
        await asyncio.gather(*[agent.shutdown() for agent in self._agents])
        await self._llm_service.shutdown()
        
        self._initialized = False
        logger.info("Orchestrator shutdown complete")
        
    def get_metrics(self) -> dict[str, Any]:
        """Get orchestrator and agent metrics.
        
        Returns:
            dict: Metrics for orchestrator and all agents
        """
        return {
            "orchestrator": {
                "initialized": self._initialized,
                "threshold": self.relevance_threshold,
                "agent_count": len(self._agents)
            },
            "agents": {
                agent.agent_name: agent.get_metrics()
                for agent in self._agents
            },
            "llm": self._llm_service.get_metrics()
        }


# Global orchestrator instance
_orchestrator: Optional[Orchestrator] = None


async def get_orchestrator() -> Orchestrator:
    """Get or create the global orchestrator instance.
    
    Returns:
        Orchestrator: Initialized orchestrator instance
    """
    global _orchestrator
    
    if _orchestrator is None:
        _orchestrator = Orchestrator()
        await _orchestrator.initialize()
        
    return _orchestrator

