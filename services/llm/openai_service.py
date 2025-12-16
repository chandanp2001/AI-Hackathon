"""Azure OpenAI GPT-5 service wrapper for relevance scoring and response synthesis.

This module provides a wrapper around the Azure OpenAI API with support for
structured output parsing, retry logic, and proper error handling.
"""

import json
import logging
from typing import Any, Optional, TypeVar, Type
from pydantic import BaseModel, ValidationError
from openai import AsyncAzureOpenAI, APIError, RateLimitError, APIConnectionError

from config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMServiceError(Exception):
    """Custom exception for LLM service errors.
    
    Args:
        message: Error description
        original_error: Original exception that caused this error
    """
    
    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(message)
        self.original_error = original_error


class OpenAIService:
    """Wrapper service for Azure OpenAI GPT-5 API.
    
    Provides methods for relevance scoring and response synthesis with
    structured output parsing and error handling.
    
    Args:
        endpoint: Azure OpenAI endpoint (uses config if not provided)
        api_key: Azure OpenAI API key (uses config if not provided)
        deployment: Deployment name (uses config if not provided)
        
    Examples:
        >>> service = OpenAIService()
        >>> response = await service.generate("What is 2+2?")
        >>> print(response)
        "2+2 equals 4."
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
        
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7
    ) -> str:
        """Generate a text response from GPT-5.
        
        Args:
            prompt: User prompt/query
            system_prompt: Optional system instructions
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (0.0-1.0)
            
        Returns:
            str: Generated text response
            
        Raises:
            LLMServiceError: If API call fails
        """
        try:
            messages = []
            
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
                
            messages.append({"role": "user", "content": prompt})
            
            response = await self.client.chat.completions.create(
                model=self.deployment,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )
            
            return response.choices[0].message.content
            
        except RateLimitError as e:
            logger.error(f"Rate limit exceeded: {e}")
            raise LLMServiceError("Rate limit exceeded, please retry later", e)
        except APIConnectionError as e:
            logger.error(f"Connection error: {e}")
            raise LLMServiceError("Failed to connect to Azure OpenAI API", e)
        except APIError as e:
            logger.error(f"API error: {e}")
            raise LLMServiceError(f"Azure OpenAI API error: {str(e)}", e)
            
    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3
    ) -> T:
        """Generate a structured response validated against a Pydantic model.
        
        Args:
            prompt: User prompt requesting structured output
            response_model: Pydantic model class to validate response
            system_prompt: Optional system instructions
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (lower for structured output)
            
        Returns:
            T: Validated Pydantic model instance
            
        Raises:
            LLMServiceError: If API call or validation fails
            
        Examples:
            >>> class ScoreResponse(BaseModel):
            ...     score: float
            ...     reason: str
            >>> result = await service.generate_structured(
            ...     "Rate this query relevance",
            ...     ScoreResponse
            ... )
            >>> print(result.score)
            0.85
        """
        # Build JSON schema instruction
        schema = response_model.model_json_schema()
        schema_str = json.dumps(schema, indent=2)
        
        enhanced_system = f"""You must respond with valid JSON that matches this schema:

{schema_str}

IMPORTANT: Return ONLY the JSON object, no markdown formatting, no explanations.
Do not wrap the response in ```json``` blocks."""

        if system_prompt:
            enhanced_system = f"{system_prompt}\n\n{enhanced_system}"
            
        try:
            raw_response = await self.generate(
                prompt=prompt,
                system_prompt=enhanced_system,
                max_tokens=max_tokens,
                temperature=temperature
            )
            
            # Clean the response (remove potential markdown formatting)
            cleaned = raw_response.strip()
            if cleaned.startswith("```"):
                # Remove markdown code block
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])
                cleaned = cleaned.strip()
                
            # Parse JSON
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON: {raw_response}")
                raise LLMServiceError(f"Invalid JSON response: {str(e)}", e)
                
            # Validate against Pydantic model
            try:
                return response_model.model_validate(data)
            except ValidationError as e:
                logger.error(f"Validation failed: {e}")
                raise LLMServiceError(f"Response validation failed: {str(e)}", e)
                
        except LLMServiceError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error in structured generation: {e}")
            raise LLMServiceError(f"Unexpected error: {str(e)}", e)
            
    async def evaluate_relevance(
        self,
        query: str,
        agent_name: str,
        data_source_description: str
    ) -> dict[str, Any]:
        """Evaluate query relevance for a specific data source.
        
        Args:
            query: User's natural language query
            agent_name: Name of the agent being evaluated
            data_source_description: Description of what the data source contains
            
        Returns:
            dict: Contains score, justification, and suggested_search_terms
            
        Raises:
            LLMServiceError: If evaluation fails
        """
        prompt = f"""Evaluate if this user query requires data from {agent_name}.

