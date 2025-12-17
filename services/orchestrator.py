"""Central orchestrator with smart routing, caching, action support, and optimized flow.

Features:
- Intent-based smart agent routing
- Action classification and execution
- In-memory query caching with TTL
- Data ranking and filtering before synthesis
- Parallel execution with timeouts
- Graceful error handling
"""

import asyncio
import logging
import time
import hashlib
import re
from datetime import datetime, timedelta
from typing import Optional, Any, Union
from dataclasses import dataclass, field

from google.oauth2.credentials import Credentials

from config import settings
from models.query import QueryRequest, QueryResponse, AgentContribution
from models.agent_response import RelevanceScore, AgentResult, ConfidenceLevel
from models.action import (
    ActionIntent,
    ActionPlan,
    ActionResult,
    ActionStepResult,
    ActionType,
    QueryType,
    RiskLevel,
)
from services.agents.base_agent import BaseDataAgent
from services.agents.calendar_agent import CalendarAgent
from services.agents.gmail_agent import GmailAgent
from services.agents.drive_agent import DriveAgent
from services.agents.slack_agent import SlackAgent
from services.agents.devrev_agent import DevRevAgent
from services.action_planner import ActionPlanner
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
    """Optimized orchestrator with smart routing, caching, and action support.
    
    Features:
    - Intent classification for smart agent selection
    - Action planning and execution
    - Query result caching (5-minute TTL)
    - Data ranking and filtering
    - Parallel execution with timeouts
    - Graceful degradation on errors
    """
    
    def __init__(
        self,
        relevance_threshold: Optional[float] = None,
        enable_cache: bool = False,  # Disabled by default - always fetch fresh data
        agent_timeout: float = 30.0
    ):
        self.relevance_threshold = relevance_threshold or settings.relevance_threshold
        self._llm_service = OpenAIService()
        self._action_planner = ActionPlanner(self._llm_service)
        self._agents: dict[str, BaseDataAgent] = {}
        self._cache = QueryCache() if enable_cache else None
        self._agent_timeout = agent_timeout
        self._initialized = False
        
        # Agent mapping for intent-based routing
        self._intent_to_agents = {
            "calendar": ["calendar"],
            "email": ["gmail"],
            "files": ["drive"],
            "slack": ["slack"],
            "devrev": ["devrev"],
            "tickets": ["devrev"],
            "issues": ["devrev"],
            "messages": ["slack", "gmail"],
            "multi_source": ["calendar", "gmail", "drive", "slack", "devrev"],
            "general": ["calendar", "gmail", "drive", "slack", "devrev"],
        }
        
        # Action type to agent mapping
        self._action_to_agent = {
            ActionType.CREATE_EVENT: "calendar",
            ActionType.UPDATE_EVENT: "calendar",
            ActionType.DELETE_EVENT: "calendar",
            ActionType.CHECK_AVAILABILITY: "calendar",
            ActionType.SEND_EMAIL: "gmail",
            ActionType.CREATE_DRAFT: "gmail",
            ActionType.REPLY_EMAIL: "gmail",
            ActionType.FORWARD_EMAIL: "gmail",
            ActionType.CREATE_DOCUMENT: "drive",
            ActionType.CREATE_SPREADSHEET: "drive",
            ActionType.SHARE_FILE: "drive",
            ActionType.CREATE_FOLDER: "drive",
        }
        
    async def initialize(self) -> None:
        """Initialize orchestrator and all agents."""
        logger.info("Initializing optimized orchestrator...")
        
        # Create agents with shared LLM service
        self._agents = {
            "calendar": CalendarAgent(self._llm_service),
            "gmail": GmailAgent(self._llm_service),
            "drive": DriveAgent(self._llm_service),
            "slack": SlackAgent(self._llm_service),
            "devrev": DevRevAgent(self._llm_service),
        }
        
        # Initialize all agents (with error handling for Slack)
        init_tasks = []
        for name, agent in self._agents.items():
            init_tasks.append(self._safe_agent_init(name, agent))
        await asyncio.gather(*init_tasks)
        
        self._initialized = True
        logger.info(f"Orchestrator initialized with {len(self._agents)} agents")
        
    async def _safe_agent_init(self, name: str, agent: BaseDataAgent) -> None:
        """Safely initialize an agent, logging errors but not failing."""
        try:
            await agent.initialize()
            logger.info(f"Agent {name} initialized successfully")
        except Exception as e:
            logger.warning(f"Agent {name} initialization failed: {e} - agent will be unavailable")
        
    async def process_query(
        self,
        request: QueryRequest,
        credentials: Credentials,
        conversation_history: Optional[list[dict[str, str]]] = None
    ) -> Union[QueryResponse, dict[str, Any]]:
        """Process query with optimized flow, supporting both reads and actions.
        
        Flow:
        1. Classify intent (read vs action)
        2. For reads:
           a. Check cache
           b. Score relevant agents
           c. Fetch data in parallel
           d. Synthesize response
           e. Cache result
        3. For actions:
           a. Create action plan
           b. Return plan for confirmation
           c. (Execute after user confirms)
           
        Args:
            request: Query request with user_id and query
            credentials: Google OAuth credentials
            conversation_history: Optional list of previous messages for context
        """
        if not self._initialized:
            raise RuntimeError("Orchestrator not initialized")
            
        start_time = time.time()
        query = request.query
        threshold = request.threshold_override or self.relevance_threshold
        
        logger.info(f"Processing query: {query[:100]}...")
        
        # Phase 0.5: Check if query contains a Slack thread URL for summarization
        slack_thread_url_pattern = re.compile(
            r'https?://[\w-]+\.slack\.com/archives/[A-Z0-9]+/p\d+'
        )
        slack_thread_match = slack_thread_url_pattern.search(query)
        
        if slack_thread_match:
            logger.info(f"Detected Slack thread URL in query: {slack_thread_match.group()}")
            return await self._handle_slack_thread_summary(
                thread_url=slack_thread_match.group(),
                query=query,
                credentials=credentials,
                start_time=start_time
            )
        
        # Phase 1: Intent classification (smart routing + action detection)
        # Pass conversation history to help recognize follow-up responses to actions
        intent = await self._classify_intent(query, conversation_history)
        
        logger.info(f"Intent classified: query_type={intent.query_type}, action_type={intent.action_type}")
        
        # Check if this is an action request
        if intent.query_type in ("action", "workflow"):
            logger.info(f"Detected action query: {intent.action_type}")
            return await self._handle_action_query(
                query=query,
                intent_data=intent.model_dump(),
                credentials=credentials,
                conversation_history=conversation_history,
                start_time=start_time
            )
            
        # For read queries, proceed with normal flow
        # Phase 0: Check cache (only if caching is enabled and skip_cache is False)
        if self._cache and not request.skip_cache:
            cached = self._cache.get(query, request.user_id)
            if cached:
                logger.info("Returning cached response")
                return cached
        elif request.skip_cache:
            logger.info("Skipping cache as requested - fetching fresh data")
                
        target_agents = self._get_agents_for_intent(intent)
        
        logger.info(
            f"Intent: {intent.primary_intent}, "
            f"targeting {len(target_agents)} agents: {list(target_agents.keys())}"
        )
        
        # Phase 2: Parallel relevance scoring (only targeted agents)
        relevance_scores = await self._evaluate_agents(query, target_agents)
        
        # Log all relevance scores for debugging
        for agent_name, score in relevance_scores.items():
            logger.info(f"Agent {agent_name} score: {score.score:.2f} - {score.justification[:80]}")
        
        # Phase 3: Filter by threshold
        relevant_agents = self._filter_relevant_agents(
            target_agents, relevance_scores, threshold
        )
        
        logger.info(
            f"{len(relevant_agents)} agents meet threshold ({threshold}): "
            f"{list(relevant_agents.keys())}"
        )
        
        # Phase 3.5: Check if clarification is needed using combined approach
        # Trigger clarification when:
        # 1. Max relevance score < 0.4 across all agents, OR
        # 2. LLM query clarity score < 0.5, OR  
        # 3. Agent confidence is "low" in relevance evaluation
        max_score = max((s.score for s in relevance_scores.values()), default=0.0)
        has_low_confidence = any(
            s.confidence_level == ConfidenceLevel.LOW 
            for s in relevance_scores.values()
        )
        all_low_confidence = all(
            s.confidence_level == ConfidenceLevel.LOW 
            for s in relevance_scores.values()
        ) if relevance_scores else True
        
        needs_clarification = (
            not relevant_agents or 
            max_score < 0.4 or 
            all_low_confidence
        )
        
        if needs_clarification:
            clarification = await self._check_clarification_needed(
                query, relevance_scores, conversation_history
            )
            if clarification:
                return clarification
        
        # Phase 4: Parallel data fetching with timeout
        agent_results: list[AgentResult] = []
        if relevant_agents:
            agent_results = await self._fetch_with_timeout(
                query, relevant_agents, relevance_scores, credentials
            )
            
            # Check if any agent returned a clarification request
            clarification_result = self._check_agent_clarification(agent_results)
            if clarification_result:
                return clarification_result
            
        # Phase 5: Rank and filter data
        ranked_results = self._rank_and_filter_results(agent_results)
        
        # Phase 6: Synthesize response with conversation context
        synthesized_response = await self._synthesize_response(
            query, ranked_results, conversation_history
        )
        
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
        
        # Phase 7: Cache result (only if caching is enabled and skip_cache is False)
        if self._cache and not request.skip_cache:
            self._cache.set(query, request.user_id, response)
            
        logger.info(f"Query processed in {total_time:.1f}ms")
        return response
        
    async def _classify_intent(
        self, 
        query: str,
        conversation_history: Optional[list[dict[str, str]]] = None
    ) -> QueryIntent:
        """Classify query intent for smart routing.
        
        Args:
            query: User's query
            conversation_history: Optional conversation history to recognize follow-ups
        """
        try:
            return await self._llm_service.classify_intent(query, conversation_history)
        except Exception as e:
            logger.warning(f"Intent classification failed: {e}")
            return QueryIntent(
                primary_intent="multi_source",
                sources_needed=["calendar", "email", "files"],
                complexity="moderate"
            )
            
    async def _check_clarification_needed(
        self,
        query: str,
        relevance_scores: dict[str, RelevanceScore],
        conversation_history: Optional[list[dict[str, str]]] = None
    ) -> Optional[dict[str, Any]]:
        """Check if the query needs clarification and return a clarification response.
        
        Uses a combined approach:
        1. Check LLM query clarity score
        2. Consider agent relevance scores
        3. Check agent confidence levels
        4. Include conversation context in clarification
        
        Args:
            query: Original user query
            relevance_scores: Relevance scores from all agents
            conversation_history: Optional conversation history for context
            
        Returns:
            Clarification response dict if clarification needed, None otherwise
        """
        try:
            # Get clarity check from LLM
            clarity_result = await self._llm_service.check_query_clarity(query)
            clarity_score = clarity_result.get("clarity_score", 1.0)
            
            # Calculate confidence-based need for clarification
            max_score = max((s.score for s in relevance_scores.values()), default=0.0)
            low_confidence_agents = [
                name for name, s in relevance_scores.items() 
                if s.confidence_level == ConfidenceLevel.LOW
            ]
            
            # Determine if clarification is needed based on combined factors
            needs_clarification = (
                clarity_result.get("needs_clarification", False) or
                clarity_score < 0.5 or
                (max_score < 0.4 and len(low_confidence_agents) > 0)
            )
            
            if needs_clarification:
                logger.info(
                    f"Query needs clarification: clarity_score={clarity_score:.2f}, "
                    f"max_relevance={max_score:.2f}, reason={clarity_result.get('reason')}"
                )
                
                # Build context-aware clarification message
                context_note = ""
                if conversation_history and len(conversation_history) > 0:
                    context_note = (
                        "I noticed you've been asking about related topics. "
                        "To help you better, could you clarify:"
                    )
                else:
                    context_note = "To help you better, could you clarify:"
                
                return {
                    "type": "clarification",
                    "query": query,
                    "needs_clarification": True,
                    "reason": clarity_result.get("reason", "Query is ambiguous"),
                    "context_note": context_note,
                    "suggested_questions": clarity_result.get("suggested_questions", [
                        "Could you be more specific about what you're looking for?",
                        "Which data source should I search - Calendar, Email, Drive, Slack, or DevRev?"
                    ]),
                    "likely_sources": clarity_result.get("likely_sources", []),
                    "agent_scores": {name: s.score for name, s in relevance_scores.items()},
                    "confidence_levels": {
                        name: s.confidence_level.value 
                        for name, s in relevance_scores.items()
                    },
                    "clarity_score": clarity_score
                }
                
        except Exception as e:
            logger.warning(f"Clarification check failed: {e}")
            
        return None
        
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
            "slack": "slack",
            "devrev": "devrev",
            "tickets": "devrev",
            "issues": "devrev",
        }
        
        agent_names = set()
        for source in intent.sources_needed:
            if source in source_to_agent:
                agent_names.add(source_to_agent[source])
                
        # Fallback to intent-based mapping
        if not agent_names:
            agent_names = set(self._intent_to_agents.get(
                intent.primary_intent,
                ["calendar", "gmail", "drive", "slack", "devrev"]
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
    
    def _check_agent_clarification(
        self,
        agent_results: list[AgentResult]
    ) -> Optional[dict[str, Any]]:
        """Check if any agent returned a clarification request.
        
        This is particularly important for DevRev queries that need
        more specificity before fetching data.
        
        Args:
            agent_results: List of results from agents
            
        Returns:
            Clarification response dict if clarification needed, None otherwise
        """
        for result in agent_results:
            if result.metadata and result.metadata.get("type") == "clarification_needed":
                logger.info(f"Agent {result.agent_name} requested clarification")
                return {
                    "type": "clarification",
                    "agent": result.agent_name,
                    "query": result.query_used,
                    "needs_clarification": True,
                    "reason": result.metadata.get("reason", "Query needs more specificity"),
                    "clarity_score": result.metadata.get("clarity_score", 0.0),
                    "suggested_questions": result.metadata.get("questions", []),
                    "suggested_refinements": result.metadata.get("suggested_refinements", []),
                    "message": result.metadata.get("clarification_message", ""),
                    "execution_time_ms": result.execution_time_ms
                }
        return None
        
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
        agent_results: list[AgentResult],
        conversation_history: Optional[list[dict[str, str]]] = None
    ) -> str:
        """Synthesize final response using improved prompts.
        
        Uses pre-formatted responses from agents when available (e.g., DevRev),
        otherwise falls back to LLM synthesis.
        
        Args:
            query: User's query
            agent_results: Results from agents
            conversation_history: Optional previous conversation for context
        """
        if not agent_results:
            return (
                "I wasn't able to find relevant information for your query. "
                "Please try asking about your calendar events, emails, or files."
            )
        
        # Check if any agent provided a pre-formatted response
        # This is used for DevRev and other agents with dynamic formatting
        formatted_parts = []
        needs_llm_synthesis = []
        
        for r in agent_results:
            if r.success and r.data:
                if r.metadata and r.metadata.get("formatted_response"):
                    # Use pre-formatted response from agent
                    formatted_parts.append(r.metadata["formatted_response"])
                    logger.debug(f"Using pre-formatted response from {r.agent_name}")
                else:
                    # Queue for LLM synthesis
                    needs_llm_synthesis.append({
                        "agent_name": r.agent_name,
                        "data": r.data,
                        "metadata": r.metadata
                    })
        
        # If we have only pre-formatted responses, use them directly
        if formatted_parts and not needs_llm_synthesis:
            return "\n\n".join(formatted_parts)
        
        # If we have both, combine formatted parts with LLM synthesis
        if formatted_parts and needs_llm_synthesis:
            llm_response = await self._llm_service.synthesize_response(
                query, needs_llm_synthesis, conversation_history
            )
            return "\n\n".join(formatted_parts) + "\n\n" + llm_response
            
        # Prepare results for synthesis (no pre-formatted responses)
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
            
        return await self._llm_service.synthesize_response(
            query, successful_results, conversation_history
        )
        
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
            
    # =========================================================================
    # ACTION HANDLING
    # =========================================================================
    
    async def _handle_action_query(
        self,
        query: str,
        intent_data: dict[str, Any],
        credentials: Credentials,
        conversation_history: Optional[list[dict[str, str]]] = None,
        start_time: float = None
    ) -> dict[str, Any]:
        """Handle an action query by creating an action plan.
        
        Args:
            query: User query
            intent_data: Classified intent data
            credentials: User credentials
            conversation_history: Optional conversation history for context
            start_time: Query start time
            
        Returns:
            dict: Action plan response with confirmation request
        """
        try:
            # Convert to ActionIntent
            action_intent = await self._action_planner.classify_action_intent(intent_data)
            
            if not action_intent.action_type:
                # Could not determine action type, fall back to read
                logger.warning("Could not determine action type, treating as read query")
                return {
                    "type": "clarification_needed",
                    "message": "I'm not sure what action you'd like me to take. Could you please clarify?",
                    "suggestions": [
                        "Try: 'Schedule a meeting with...'",
                        "Try: 'Send an email to...'",
                        "Try: 'Create a document called...'"
                    ],
                    "execution_time_ms": (time.time() - start_time) * 1000
                }
                
            # Check for missing parameters that need clarification
            plan_data = await self._action_planner._extract_action_parameters(query, action_intent)
            
            if plan_data.get("clarification_needed"):
                return {
                    "type": "clarification_needed",
                    "message": plan_data["clarification_needed"],
                    "missing_parameters": plan_data.get("missing_parameters", []),
                    "partial_plan": plan_data.get("parameters", {}),
                    "execution_time_ms": (time.time() - start_time) * 1000
                }
            
            # For meeting creation, check attendee availability first
            if action_intent.action_type == ActionType.CREATE_EVENT:
                availability_result = await self._check_meeting_availability(
                    credentials, plan_data.get("parameters", {})
                )
                if availability_result:
                    return {
                        "type": "availability_conflict",
                        "message": availability_result["message"],
                        "conflicts": availability_result.get("conflicts", []),
                        "suggested_times": availability_result.get("suggested_times", []),
                        "original_plan": plan_data.get("parameters", {}),
                        "execution_time_ms": (time.time() - start_time) * 1000
                    }
                
            # Create action plan with conversation history for context
            plan = await self._action_planner.create_action_plan(
                query=query,
                intent=action_intent,
                conversation_history=conversation_history,
                context=None
            )
            
            total_time = (time.time() - start_time) * 1000
            
            # Extract parameters for editable fields
            params = {}
            if plan.steps:
                params = plan.steps[0].parameters or {}
            
            # Build editable fields based on action type
            editable_fields = self._get_editable_fields(action_intent.action_type, params)
            
            return {
                "type": "action_plan",
                "plan_id": plan.plan_id,
                "query": query,
                "summary": plan.summary,
                "preview": plan.preview,
                "steps": [
                    {
                        "step_number": step.step_number,
                        "action": step.action_type.value,
                        "description": step.description,
                        "agent": step.agent,
                        "parameters": step.parameters
                    }
                    for step in plan.steps
                ],
                "editable_fields": editable_fields,
                "risk_level": plan.risk_level.value,
                "requires_confirmation": action_intent.requires_confirmation,
                "estimated_duration": plan.estimated_duration,
                "execution_time_ms": total_time
            }
            
        except Exception as e:
            logger.error(f"Error handling action query: {e}")
            return {
                "type": "error",
                "message": f"Failed to create action plan: {str(e)}",
                "execution_time_ms": (time.time() - start_time) * 1000
            }
    
    async def _check_meeting_availability(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Check if attendees are available for the proposed meeting time.
        
        Args:
            credentials: User credentials for calendar API
            params: Meeting parameters including attendees and start_time
            
        Returns:
            dict with conflict info if there are conflicts, None if all clear
        """
        attendees = params.get("attendees", [])
        start_time = params.get("start_time")
        duration_minutes = params.get("duration_minutes", 60)
        
        # Skip check if no attendees or no start time
        if not attendees or not start_time:
            return None
        
        try:
            # Parse the start time to calculate end time
            calendar_agent = self._agents.get("calendar")
            if not calendar_agent:
                logger.warning("Calendar agent not available for availability check")
                return None
            
            # Use calendar agent's check_availability method
            availability_result = await calendar_agent.check_availability(
                credentials,
                {
                    "attendees": attendees,
                    "start_time": start_time,
                    "duration_minutes": duration_minutes
                }
            )
            
            if not availability_result.success:
                logger.warning(f"Availability check failed: {availability_result.error_message}")
                return None
            
            # Check for conflicts in the result
            availability_data = availability_result.result_data or {}
            availability_info = availability_data.get("availability", {})
            
            conflicts = []
            for calendar_id, cal_data in availability_info.items():
                if calendar_id == "primary":
                    continue  # Skip self
                    
                busy_times = cal_data.get("busy_times", [])
                if busy_times:
                    conflicts.append({
                        "attendee": calendar_id,
                        "busy_periods": busy_times[:3]  # Show up to 3 conflicts
                    })
            
            if conflicts:
                # Format a helpful message
                conflict_names = [c["attendee"] for c in conflicts]
                message = f"⚠️ **Scheduling Conflict Detected**\n\n"
                message += f"The following attendee(s) appear to be busy at the proposed time:\n"
                for conflict in conflicts:
                    message += f"- **{conflict['attendee']}** has {len(conflict['busy_periods'])} conflicting event(s)\n"
                message += f"\nWould you like to:\n"
                message += f"1. Proceed anyway (attendees will be notified)\n"
                message += f"2. Choose a different time\n"
                message += f"3. Check their full availability for today"
                
                return {
                    "message": message,
                    "conflicts": conflicts,
                    "suggested_times": []  # Could add smart time suggestions here
                }
            
            return None  # No conflicts
            
        except Exception as e:
            logger.warning(f"Error checking availability: {e}")
            return None  # Don't block on availability check failures
            
    async def execute_action_plan(
        self,
        plan_id: str,
        credentials: Credentials
    ) -> ActionResult:
        """Execute a confirmed action plan.
        
        Args:
            plan_id: ID of the plan to execute
            credentials: User credentials
            
        Returns:
            ActionResult: Result of the execution
        """
        # Get the plan
        plan = self._action_planner.get_pending_plan(plan_id)
        if not plan:
            return ActionResult(
                plan_id=plan_id,
                success=False,
                step_results=[],
                summary="Action plan not found or expired",
                links={}
            )
            
        logger.info(f"Executing action plan {plan_id}: {plan.summary}")
        
        step_results: list[ActionStepResult] = []
        all_links: dict[str, str] = {}
        overall_success = True
        
        for step in plan.steps:
            # Get the appropriate agent
            agent_name = self._action_to_agent.get(step.action_type)
            if not agent_name or agent_name not in self._agents:
                step_results.append(ActionStepResult(
                    step_number=step.step_number,
                    success=False,
                    error_message=f"No agent for action type: {step.action_type}",
                    execution_time_ms=0.0
                ))
                overall_success = False
                continue
                
            agent = self._agents[agent_name]
            
            try:
                # Execute the action
                result = await asyncio.wait_for(
                    agent.execute_action(
                        action_type=step.action_type,
                        credentials=credentials,
                        params=step.parameters
                    ),
                    timeout=self._agent_timeout
                )
                
                result.step_number = step.step_number
                step_results.append(result)
                
                if not result.success:
                    overall_success = False
                    logger.error(f"Step {step.step_number} failed: {result.error_message}")
                    break  # Stop on failure
                    
                # Collect links
                if result.result_data:
                    for key in ["html_link", "web_view_link", "meeting_link"]:
                        if result.result_data.get(key):
                            all_links[key] = result.result_data[key]
                            
            except asyncio.TimeoutError:
                step_results.append(ActionStepResult(
                    step_number=step.step_number,
                    success=False,
                    error_message="Action timed out",
                    execution_time_ms=self._agent_timeout * 1000
                ))
                overall_success = False
                break
            except Exception as e:
                step_results.append(ActionStepResult(
                    step_number=step.step_number,
                    success=False,
                    error_message=str(e),
                    execution_time_ms=0.0
                ))
                overall_success = False
                break
                
        # Generate summary
        if overall_success:
            summary = f"✅ {plan.summary} - completed successfully"
        else:
            failed_step = next((r for r in step_results if not r.success), None)
            summary = f"❌ Action failed: {failed_step.error_message if failed_step else 'Unknown error'}"
            
        # Remove from pending
        self._action_planner.cancel_plan(plan_id)
        
        return ActionResult(
            plan_id=plan_id,
            success=overall_success,
            step_results=step_results,
            summary=summary,
            links=all_links,
            rollback_available=any(r.can_rollback for r in step_results if r.success)
        )
        
    async def cancel_action_plan(self, plan_id: str) -> bool:
        """Cancel a pending action plan.
        
        Args:
            plan_id: ID of the plan to cancel
            
        Returns:
            bool: True if cancelled successfully
        """
        return self._action_planner.cancel_plan(plan_id)
        
    def get_pending_action_plan(self, plan_id: str) -> Optional[ActionPlan]:
        """Get a pending action plan by ID.
        
        Args:
            plan_id: Plan ID
            
        Returns:
            ActionPlan or None
        """
        return self._action_planner.get_pending_plan(plan_id)
        
    # =========================================================================
    # SLACK THREAD SUMMARIZATION
    # =========================================================================
    
    async def _handle_slack_thread_summary(
        self,
        thread_url: str,
        query: str,
        credentials: Credentials,
        start_time: float
    ) -> QueryResponse:
        """Handle Slack thread summarization request.
        
        Args:
            thread_url: Slack thread URL to summarize
            query: Original user query (may contain additional context)
            credentials: User credentials (not used for Slack, kept for consistency)
            start_time: Query start time
            
        Returns:
            QueryResponse: Thread summary in standard format
        """
        try:
            from services.slack_service import SlackThreadService
            
            # Initialize thread service with LLM
            thread_service = SlackThreadService(self._llm_service)
            
            # Extract optional query context (e.g., "summarize this thread about X")
            # Remove the URL from the query to get any additional context
            query_context = re.sub(
                r'https?://[\w-]+\.slack\.com/archives/[A-Z0-9]+/p\d+', 
                '', 
                query
            ).strip()
            
            # Remove common phrases to get the actual question
            for phrase in ['summarize', 'summary', 'thread', 'this', 'the', 'can you', 'please']:
                query_context = query_context.replace(phrase, '').strip()
            
            # Clean up
            query_context = query_context.strip('.,!? ')
            specific_query = query_context if len(query_context) > 3 else None
            
            logger.info(f"Summarizing Slack thread: {thread_url}")
            if specific_query:
                logger.info(f"With specific question: {specific_query}")
            
            # Summarize the thread
            result = await thread_service.summarize_thread_url(
                url=thread_url,
                query=specific_query
            )
            
            if not result.get("success"):
                # Return error as QueryResponse
                execution_time = (time.time() - start_time) * 1000
                return QueryResponse(
                    query=query,
                    response=f"❌ Failed to summarize thread: {result.get('error', 'Unknown error')}",
                    agents_triggered=[],
                    total_execution_time_ms=execution_time
                )
            
            # Build response
            execution_time = (time.time() - start_time) * 1000
            
            return QueryResponse(
                query=query,
                response=result.get("response", "No summary available"),
                agents_triggered=[
                    AgentContribution(
                        agent_name="slack",
                        relevance_score=1.0,
                        justification="Thread summarization from URL",
                        data_count=result.get("metadata", {}).get("message_count", 0),
                        execution_time_ms=execution_time
                    )
                ],
                raw_data=result.get("raw_data"),
                total_execution_time_ms=execution_time
            )
            
        except Exception as e:
            logger.error(f"Error handling Slack thread summary: {e}", exc_info=True)
            execution_time = (time.time() - start_time) * 1000
            return QueryResponse(
                query=query,
                response=f"❌ Error summarizing thread: {str(e)}",
                agents_triggered=[],
                total_execution_time_ms=execution_time
            )


# Global orchestrator instance
_orchestrator: Optional[Orchestrator] = None


async def get_orchestrator() -> Orchestrator:
    """Get or create the global orchestrator instance."""
    global _orchestrator
    
    if _orchestrator is None:
        _orchestrator = Orchestrator()
        await _orchestrator.initialize()
        
    return _orchestrator
