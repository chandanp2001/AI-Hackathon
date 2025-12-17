"""Azure OpenAI GPT-5 service wrapper with optimized prompts.

This module provides a wrapper around the Azure OpenAI API with:
- Intent classification for smart routing
- Chain-of-thought relevance scoring
- Few-shot response synthesis
- Structured output parsing
- Current date/time awareness for temporal queries
- Dynamic date/time extraction from natural language queries
"""

import json
import logging
import asyncio
from datetime import datetime
from typing import Any, Optional, TypeVar, Type
from pydantic import BaseModel, ValidationError
from openai import AsyncAzureOpenAI, APIError, RateLimitError, APIConnectionError

from config import settings
from services.llm.prompts import (
    get_relevance_prompt,
    get_synthesis_prompt,
    get_intent_classification_prompt,
    get_clarification_prompt,
    get_current_datetime_context,
    get_date_extraction_prompt,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMServiceError(Exception):
    """Custom exception for LLM service errors."""
    
    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.original_error = original_error


class QueryIntent(BaseModel):
    """Parsed query intent from classification with action support."""
    # Core fields
    primary_intent: str
    sources_needed: list[str]
    time_reference: Optional[str] = None
    entities: list[str] = []
    complexity: str = "simple"
    # Action-related fields
    query_type: str = "read"  # read, action, or workflow
    action_type: Optional[str] = None  # create_event, send_email, etc.
    requires_confirmation: bool = True


class RelevanceResult(BaseModel):
    """Parsed relevance scoring result."""
    reasoning: str
    score: float
    confidence: str
    retrieval_plan: Optional[str] = None
    search_terms: list[str] = []
    date_range: Optional[dict[str, Any]] = None


class DateRangeResult(BaseModel):
    """Parsed date range extraction result."""
    has_date_reference: bool
    time_reference_type: str  # specific_date, specific_range, relative_day, relative_range, none
    original_reference: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    confidence: str = "medium"


class OpenAIService:
    """Optimized Azure OpenAI GPT-5 service.
    
    Features:
    - Intent classification for smart agent routing
    - Chain-of-thought relevance scoring
    - Few-shot response synthesis
    - Request timeout handling
    - Structured output parsing
    """
    
    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        deployment: Optional[str] = None,
        api_version: Optional[str] = None
    ):
        self.endpoint = endpoint or settings.azure_openai_endpoint
        self.api_key = api_key or settings.azure_openai_api_key
        self.deployment = deployment or settings.azure_openai_deployment
        self.api_version = api_version or settings.azure_openai_api_version
        
        self.client = AsyncAzureOpenAI(
            azure_endpoint=self.endpoint,
            api_key=self.api_key,
            api_version=self.api_version
        )
        
        # Default timeouts - increased for Azure OpenAI reliability and complex queries
        self._default_timeout = 90.0
        self._quick_timeout = 45.0
        
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        timeout: Optional[float] = None
    ) -> str:
        """Generate a text response from GPT-5 with timeout handling."""
        timeout = timeout or self._default_timeout
        
        try:
            messages = []
            
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
                
            messages.append({"role": "user", "content": prompt})
            
            response = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=self.deployment,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature
                ),
                timeout=timeout
            )
            
            return response.choices[0].message.content
            
        except asyncio.TimeoutError:
            logger.error(f"LLM request timed out after {timeout}s")
            raise LLMServiceError(f"Request timed out after {timeout} seconds")
        except RateLimitError as e:
            logger.error(f"Rate limit exceeded: {e}")
            raise LLMServiceError("Rate limit exceeded, please retry later", e)
        except APIConnectionError as e:
            logger.error(f"Connection error: {e}")
            raise LLMServiceError("Failed to connect to Azure OpenAI API", e)
        except APIError as e:
            logger.error(f"API error: {e}")
            raise LLMServiceError(f"Azure OpenAI API error: {str(e)}", e)
            
    def _parse_json_response(self, response: str) -> dict:
        """Parse JSON from LLM response, handling markdown code blocks."""
        cleaned = response.strip()
        
        # Remove markdown code blocks if present
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # Remove first line (```json) and last line (```)
            if lines[-1].strip() == "```":
                cleaned = "\n".join(lines[1:-1])
            else:
                cleaned = "\n".join(lines[1:])
            cleaned = cleaned.strip()
            
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON: {cleaned[:200]}...")
            raise LLMServiceError(f"Invalid JSON response: {str(e)}")
            
    async def classify_intent(self, query: str) -> QueryIntent:
        """Classify query intent for smart agent routing.
        
        Args:
            query: User's natural language query
            
        Returns:
            QueryIntent: Classified intent with sources needed
        """
        prompt = get_intent_classification_prompt(query)
        
        try:
            response = await self.generate(
                prompt=prompt,
                temperature=0.1,  # Low temperature for consistent classification
                max_tokens=500,
                timeout=self._quick_timeout
            )
            
            data = self._parse_json_response(response)
            return QueryIntent(**data)
            
        except Exception as e:
            logger.warning(f"Intent classification failed: {e}, defaulting to multi_source")
            # Default to checking all sources if classification fails
            return QueryIntent(
                primary_intent="multi_source",
                sources_needed=["calendar", "email", "files"],
                complexity="moderate"
            )
            
    async def evaluate_relevance(
        self,
        query: str,
        agent_name: str,
        data_source_description: str
    ) -> dict[str, Any]:
        """Evaluate query relevance with chain-of-thought reasoning and date extraction.
        
        Args:
            query: User's natural language query
            agent_name: Name of the agent being evaluated
            data_source_description: Description of the data source (unused, using prompts.py)
            
        Returns:
            dict: Contains score, reasoning, confidence, search_terms, and date_range
        """
        prompt = get_relevance_prompt(agent_name, query)
        
        try:
            response = await self.generate(
                prompt=prompt,
                temperature=0.2,  # Low temperature for consistent scoring
                max_tokens=1000,  # Increased for date extraction
                timeout=self._quick_timeout
            )
            
            data = self._parse_json_response(response)
            
            # Parse date_range if present
            date_range = data.get("date_range")
            if date_range and isinstance(date_range, dict):
                # Validate date_range structure
                if date_range.get("start") and date_range.get("end"):
                    date_range = {
                        "start": date_range.get("start"),
                        "end": date_range.get("end"),
                        "type": date_range.get("type", "unknown")
                    }
                else:
                    date_range = None
            else:
                date_range = None
            
            # Validate and normalize the response
            return {
                "score": float(data.get("score", 0.0)),
                "justification": data.get("reasoning", "No reasoning provided"),
                "confidence": data.get("confidence", "medium"),
                "retrieval_plan": data.get("retrieval_plan", ""),
                "suggested_search_terms": data.get("search_terms", []),
                "date_range": date_range
            }
            
        except Exception as e:
            logger.warning(f"Relevance evaluation failed for {agent_name}: {e}")
            return {
                "score": 0.3,  # Default to low-moderate score
                "justification": f"Evaluation failed: {str(e)}",
                "confidence": "low",
                "suggested_search_terms": [],
                "date_range": None
            }
    
    async def extract_date_range(self, query: str) -> Optional[dict[str, Any]]:
        """Extract date/time range from a natural language query.
        
        Uses LLM to dynamically parse dates like:
        - Specific dates: "December 22nd", "Jan 15 2025"
        - Date ranges: "December 22-28", "from Monday to Friday"
        - Relative dates: "today", "tomorrow", "next week", "last month"
        
        Args:
            query: User's natural language query
            
        Returns:
            dict with start, end (ISO format strings), and type, or None if no date found
            
        Examples:
            >>> result = await service.extract_date_range("meetings on Dec 22")
            >>> result
            {"start": "2024-12-22T00:00:00", "end": "2024-12-23T00:00:00", "type": "specific_date"}
        """
        prompt = get_date_extraction_prompt(query)
        
        try:
            response = await self.generate(
                prompt=prompt,
                temperature=0.1,  # Very low for consistent date parsing
                max_tokens=500,
                timeout=self._quick_timeout
            )
            
            data = self._parse_json_response(response)
            
            # Check if a date reference was found
            if not data.get("has_date_reference", False):
                return None
                
            start_date = data.get("start_date")
            end_date = data.get("end_date")
            
            if not start_date or not end_date:
                return None
                
            return {
                "start": start_date,
                "end": end_date,
                "type": data.get("time_reference_type", "unknown"),
                "original_reference": data.get("original_reference"),
                "confidence": data.get("confidence", "medium")
            }
            
        except Exception as e:
            logger.warning(f"Date extraction failed for query '{query[:50]}...': {e}")
            return None
            
    async def synthesize_response(
        self,
        query: str,
        agent_results: list[dict[str, Any]],
        conversation_history: Optional[list[dict[str, str]]] = None
    ) -> str:
        """Synthesize a coherent response using few-shot prompting.
        
        Args:
            query: Original user query
            agent_results: List of results from triggered agents
            conversation_history: Optional previous messages for context
            
        Returns:
            str: Well-formatted, grounded response
        """
        prompt = get_synthesis_prompt(query, agent_results)
        
        # Build context-aware system prompt with datetime context
        datetime_context = get_current_datetime_context()
        base_system = f"""You are a helpful assistant with access to the user's data.

{datetime_context}

Use this date/time context to interpret relative time references in queries and format responses appropriately."""
        
        system_prompt = base_system
        if conversation_history:
            # Format conversation history for context
            history_text = "\n".join([
                f"{msg['role'].capitalize()}: {msg['content']}"
                for msg in conversation_history[-6:]  # Last 6 messages for context
            ])
            system_prompt = f"""{base_system}
            
Previous conversation context:
{history_text}

Use this context to provide more relevant and personalized responses. 
Reference previous topics if relevant to the current query."""
        
        try:
            response = await self.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.5,  # Moderate temperature for natural responses
                max_tokens=2000,
                timeout=self._default_timeout
            )
            
            return response.strip()
            
        except Exception as e:
            logger.error(f"Response synthesis failed: {e}")
            # Provide a graceful fallback
            return self._generate_fallback_response(query, agent_results)
            
    def _generate_fallback_response(
        self,
        query: str,
        agent_results: list[dict[str, Any]]
    ) -> str:
        """Generate a simple fallback response when synthesis fails."""
        successful_agents = [r for r in agent_results if r.get("data")]
        
        if not successful_agents:
            return "I wasn't able to find relevant information for your query. Please try rephrasing or ask about calendar events, emails, or files."
            
        # Simple data dump
        parts = ["Here's what I found:\n"]
        
        for result in successful_agents:
            agent_name = result.get("agent_name", "Unknown").capitalize()
            data = result.get("data", [])
            count = len(data)
            parts.append(f"**{agent_name}**: {count} item(s) found")
            
        return "\n".join(parts)
        
    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3
    ) -> T:
        """Generate a structured response validated against a Pydantic model."""
        schema = response_model.model_json_schema()
        schema_str = json.dumps(schema, indent=2)
        
        enhanced_system = f"""You must respond with valid JSON that matches this schema:

{schema_str}

IMPORTANT: Return ONLY the JSON object, no markdown formatting, no explanations."""

        if system_prompt:
            enhanced_system = f"{system_prompt}\n\n{enhanced_system}"
            
        try:
            raw_response = await self.generate(
                prompt=prompt,
                system_prompt=enhanced_system,
                max_tokens=max_tokens,
                temperature=temperature
            )
            
            data = self._parse_json_response(raw_response)
            return response_model.model_validate(data)
            
        except LLMServiceError:
            raise
        except ValidationError as e:
            logger.error(f"Validation failed: {e}")
            raise LLMServiceError(f"Response validation failed: {str(e)}", e)
        except Exception as e:
            logger.error(f"Unexpected error in structured generation: {e}")
            raise LLMServiceError(f"Unexpected error: {str(e)}", e)
            
    async def check_query_clarity(self, query: str) -> dict[str, Any]:
        """Check if a query is clear enough or needs clarification.
        
        Args:
            query: User's natural language query
            
        Returns:
            dict: Contains needs_clarification, clarity_score, suggested_questions, etc.
        """
        prompt = get_clarification_prompt(query)
        
        try:
            response = await self.generate(
                prompt=prompt,
                temperature=0.3,
                max_tokens=500,
                timeout=self._quick_timeout
            )
            
            data = self._parse_json_response(response)
            
            return {
                "needs_clarification": data.get("needs_clarification", False),
                "clarity_score": float(data.get("clarity_score", 0.8)),
                "reason": data.get("reason", ""),
                "suggested_questions": data.get("suggested_questions", []),
                "likely_sources": data.get("likely_sources", []),
                "refined_query": data.get("refined_query")
            }
            
        except Exception as e:
            logger.warning(f"Query clarity check failed: {e}")
            # Default to not needing clarification to avoid blocking
            return {
                "needs_clarification": False,
                "clarity_score": 0.5,
                "reason": "Clarity check failed, proceeding with query",
                "suggested_questions": [],
                "likely_sources": [],
                "refined_query": None
            }
            
    async def health_check(self) -> bool:
        """Check if the OpenAI service is operational."""
        try:
            await self.generate("Say 'ok'", max_tokens=10, timeout=10.0)
            return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False
            
    async def shutdown(self) -> None:
        """Clean up resources."""
        await self.client.close()
        
    def get_metrics(self) -> dict[str, Any]:
        """Get service metrics."""
        return {
            "deployment": self.deployment,
            "endpoint": self.endpoint,
            "api_version": self.api_version,
            "service": "azure_openai",
            "status": "active"
        }


# Alias for backward compatibility
LLMService = OpenAIService