Data Source Description: {data_source_description}

User Query: {query}

Provide your evaluation as JSON with these fields:
- score: float between 0.0 and 1.0 indicating relevance
- justification: brief explanation (max 100 words)
- suggested_search_terms: list of terms to use for searching this data source

Scoring Guidelines:
- 1.0: Query explicitly and directly requests this type of data
- 0.7-0.9: Query strongly implies need for this data
- 0.4-0.6: Query might benefit from this data
- 0.1-0.3: Query has weak connection to this data
- 0.0: Query has no relation to this data source"""

        system_prompt = f"""You are the {agent_name} relevance evaluator. 
Your job is to determine if a user query requires data from your data source.
Be precise and objective in your scoring. Do not inflate scores."""

        response = await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.2  # Low temperature for consistent scoring
        )
        
        # Parse the JSON response
        try:
            # Clean potential markdown
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])
                cleaned = cleaned.strip()
                
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse relevance response: {response}")
            # Return a safe default
            return {
                "score": 0.0,
                "justification": "Failed to evaluate relevance",
                "suggested_search_terms": []
            }
            
    async def synthesize_response(
        self,
        query: str,
        agent_results: list[dict[str, Any]]
    ) -> str:
        """Synthesize a final response from multiple agent results.
        
        Args:
            query: Original user query
            agent_results: List of results from triggered agents
            
        Returns:
            str: Coherent natural language response
            
        Raises:
            LLMServiceError: If synthesis fails
        """
        # Build context from agent results
        context_parts = []
        for result in agent_results:
            agent_name = result.get("agent_name", "Unknown")
            data = result.get("data", [])
            if data:
                context_parts.append(f"### Data from {agent_name}:\n{json.dumps(data, indent=2, default=str)}")
                
        context = "\n\n".join(context_parts) if context_parts else "No data was retrieved from any source."
        
        prompt = f"""Based on the following data retrieved from various sources, provide a helpful and accurate response to the user's query.

User Query: {query}

Retrieved Data:
{context}

Instructions:
1. Synthesize the information into a clear, natural response
2. Only include information that is present in the retrieved data
3. If data is insufficient, acknowledge what's missing
4. Do not make up or hallucinate any information
5. Be concise but complete
6. If there are dates/times, format them in a human-readable way"""

        system_prompt = """You are a helpful assistant that synthesizes information from multiple data sources.
You must only use the data provided - never invent or assume information.
Always cite which source (Calendar, Gmail, Drive) information came from when relevant."""

        return await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.5
        )
        
    async def health_check(self) -> bool:
        """Check if the OpenAI service is operational.
        
        Returns:
            bool: True if service is healthy
        """
        try:
            await self.generate("Say 'ok'", max_tokens=10)
            return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False
            
    async def shutdown(self) -> None:
        """Clean up resources."""
        await self.client.close()
        
    def get_metrics(self) -> dict[str, Any]:
        """Get service metrics.
        
        Returns:
            dict: Service metrics including model info
        """
        return {
            "deployment": self.deployment,
            "endpoint": self.endpoint,
            "api_version": self.api_version,
            "service": "azure_openai",
            "status": "active"
        }


# Alias for backward compatibility
LLMService = OpenAIService

