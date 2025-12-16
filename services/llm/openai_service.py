"""Azure OpenAI GPT-5 service wrapper with optimized prompts.

This module provides a wrapper around the Azure OpenAI API with:
- Intent classification for smart routing
- Chain-of-thought relevance scoring
- Few-shot response synthesis
- Structured output parsing
"""

import json
import logging
import asyncio
from typing import Any, Optional, TypeVar, Type
from pydantic import BaseModel, ValidationError
from openai import AsyncAzureOpenAI, APIError, RateLimitError, APIConnectionError

from config import settings
from services.llm.prompts import (
    get_relevance_prompt,
    get_synthesis_prompt,
    get_intent_classification_prompt,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMServiceError(Exception):
    """Custom exception for LLM service errors."""
    
    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.original_error = original_error


class QueryIntent(BaseModel):
    """Parsed query intent from classification."""
    primary_intent: str
    sources_needed: list[str]
    time_reference: Optional[str] = None
    entities: list[str] = []
    complexity: str = "simple"


class RelevanceResult(BaseModel):
    """Parsed relevance scoring result."""
    reasoning: str
    score: float
    confidence: str
    retrieval_plan: Optional[str] = None
    search_terms: list[str] = []


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
        
        # Default timeouts
        self._default_timeout = 30.0
        self._quick_timeout = 10.0
        
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
        """Evaluate query relevance with chain-of-thought reasoning.
        
        Args:
            query: User's natural language query
            agent_name: Name of the agent being evaluated
            data_source_description: Description of the data source (unused, using prompts.py)
            
        Returns:
            dict: Contains score, reasoning, confidence, and search_terms
        """
        prompt = get_relevance_prompt(agent_name, query)
        
        try:
            response = await self.generate(
                prompt=prompt,
                temperature=0.2,  # Low temperature for consistent scoring
                max_tokens=800,
                timeout=self._quick_timeout
            )
            
            data = self._parse_json_response(response)
            
            # Validate and normalize the response
            return {
                "score": float(data.get("score", 0.0)),
                "justification": data.get("reasoning", "No reasoning provided"),
                "confidence": data.get("confidence", "medium"),
                "retrieval_plan": data.get("retrieval_plan", ""),
                "suggested_search_terms": data.get("search_terms", [])
            }
            
        except Exception as e:
            logger.warning(f"Relevance evaluation failed for {agent_name}: {e}")
            return {
                "score": 0.3,  # Default to low-moderate score
                "justification": f"Evaluation failed: {str(e)}",
                "confidence": "low",
                "suggested_search_terms": []
            }
            
    async def synthesize_response(
        self,
        query: str,
        agent_results: list[dict[str, Any]]
    ) -> str:
        """Synthesize a coherent response using few-shot prompting.
        
        Args:
            query: Original user query
            agent_results: List of results from triggered agents
            
        Returns:
            str: Well-formatted, grounded response
        """
        prompt = get_synthesis_prompt(query, agent_results)
        
        try:
            response = await self.generate(
                prompt=prompt,
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
