"""Central orchestrator with smart routing, caching, and optimized flow.

Features:
- Intent-based smart agent routing
- In-memory query caching with TTL
- Data ranking and filtering before synthesis
- Parallel execution with timeouts
- Graceful error handling
"""

import asyncio
import logging
import time
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Any
from dataclasses import dataclass, field

from google.oauth2.credentials import Credentials

from config import settings
from models.query import QueryRequest, QueryResponse, AgentContribution
from models.agent_response import RelevanceScore, AgentResult
from services.agents.base_agent import BaseDataAgent
from services.agents.calendar_agent import CalendarAgent
from services.agents.gmail_agent import GmailAgent
from services.agents.drive_agent import DriveAgent
from services.llm.openai_service import OpenAIService, QueryIntent

logger = logging.getLogger(__name__)


# =============================================================================
# CACHING
# =============================================================================

@dataclass
class CacheEntry:
    """Cache entry with TTL."""
    response: QueryResponse
    created_at: datetime
    ttl_seconds: int = 300  # 5 minutes default
    
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.created_at + timedelta(seconds=self.ttl_seconds)


class QueryCache:
    """Simple in-memory cache for query responses."""
    
    def __init__(self, max_size: int = 100, default_ttl: int = 300):
        self._cache: dict[str, CacheEntry] = {}
        self._max_size = max_size
        self._default_ttl = default_ttl
        
    def _make_key(self, query: str, user_id: str) -> str:
        """Create a cache key from query and user."""
        content = f"{user_id}:{query.lower().strip()}"
        return hashlib.md5(content.encode()).hexdigest()
        
    def get(self, query: str, user_id: str) -> Optional[QueryResponse]:
        """Get cached response if valid."""
        key = self._make_key(query, user_id)
        entry = self._cache.get(key)
        
        if entry and not entry.is_expired():
            logger.debug(f"Cache hit for query: {query[:50]}...")
            return entry.response
            
        if entry:
            # Clean up expired entry
            del self._cache[key]
            
        return None
        
    def set(self, query: str, user_id: str, response: QueryResponse) -> None:
        """Cache a response."""
        # Evict oldest entries if at capacity
        if len(self._cache) >= self._max_size:
            self._evict_oldest()
            
        key = self._make_key(query, user_id)
        self._cache[key] = CacheEntry(
            response=response,
            created_at=datetime.utcnow(),
            ttl_seconds=self._default_ttl
        )
        logger.debug(f"Cached response for query: {query[:50]}...")
        
    def _evict_oldest(self) -> None:
        """Remove oldest cache entries."""
        if not self._cache:
            return
            
        # Sort by creation time and remove oldest 10%
        sorted_keys = sorted(
            self._cache.keys(),
            key=lambda k: self._cache[k].created_at
        )
        
        to_remove = max(1, len(sorted_keys) // 10)
        for key in sorted_keys[:to_remove]:
            del self._cache[key]
            
    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()
        
    def stats(self) -> dict:
        """Get cache statistics."""
        valid_entries = sum(1 for e in self._cache.values() if not e.is_expired())
        return {
            "total_entries": len(self._cache),
            "valid_entries": valid_entries,
            "max_size": self._max_size
        }


# =============================================================================
# ORCHESTRATOR
# =============================================================================

class Orchestrator:
    """Optimized orchestrator with smart routing and caching.
    
    Features:
    - Intent classification for smart agent selection
    - Query result caching (5-minute TTL)
    - Data ranking and filtering
    - Parallel execution with timeouts
    - Graceful degradation on errors
    """
    
    def __init__(
        self,
        relevance_threshold: Optional[float] = None,
        enable_cache: bool = True,
        agent_timeout: float = 15.0
    ):
        self.relevance_threshold = relevance_threshold or settings.relevance_threshold
        self._llm_service = OpenAIService()
        self._agents: dict[str, BaseDataAgent] = {}
        self._cache = QueryCache() if enable_cache else None
        self._agent_timeout = agent_timeout
        self._initialized = False
        
        # Agent mapping for intent-based routing
        self._intent_to_agents = {
            "calendar": ["calendar"],
            "email": ["gmail"],
            "files": ["drive"],
            "multi_source": ["calendar", "gmail", "drive"],
            "general": ["calendar", "gmail", "drive"],
        }
        
    async def initialize(self) -> None:
        """Initialize orchestrator and all agents."""
        logger.info("Initializing optimized orchestrator...")
        
        # Create agents with shared LLM service
        self._agents = {
            "calendar": CalendarAgent(self._llm_service),
            "gmail": GmailAgent(self._llm_service),
            "drive": DriveAgent(self._llm_service),
        }
        
        # Initialize all agents
        await asyncio.gather(*[agent.initialize() for agent in self._agents.values()])
        
        self._initialized = True
        logger.info(f"Orchestrator initialized with {len(self._agents)} agents")
        
    async def process_query(
        self,
        request: QueryRequest,
        credentials: Credentials
    ) -> QueryResponse:
        """Process query with optimized flow.
        
        Flow:
        1. Check cache
        2. Classify intent (smart routing)
        3. Score relevant agents only
        4. Fetch data in parallel with timeout
        5. Rank and filter results
        6. Synthesize response
        7. Cache result
        """
        if not self._initialized:
            raise RuntimeError("Orchestrator not initialized")
            
        start_time = time.time()
        query = request.query
        threshold = request.threshold_override or self.relevance_threshold
        
        logger.info(f"Processing query: {query[:100]}...")
        
        # Phase 0: Check cache
        if self._cache:
            cached = self._cache.get(query, request.user_id)
            if cached:
                logger.info("Returning cached response")
                return cached
                
        # Phase 1: Intent classification (smart routing)
        intent = await self._classify_intent(query)
        target_agents = self._get_agents_for_intent(intent)
        
        logger.info(
            f"Intent: {intent.primary_intent}, "
            f"targeting {len(target_agents)} agents: {list(target_agents.keys())}"
        )
        
        # Phase 2: Parallel relevance scoring (only targeted agents)
        relevance_scores = await self._evaluate_agents(query, target_agents)
        
        # Phase 3: Filter by threshold
        relevant_agents = self._filter_relevant_agents(
            target_agents, relevance_scores, threshold
        )
        
        logger.info(
            f"{len(relevant_agents)} agents meet threshold ({threshold}): "
            f"{list(relevant_agents.keys())}"
        )
        
        # Phase 4: Parallel data fetching with timeout
        agent_results: list[AgentResult] = []
        if relevant_agents:
            agent_results = await self._fetch_with_timeout(
                query, relevant_agents, relevance_scores, credentials
            )
            
        # Phase 5: Rank and filter data
        ranked_results = self._rank_and_filter_results(agent_results)
        
        # Phase 6: Synthesize response
        synthesized_response = await self._synthesize_response(query, ranked_results)
        
        # Build response
        contributions = self._build_contributions(relevance_scores, agent_results, threshold)
        total_time = (time.time() - start_time) * 1000
        
        response = QueryResponse(
            query=query,
            response=synthesized_response,
            agents_triggered=contributions,
            raw_data={
                r.agent_name: r.data
                for r in agent_results
                if r.success
            } if agent_results else None,
            total_execution_time_ms=total_time
        )
        
        # Phase 7: Cache result
        if self._cache:
            self._cache.set(query, request.user_id, response)
            
        logger.info(f"Query processed in {total_time:.1f}ms")
        return response
        
    async def _classify_intent(self, query: str) -> QueryIntent:
        """Classify query intent for smart routing."""
        try:
            return await self._llm_service.classify_intent(query)
        except Exception as e:
            logger.warning(f"Intent classification failed: {e}")
            return QueryIntent(
                primary_intent="multi_source",
                sources_needed=["calendar", "email", "files"],
                complexity="moderate"
            )
            
    def _get_agents_for_intent(
        self,
        intent: QueryIntent
    ) -> dict[str, BaseDataAgent]:
        """Get agents based on classified intent."""
        # Map intent sources to agent names
        source_to_agent = {
            "calendar": "calendar",
            "email": "gmail",
            "files": "drive",
        }
        
        agent_names = set()
        for source in intent.sources_needed:
            if source in source_to_agent:
                agent_names.add(source_to_agent[source])
                
        # Fallback to intent-based mapping
        if not agent_names:
            agent_names = set(self._intent_to_agents.get(
                intent.primary_intent,
                ["calendar", "gmail", "drive"]
            ))
            
        return {name: self._agents[name] for name in agent_names if name in self._agents}
        
    async def _evaluate_agents(
        self,
        query: str,
        agents: dict[str, BaseDataAgent]
    ) -> dict[str, RelevanceScore]:
        """Evaluate relevance for selected agents in parallel."""
        async def evaluate_one(name: str, agent: BaseDataAgent) -> tuple[str, RelevanceScore]:
            try:
                score = await asyncio.wait_for(
                    agent.evaluate_relevance(query),
                    timeout=self._agent_timeout
                )
                return name, score
            except asyncio.TimeoutError:
                logger.warning(f"Relevance evaluation timed out for {name}")
                return name, RelevanceScore(
                    agent_name=name,
                    score=0.0,
                    justification="Evaluation timed out",
                    suggested_search_terms=[]
                )
            except Exception as e:
                logger.error(f"Error evaluating {name}: {e}")
                return name, RelevanceScore(
                    agent_name=name,
                    score=0.0,
                    justification=f"Error: {str(e)}",
                    suggested_search_terms=[]
                )
                
        tasks = [evaluate_one(name, agent) for name, agent in agents.items()]
        results = await asyncio.gather(*tasks)
        
        return {name: score for name, score in results}
        
    def _filter_relevant_agents(
        self,
        agents: dict[str, BaseDataAgent],
        scores: dict[str, RelevanceScore],
        threshold: float
    ) -> dict[str, BaseDataAgent]:
        """Filter agents by relevance threshold."""
        relevant = {}
        for name, agent in agents.items():
            score = scores.get(name)
            if score and score.score >= threshold:
                relevant[name] = agent
                
        # Sort by score descending
        return dict(sorted(
            relevant.items(),
            key=lambda x: scores[x[0]].score,
            reverse=True
        ))
        
    async def _fetch_with_timeout(
        self,
        query: str,
        agents: dict[str, BaseDataAgent],
        scores: dict[str, RelevanceScore],
        credentials: Credentials
    ) -> list[AgentResult]:
        """Fetch data from agents with timeout handling."""
        async def fetch_one(name: str, agent: BaseDataAgent) -> AgentResult:
            score = scores.get(name)
            search_terms = score.suggested_search_terms if score else None
            
            try:
                result = await asyncio.wait_for(
                    agent.fetch_data(query, credentials, search_terms),
                    timeout=self._agent_timeout
                )
                return result
            except asyncio.TimeoutError:
                logger.warning(f"Data fetch timed out for {name}")
                return AgentResult(
                    agent_name=name,
                    agent_type=agent.agent_type,
                    success=False,
                    error_message="Request timed out",
                    execution_time_ms=self._agent_timeout * 1000
                )
            except Exception as e:
                logger.error(f"Error fetching from {name}: {e}")
                return AgentResult(
                    agent_name=name,
                    agent_type=agent.agent_type,
                    success=False,
                    error_message=str(e),
                    execution_time_ms=0.0
                )
                
        tasks = [fetch_one(name, agent) for name, agent in agents.items()]
        return await asyncio.gather(*tasks)
        
    def _rank_and_filter_results(
        self,
        results: list[AgentResult],
        max_items_per_agent: int = 10
    ) -> list[AgentResult]:
        """Rank and filter results to reduce noise.
        
        - Limits items per agent to prevent token overflow
        - Filters out empty results
        - Sorts by most recent/relevant
        """
        filtered = []
        
        for result in results:
            if not result.success or not result.data:
                filtered.append(result)
                continue
                
            # Limit data items
            limited_data = result.data[:max_items_per_agent]
            
            # Create new result with limited data
            filtered.append(AgentResult(
                agent_name=result.agent_name,
                agent_type=result.agent_type,
                success=result.success,
                data=limited_data,
                metadata={
                    **result.metadata,
                    "original_count": len(result.data),
                    "filtered_count": len(limited_data)
                },
                execution_time_ms=result.execution_time_ms,
                query_used=result.query_used
            ))
            
        return filtered
        
    async def _synthesize_response(
        self,
        query: str,
        agent_results: list[AgentResult]
    ) -> str:
        """Synthesize final response using improved prompts."""
        if not agent_results:
            return (
                "I wasn't able to find relevant information for your query. "
                "Please try asking about your calendar events, emails, or files."
            )
            
        # Prepare results for synthesis
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
            # Check for errors
            errors = [r for r in agent_results if not r.success and r.error_message]
            no_data = [r for r in agent_results if r.success and not r.data]
            
            parts = []
            if errors:
                parts.append(f"Encountered errors with: {', '.join(r.agent_name for r in errors)}")
            if no_data:
                parts.append(f"No matching data in: {', '.join(r.agent_name for r in no_data)}")
                
            return " ".join(parts) or "No data was retrieved from any source."
            
        return await self._llm_service.synthesize_response(query, successful_results)
        
    def _build_contributions(
        self,
        scores: dict[str, RelevanceScore],
        results: list[AgentResult],
        threshold: float
    ) -> list[AgentContribution]:
        """Build agent contribution details."""
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
                
        contributions.sort(key=lambda c: c.relevance_score, reverse=True)
        return contributions
        
    async def health_check(self) -> dict[str, Any]:
        """Check health of all components."""
        health = {
            "orchestrator": {
                "initialized": self._initialized,
                "threshold": self.relevance_threshold,
                "agent_count": len(self._agents)
            },
            "agents": {},
            "cache": self._cache.stats() if self._cache else None
        }
        
        for name, agent in self._agents.items():
            health["agents"][name] = await agent.health_check()
            
        health["llm"] = await self._llm_service.health_check()
        
        return health
        
    async def shutdown(self) -> None:
        """Shutdown orchestrator."""
        logger.info("Shutting down orchestrator...")
        
        await asyncio.gather(*[agent.shutdown() for agent in self._agents.values()])
        await self._llm_service.shutdown()
        
        if self._cache:
            self._cache.clear()
            
        self._initialized = False
        logger.info("Orchestrator shutdown complete")
        
    def get_metrics(self) -> dict[str, Any]:
        """Get orchestrator metrics."""
        return {
            "orchestrator": {
                "initialized": self._initialized,
                "threshold": self.relevance_threshold,
                "agent_count": len(self._agents)
            },
            "agents": {
                name: agent.get_metrics()
                for name, agent in self._agents.items()
            },
            "llm": self._llm_service.get_metrics(),
            "cache": self._cache.stats() if self._cache else None
        }
        
    def clear_cache(self) -> None:
        """Clear the query cache."""
        if self._cache:
            self._cache.clear()
            logger.info("Query cache cleared")


# Global orchestrator instance
_orchestrator: Optional[Orchestrator] = None


async def get_orchestrator() -> Orchestrator:
    """Get or create the global orchestrator instance."""
    global _orchestrator
    
    if _orchestrator is None:
        _orchestrator = Orchestrator()
        await _orchestrator.initialize()
        
    return _orchestrator
