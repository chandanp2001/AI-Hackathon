from abc import ABC, abstractmethod
from typing import Dict, Any, List, Union, Optional
import json
import logging
import abc

class DataSource(ABC):
    """Base class for all data sources"""
    
    @abstractmethod
    async def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate the inputs for this data source"""
        pass
        
    @abstractmethod
    async def fetch_data(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch data from the source"""
        pass
        
    @abstractmethod
    def format_for_llm(self, data: Dict[str, Any]) -> str:
        """Format the data for LLM consumption"""
        pass
        
    @abstractmethod
    def get_form_fields(self) -> Dict[str, Any]:
        """Get form fields for data source configuration."""
        pass

    def _format_with_metadata_protocol(self, data: Dict[str, Any], source_type: str) -> str:
        """Format data using metadata control protocol for better LLM understanding.
        
        This method provides consistent formatting across different data sources,
        helping the LLM better understand and utilize the structured data.
        
        Args:
            data: The data to format
            source_type: Type of data source (e.g., "SLACK", "GOOGLE_DOCS")
            
        Returns:
            Formatted string with metadata control markers
        """
        if not data or (isinstance(data, dict) and 'error' in data):
            return f"Error retrieving {source_type} data: {data.get('error', 'Unknown error')}"
            
        formatted = []
        formatted.append(f"# {source_type} DATA")
        formatted.append(f"The following sections contain data from {source_type}.")
        
        # Add global metadata if available
        if isinstance(data, dict) and 'metadata' in data:
            formatted.append("\n```metadata")
            for key, value in data['metadata'].items():
                formatted.append(f"{key}: {value}")
            formatted.append("```\n")
        
        # Add content sections - to be implemented by subclasses
        # This is a base template that subclasses can use
        
        return "\n".join(formatted)
    
    def _format_metadata_section(self, metadata: Dict[str, Any], section_name: Optional[str] = None) -> str:
        """Format a metadata section with proper markers."""
        lines = []
        if section_name:
            lines.append(f"\n### {section_name}")
        
        lines.append("```metadata")
        for key, value in metadata.items():
            # Handle different value types
            if isinstance(value, (list, tuple)):
                lines.append(f"{key}: {', '.join(str(v) for v in value)}")
            elif isinstance(value, dict):
                for k, v in value.items():
                    lines.append(f"{key}.{k}: {v}")
            else:
                lines.append(f"{key}: {value}")
        lines.append("```")
        
        return "\n".join(lines)
    
    def _format_data_section(self, data: Any, data_type: str, section_name: Optional[str] = None) -> str:
        """Format a data section with proper markers."""
        lines = []
        if section_name:
            lines.append(f"\n### {section_name}")
        
        lines.append(f"```{data_type}")
        
        if isinstance(data, (list, tuple)):
            for item in data:
                lines.append(str(item))
        elif isinstance(data, dict):
            for key, value in data.items():
                lines.append(f"{key}: {value}")
        else:
            lines.append(str(data))
            
        lines.append("```")
        
        return "\n".join(lines)
    
    def _format_error(self, error_message: str) -> str:
        """Format an error message."""
        return f"```error\n{error_message}\n```"
        
    # Legacy formatting methods for backward compatibility
    def _format_section(self, title: str) -> str:
        """Format a section header."""
        return f"\n## {title}\n"
        
    def _format_key_value(self, data: Dict[str, Any], title: Optional[str] = None) -> str:
        """Format key-value data."""
        result = []
        if title:
            result.append(f"### {title}:")
        for key, value in data.items():
            result.append(f"- {key}: {value}")
        return "\n".join(result) + "\n"
        
    def _format_subsection(self, title: str, content: str) -> str:
        """Format a subsection with content."""
        return f"### {title}:\n{content}\n"
        
    def _format_table(self, headers: List[str], rows: List[List[str]], title: Optional[str] = None) -> str:
        """Format data as a table."""
        result = []
        if title:
            result.append(f"### {title}")
        
        # Format header row
        header_row = "| " + " | ".join(headers) + " |"
        result.append(header_row)
        
        # Format separator
        separator = "|" + "|".join(["-" * len(h) for h in headers]) + "|"
        result.append(separator)
        
        # Format data rows, limited to 30 rows to avoid token overflow
        display_rows = rows[:30]
        for row in display_rows:
            row_data = "| " + " | ".join(row) + " |"
            result.append(row_data)
        
        if len(rows) > 30:
            result.append(f"... {len(rows) - 30} more rows (truncated)")
            
        return "\n".join(result) + "\n"
        
    def _format_metrics(self, metrics: Dict[str, Any], title: Optional[str] = None) -> str:
        """Format metrics data."""
        return self._format_key_value(metrics, title)
        
    def _format_summary(self, summary: Dict[str, List[str]]) -> str:
        """Format a summary section."""
        result = []
        result.append("### Summary:")
        
        for section, items in summary.items():
            result.append(f"#### {section.capitalize()}:")
            for item in items:
                result.append(f"- {item}")
                
        return "\n".join(result) + "\n"

class DataSourceRegistry:
    """Registry for data sources"""
    
    def __init__(self):
        self._sources = {}
        
    def register(self, name: str, source: DataSource):
        """Register a data source"""
        self._sources[name] = source
        
    def get_source(self, name: str) -> DataSource:
        """Get a data source by name"""
        return self._sources.get(name)
        
    def get_sources(self) -> List[str]:
        """Get list of registered source names"""
        return list(self._sources.keys()) 