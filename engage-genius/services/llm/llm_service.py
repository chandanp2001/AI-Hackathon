import aiohttp
import logging
import json
import time
import asyncio
import os
import requests
from typing import Dict, Any, Optional, List
from config import OPENAI_API_KEY, OPENAI_API_URL, LLM_MODEL, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION

class LLMService:
    """Service for interacting with language models"""
    
    def __init__(self):
        self.api_key = OPENAI_API_KEY
        self.api_url = OPENAI_API_URL
        self.model = LLM_MODEL
        self.azure_endpoint = AZURE_OPENAI_ENDPOINT
        self.azure_deployment = AZURE_OPENAI_DEPLOYMENT
        self.azure_api_version = AZURE_OPENAI_API_VERSION
        self.logger = logging.getLogger(__name__)
        self.max_retries = 3
        self.retry_delay = 1  # seconds
        
    async def generate_completion(self, prompt: str, max_tokens: int = 2000, temperature: float = 0.7) -> str:
        """Generate a completion using the configured LLM with improved error handling and retries"""
        retry_count = 0
        last_error = None
        
        try:
            # Detect data sources and fields in the prompt
            data_sources_count = 0
            data_fields_count = 0
            has_slack = 'SLACK:' in prompt
            has_jira = 'JIRA:' in prompt
            has_google_docs = 'GOOGLE DOCS:' in prompt
            has_google_sheets = 'GOOGLE SHEETS:' in prompt
            
            # Count total data sources
            if has_slack: data_sources_count += 1
            if has_jira: data_sources_count += 1 
            if has_google_docs: data_sources_count += 1
            if has_google_sheets: data_sources_count += 1
            
            # Count channels for Slack
            channel_count = prompt.count("Channel: #")
            
            # Count data fields
            data_fields_count += prompt.count("messages") + prompt.count("threads")  # Slack fields
            data_fields_count += prompt.count("issues") + prompt.count("status")  # JIRA fields
            data_fields_count += prompt.count("document") + prompt.count("heading")  # Docs fields
            data_fields_count += prompt.count("cell") + prompt.count("table")  # Sheets fields
            
            # Check for relevance scores to use in token distribution
            relevance_scores_present = "relevance score" in prompt.lower() or "relevance:" in prompt.lower()
            
            # Dynamic token allocation based on sources, fields and complexity
            adjusted_max_tokens = max_tokens
            
            # Base increase for multiple sources
            if data_sources_count > 1:
                # Start with high token count for multiple sources
                source_tokens = 2000 + (500 * data_sources_count)
                adjusted_max_tokens = max(adjusted_max_tokens, source_tokens)
                self.logger.info(f"Adjusted tokens for {data_sources_count} sources: {adjusted_max_tokens}")
            
            # Additional tokens for multiple data fields within sources
            if data_fields_count > 2:
                # Add tokens for complex field analysis
                field_tokens = adjusted_max_tokens + (200 * data_fields_count)
                adjusted_max_tokens = min(4000, field_tokens)  # Cap at 4000 tokens
                self.logger.info(f"Adjusted tokens for {data_fields_count} data fields: {adjusted_max_tokens}")
            
            # Special handling for multi-channel Slack data
            if has_slack and channel_count > 1:
                channel_tokens = 2000 + (300 * channel_count)
                adjusted_max_tokens = max(adjusted_max_tokens, channel_tokens)
                self.logger.info(f"Adjusted tokens for {channel_count} Slack channels: {adjusted_max_tokens}")
            
            # Final cap to prevent excessive costs
            adjusted_max_tokens = min(4000, adjusted_max_tokens)
            
            # Create system message with intelligent token distribution guidance
            system_message = "You are a helpful assistant specializing in data analysis."
            
            if relevance_scores_present and data_sources_count > 1:
                system_message += """
                
When analyzing multiple data sources with relevance scores:
1. Distribute your response length proportionally to relevance scores
2. Give more detail for higher relevance sources and fields
3. For lower relevance sources, provide key points only
4. Ensure all relevant sources are addressed, but prioritize by relevance
"""
            
            # Azure OpenAI headers
            headers = {
                "Content-Type": "application/json",
                "api-key": self.api_key
            }
            
            # Construct Azure OpenAI URL with API version
            azure_url = f"{self.azure_endpoint}openai/deployments/{self.azure_deployment}/chat/completions?api-version={self.azure_api_version}"
            
            # Debug logging to check actual values being used
            self.logger.info(f"Azure OpenAI Configuration Debug:")
            self.logger.info(f"  Endpoint: {self.azure_endpoint}")
            self.logger.info(f"  Deployment: {self.azure_deployment}")
            self.logger.info(f"  API Version: {self.azure_api_version}")
            self.logger.info(f"  Full URL: {azure_url}")
            self.logger.info(f"  API Key (first 10 chars): {self.api_key[:10]}...")
            
            payload = {
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": adjusted_max_tokens,
                "temperature": temperature
            }
            
            self.logger.info(f"Sending request to Azure OpenAI API with {len(prompt)} chars prompt, max_tokens={adjusted_max_tokens}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(azure_url, headers=headers, json=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        self.logger.error(f"LLM API error: {response.status} - {error_text}")
                        return f"Error: API returned status code {response.status}"
                    
                    result = await response.json()
                    
                    # Extract the completion text
                    if "choices" in result and len(result["choices"]) > 0:
                        completion = result["choices"][0]["message"]["content"].strip()
                        
                        # Log token usage
                        if "usage" in result:
                            usage = result["usage"]
                            self.logger.info(
                                f"LLM API usage: prompt_tokens={usage.get('prompt_tokens', 'unknown')}, "
                                f"completion_tokens={usage.get('completion_tokens', 'unknown')}, "
                                f"total_tokens={usage.get('total_tokens', 'unknown')}"
                            )
                        
                        return completion
                    else:
                        self.logger.error(f"Unexpected API response format: {json.dumps(result)}")
                        return "Error: Unexpected API response format"
        
        except Exception as e:
            self.logger.error(f"Error generating completion: {str(e)}", exc_info=True)
            return f"Error generating completion: {str(e)}"
    async def analyze_data(self, data: Dict[str, Any], query: str, max_tokens: int = 1500) -> str:
        """Analyze data with a specific query"""
        try:
            # Format data as a string for inclusion in the prompt
            data_str = json.dumps(data, indent=2)
            
            # Build prompt
            prompt = f"""
            I need you to analyze the following data based on this query: "{query}"
            
            Data:
            {data_str}
            
            Please provide a comprehensive analysis addressing the query.
            """
            
            return await self.generate_completion(prompt, max_tokens)
            
        except Exception as e:
            self.logger.error(f"Error analyzing data: {str(e)}", exc_info=True)
            return f"Error analyzing data: {str(e)}"

# Add functions from openai_service.py
def generate_ai_analysis(prompt: str, max_tokens: int = 2000, temperature: float = 0.7) -> str:
    """
    Generate AI analysis using the Azure OpenAI API (purely synchronous implementation with retries)
    
    Args:
        prompt: The prompt to send to the model
        max_tokens: Maximum tokens in the response
        temperature: Temperature setting for response generation
        
    Returns:
        Generated analysis text
    """
    retries = 0
    backoff_delay = 1
    max_retries = 3
    logger = logging.getLogger(__name__)
    
    while retries <= max_retries:
        try:
            if retries > 0:
                logger.info(f"Retry attempt {retries} of {max_retries} for synchronous Azure OpenAI API call")
                time.sleep(backoff_delay)  # Sleep between retries with exponential backoff
            
            # System message to configure model behavior
            system_message = """You are an AI assistant specialized in analyzing data from various sources.
            Focus on extracting valuable insights and presenting them in a clear, concise manner.
            Prioritize accuracy and relevance in your analysis."""
            
            # Use imported config values instead of environment variables directly
            api_key = OPENAI_API_KEY
            azure_endpoint = AZURE_OPENAI_ENDPOINT
            azure_deployment = AZURE_OPENAI_DEPLOYMENT
            azure_api_version = AZURE_OPENAI_API_VERSION
            
            # Construct Azure OpenAI URL
            api_url = f"{azure_endpoint}openai/deployments/{azure_deployment}/chat/completions?api-version={azure_api_version}"
            
            # Debug logging to check actual values being used
            logger.info(f"Synchronous Azure OpenAI Configuration Debug:")
            logger.info(f"  Endpoint: {azure_endpoint}")
            logger.info(f"  Deployment: {azure_deployment}")
            logger.info(f"  API Version: {azure_api_version}")
            logger.info(f"  Full URL: {api_url}")
            logger.info(f"  API Key (first 10 chars): {api_key[:10]}...")
            
            # Azure OpenAI headers
            headers = {
                "Content-Type": "application/json",
                "api-key": api_key
            }
            
            payload = {
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": max_tokens,
                "temperature": temperature
            }
            
            logger.info(f"Sending synchronous request to Azure OpenAI API with {len(prompt)} chars prompt")
            
            # Make a synchronous request using the requests library
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=60  # 60 second timeout
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                logger.info(f"Successfully generated completion ({len(content)} chars)")
                return content
            elif response.status_code in [429, 500, 502, 503, 504]:
                # These error codes indicate we should retry
                error_text = response.text
                logger.error(f"Azure OpenAI API error on attempt {retries+1}: Status {response.status_code}, {error_text}")
                retries += 1
                # Exponential backoff
                backoff_delay = 2 ** retries
                continue
            else:
                error_text = response.text
                logger.error(f"Azure OpenAI API error: Status {response.status_code}, {error_text}")
                return f"Error: Failed to generate completion. Status code: {response.status_code}"
                
        except requests.exceptions.Timeout:
            logger.error(f"Timeout during synchronous API call (attempt {retries+1})")
            retries += 1
            backoff_delay = 2 ** retries
            if retries > max_retries:
                return "Error: API request timed out after multiple attempts."
        
        except requests.exceptions.ConnectionError:
            logger.error(f"Connection error during synchronous API call (attempt {retries+1})")
            retries += 1
            backoff_delay = 2 ** retries
            if retries > max_retries:
                return "Error: Failed to connect to API after multiple attempts."
                
        except Exception as e:
            logger.error(f"Error in generate_ai_analysis (attempt {retries+1}): {str(e)}")
            retries += 1
            backoff_delay = 2 ** retries
            if retries > max_retries:
                return f"Error: {str(e)}"
    
    return "Error: All retry attempts failed for AI analysis generation."

def generate_ai_analysis_batch(prompts: List[str], max_tokens: int = 2000, temperature: float = 0.7) -> List[str]:
    """
    Generate AI analyses for multiple prompts in batch using a sequential approach
    
    Args:
        prompts: List of prompts to process
        max_tokens: Maximum tokens in each response
        temperature: Temperature setting for response generation
        
    Returns:
        List of generated analysis texts
    """
    logger = logging.getLogger(__name__)
    try:
        # Simple sequential approach
        results = []
        
        for i, prompt in enumerate(prompts):
            try:
                logger.info(f"Processing batch prompt {i+1}/{len(prompts)}")
                result = generate_ai_analysis(prompt, max_tokens, temperature)
                results.append(result)
            except Exception as e:
                logger.error(f"Error processing prompt {i+1}: {str(e)}")
                results.append(f"Error: {str(e)}")
        
        return results
        
    except Exception as e:
        logger.error(f"Error in generate_ai_analysis_batch: {str(e)}")
        # Return error messages for all prompts
        return [f"Error: {str(e)}"] * len(prompts) 