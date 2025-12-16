from typing import Dict, Any, List, Optional
import logging
from .context_manager import ContextManager
from services.data_sources.base import DataSourceRegistry

class GoogleContextHandler(ContextManager):
    """Specialized context handler for Google data sources with metadata protocol"""
    
    def __init__(self, source_registry: DataSourceRegistry, llm_service):
        super().__init__(llm_service)
        self.source_registry = source_registry
        
    def _get_source_instance(self, source_name: str):
        """Get the source instance from the registry"""
        try:
            return self.source_registry.get_source(source_name)
        except ValueError:
            logging.warning(f"Source not found in registry: {source_name}")
            return None
    
    async def enhance_google_docs_format(self, docs_data: Dict[str, Any], query: str) -> Dict[str, Any]:
        """Enhance Google Docs data with additional metadata and structure"""
        enhanced_data = {}
        
        # Add global metadata
        enhanced_data['metadata'] = {
            'source_type': 'google_docs',
            'query': query,
            'document_count': len(docs_data) if isinstance(docs_data, dict) else 0
        }
        
        # Process each document
        if isinstance(docs_data, dict):
            for doc_name, doc_content in docs_data.items():
                if 'error' in doc_content:
                    enhanced_data[doc_name] = doc_content
                    continue
                    
                # Add document metadata
                enhanced_data[doc_name] = {
                    'title': doc_content.get('title', doc_name),
                    'last_modified': doc_content.get('last_modified', 'Unknown'),
                    'content': doc_content.get('content', ''),
                    'metadata': {
                        'file_type': 'document',
                        'format': 'text/html'
                    }
                }
                
                # Extract and classify document sections
                if 'content' in doc_content:
                    content = doc_content['content']
                    source = self.source_registry.get_source('google_docs')
                    if source and hasattr(source, '_split_into_sections'):
                        sections = source._split_into_sections(content)
                        enhanced_data[doc_name]['sections'] = sections
                        
                        # Identify query-relevant sections
                        relevant_sections = []
                        for section in sections:
                            if any(term in section.lower() for term in query.lower().split()):
                                relevant_sections.append(section)
                        
                        if relevant_sections:
                            enhanced_data[doc_name]['relevant_sections'] = relevant_sections
        
        return enhanced_data
    
    async def enhance_google_sheets_format(self, sheets_data: Dict[str, Any], query: str) -> Dict[str, Any]:
        """Enhance Google Sheets data with additional metadata and insights"""
        enhanced_data = {}
        
        # Add global metadata
        enhanced_data['metadata'] = {
            'source_type': 'google_sheets',
            'query': query,
            'spreadsheet_count': len(sheets_data) if isinstance(sheets_data, dict) else 0
        }
        
        # Process each spreadsheet
        if isinstance(sheets_data, dict):
            for sheet_id, sheet_content in sheets_data.items():
                if 'error' in sheet_content:
                    enhanced_data[sheet_id] = sheet_content
                    continue
                
                # Add spreadsheet metadata
                enhanced_data[sheet_id] = {
                    'title': sheet_content.get('title', sheet_id),
                    'sheets': {},
                    'metadata': {
                        'file_type': 'spreadsheet',
                        'format': 'tabular'
                    }
                }
                
                # Process individual sheets
                for tab_name, tab_data in sheet_content.get('sheets', {}).items():
                    if 'error' in tab_data:
                        enhanced_data[sheet_id]['sheets'][tab_name] = tab_data
                        continue
                    
                    # Extract headers and data
                    headers = tab_data.get('headers', [])
                    data = tab_data.get('data', [])
                    
                    # Add to enhanced data
                    enhanced_data[sheet_id]['sheets'][tab_name] = {
                        'headers': headers,
                        'data': data
                    }
                    
                    # Add sheet insights if available
                    source = self.source_registry.get_source('google_sheets')
                    if source and hasattr(source, '_extract_metrics') and headers and data:
                        metrics = source._extract_metrics(headers, data)
                        enhanced_data[sheet_id]['sheets'][tab_name]['metrics'] = metrics
                    
                    if source and hasattr(source, '_detect_patterns') and headers and data:
                        patterns = source._detect_patterns(headers, data)
                        if patterns:
                            enhanced_data[sheet_id]['sheets'][tab_name]['insights'] = patterns
        
        return enhanced_data
    
    async def generate_google_analysis(self, 
                                      docs_data: Optional[Dict[str, Any]] = None, 
                                      sheets_data: Optional[Dict[str, Any]] = None, 
                                      query: str = "") -> str:
        """Generate analysis from Google Docs and/or Sheets data using metadata protocol"""
        source_data = {}
        
        # Process Google Docs data if available
        if docs_data:
            enhanced_docs = await self.enhance_google_docs_format(docs_data, query)
            source_data['google_docs'] = enhanced_docs
            
        # Process Google Sheets data if available
        if sheets_data:
            enhanced_sheets = await self.enhance_google_sheets_format(sheets_data, query)
            source_data['google_sheets'] = enhanced_sheets
        
        # Generate the analysis using the base context manager
        return await self.generate_analysis(source_data, query) 