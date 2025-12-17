"""
Base class for data sources in the Engage Genius system.

This module provides the abstract base class that all data sources
(Slack, Jira, Google, etc.) must implement.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from pydantic import BaseModel


class FollowUpSuggestion(BaseModel):
    """A follow-up suggestion for narrowing down search results.
    
    Attributes:
        type: Type of suggestion (channel, time_range, user, keyword)
        value: The value to use for narrowing (e.g., channel name, time period)
        label: Human-readable label for the suggestion
        description: Optional detailed description
    """
    type: str
    value: str
    label: str
    description: Optional[str] = None


class ChannelSummary(BaseModel):
    """Summary of search results for a single channel.
    
    Attributes:
        channel_id: Slack channel ID
        channel_name: Human-readable channel name
        message_count: Number of matching messages
        relevance_score: Relevance score (0.0 to 1.0)
        top_message_preview: Preview of the most relevant message
    """
    channel_id: str
    channel_name: str
    message_count: int
    relevance_score: float
    top_message_preview: Optional[str] = None


class GlobalSearchResult(BaseModel):
    """Result from a global (channel-free) search.
    
    Attributes:
        query: The original search query
        results: Dict mapping channel names to their messages
        channel_summaries: List of channel summaries with match counts
        total_message_count: Total messages found across all channels
        channels_searched: Number of channels that were searched
        follow_up_suggestions: Suggestions for narrowing down results
        summary: Human-readable summary of findings
        processing_time_seconds: Time taken to process the search
    """
    query: str
    results: Dict[str, Any]
    channel_summaries: List[ChannelSummary]
    total_message_count: int
    channels_searched: int
    follow_up_suggestions: List[FollowUpSuggestion]
    summary: str
    processing_time_seconds: float


class DataSource(ABC):
    """Abstract base class for all data sources.
    
    This class defines the interface that all data sources must implement
    to be used in the Engage Genius system.
    
    Methods:
        validate_inputs: Validate the input parameters for a data fetch
        fetch_data: Fetch data from the source based on parameters
        format_for_llm: Format the fetched data for LLM consumption
        get_form_fields: Return form field definitions for UI configuration
    """
    
    @abstractmethod
    async def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate input parameters for data fetching.
        
        Args:
            inputs: Dictionary of input parameters
            
        Returns:
            bool: True if inputs are valid, False otherwise
            
        Raises:
            ValueError: If inputs are invalid with details
        """
        pass
    
    @abstractmethod
    async def fetch_data(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch data from the source based on parameters.
        
        Args:
            params: Dictionary of parameters for the fetch operation
            
        Returns:
            Dict containing the fetched data and metadata
            
        Raises:
            ConnectionError: If unable to connect to the data source
            PermissionError: If access is denied
        """
        pass
    
    @abstractmethod
    def format_for_llm(self, data: Dict[str, Any]) -> str:
        """Format the fetched data for LLM consumption.
        
        Args:
            data: The raw data fetched from the source
            
        Returns:
            str: Formatted string suitable for LLM processing
        """
        pass
    
    @abstractmethod
    def get_form_fields(self) -> Dict[str, Any]:
        """Return form field definitions for UI configuration.
        
        Returns:
            Dict containing form field specifications
        """
        pass
    
    async def global_search(self, query: str, params: Optional[Dict[str, Any]] = None) -> GlobalSearchResult:
        """Perform a global search across all available resources.
        
        This is an optional method that data sources can implement
        to support searching without specifying a specific resource
        (e.g., searching all Slack channels).
        
        Args:
            query: The search query string
            params: Optional additional parameters
            
        Returns:
            GlobalSearchResult: Results grouped by resource with suggestions
            
        Raises:
            NotImplementedError: If the data source doesn't support global search
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support global search"
        )
    
    async def health_check(self) -> Dict[str, Any]:
        """Check if the data source connection is healthy.
        
        Returns:
            Dict with health status information
        """
        return {"status": "unknown", "message": "Health check not implemented"}
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get metrics about the data source usage.
        
        Returns:
            Dict with usage metrics
        """
        return {"metrics": "not_available"}

