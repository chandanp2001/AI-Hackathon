from typing import Dict, Any, List, Tuple, Optional, Set
from .base import DataSource
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import logging
import re
import json
import os
from collections import defaultdict

class GoogleDocsDataSource(DataSource):
    def __init__(self):
        """Initialize the Google Docs data source."""
        super().__init__()
        self.service = None
        self._initialize_service()

    def _initialize_service(self):
        """Initialize the Google Docs service with credentials."""
        try:
            # Use paths from environment if available
            credentials_path = os.environ.get('GOOGLE_CREDENTIALS_PATH', 'credentials.json')
            token_path = os.environ.get('GOOGLE_TOKEN_PATH', 'token.json')
            
            # Check if token exists
            if not os.path.exists(token_path):
                raise FileNotFoundError(f"Google token not found at {token_path}. Please run services/auth/google_auth.py first.")
            
            # Create client using the token path
            creds = Credentials.from_authorized_user_file(token_path, [
                'https://www.googleapis.com/auth/documents.readonly'
            ])
            self.service = build('docs', 'v1', credentials=creds)
        except Exception as e:
            error_msg = f"Error initializing Google Docs service: {str(e)}"
            logging.error(error_msg)
            raise RuntimeError(error_msg)

    async def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate the provided document IDs."""
        try:
            # Log inputs for debugging
            logging.info(f"Validating inputs: {inputs}")
            
            # Check for document_urls key
            if 'document_urls' not in inputs:
                logging.error("No 'document_urls' key found in inputs")
                return False
                
            # Extract document IDs
            doc_ids = self._extract_doc_ids(inputs.get('document_urls', ''))
            
            # Check if we got any document IDs
            if not doc_ids:
                logging.error("No valid document IDs extracted")
                return False
                
            # Validate each document ID
            for doc_id in doc_ids:
                logging.info(f"Validating document ID: {doc_id}")
                try:
                    self.service.documents().get(documentId=doc_id).execute()
                    logging.info(f"Document ID {doc_id} validation successful")
                except Exception as e:
                    logging.error(f"Document ID {doc_id} validation failed: {e}")
                    return False
                    
            # All validations passed
            return True
            
        except Exception as e:
            logging.error(f"Error validating Google Docs inputs: {str(e)}")
            return False

    async def fetch_data(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch data from specified Google Docs."""
        try:
            doc_ids = self._extract_doc_ids(inputs.get('document_urls', ''))
            
            # Log what we're about to fetch
            logging.info(f"Fetching data from {len(doc_ids)} Google Docs: {doc_ids}")
            
            # Check if we have any document IDs
            if not doc_ids:
                logging.error("No valid document IDs were provided")
                return {'error': "No valid document IDs were provided. Please check the URL or document ID format."}
            
            results = {}
            
            for doc_id in doc_ids:
                try:
                    logging.info(f"Fetching document with ID: {doc_id}")
                    doc = self.service.documents().get(documentId=doc_id).execute()
                    content = self._extract_document_content(doc)
                    
                    # Enhanced: Extract structure metadata
                    doc_structure = self._analyze_document_structure(content, doc)
                    
                    # Log success and content length
                    logging.info(f"Successfully fetched document '{doc['title']}' ({len(content)} characters)")
                    
                    results[doc['title']] = {
                        'title': doc['title'],
                        'content': content,
                        'last_modified': doc.get('modifiedTime', 'Unknown'),
                        'structure': doc_structure
                    }
                except HttpError as e:
                    # Specific handling for Google API errors
                    error_reason = e.reason if hasattr(e, 'reason') else str(e)
                    error_status = e.status_code if hasattr(e, 'status_code') else 'unknown'
                    error_details = f"HTTP Error {error_status}: {error_reason}"
                    
                    logging.error(f"Google API error for document {doc_id}: {error_details}")
                    
                    # Try to get more specific error information
                    if error_status == 404:
                        error_msg = f"Document not found (ID: {doc_id}). Please check if the document exists and you have access to it."
                    elif error_status == 403:
                        error_msg = f"Access denied (ID: {doc_id}). Please check permissions for this document."
                    elif error_status == 401:
                        error_msg = f"Authentication failed. Please check your Google API credentials."
                    else:
                        error_msg = f"Could not fetch document (ID: {doc_id}): {error_details}"
                    
                    results[doc_id] = {
                        'error': error_msg
                    }
                except Exception as e:
                    # Generic error handling
                    logging.error(f"Error fetching document {doc_id}: {str(e)}", exc_info=True)
                    results[doc_id] = {
                        'error': f"Error fetching document: {str(e)}"
                    }
            
            # Check if we got any results
            if not results:
                return {'error': "Failed to fetch any documents. Please check document IDs and permissions."}
                
            # Check if all results have errors
            all_errors = all('error' in doc_data for doc_data in results.values())
            if all_errors:
                # Collect all error messages
                error_msgs = [data['error'] for data in results.values()]
                return {'error': f"Failed to fetch any documents successfully. Errors: {'; '.join(error_msgs)}"}
                
            return results
            
        except Exception as e:
            logging.error(f"Error in fetch_data: {str(e)}", exc_info=True)
            return {'error': f"Error fetching Google Docs: {str(e)}"}

    def _analyze_document_structure(self, content: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze document content to extract structure information.
        
        Args:
            content: The document content as a string
            doc: The raw document object from Google API
            
        Returns:
            Dictionary with document structure information
        """
        # Initialize structure object
        structure = {
            'has_tables': False,
            'has_bullets': False,
            'has_images': False,
            'section_count': 0,
            'headings': [],
            'inferred_sections': []
        }
        
        # Check for tables (quick check in raw content)
        if '|' in content and '\n| ' in content:
            structure['has_tables'] = True
            
        # Check for bullet points
        if '• ' in content or '* ' in content:
            structure['has_bullets'] = True
            
        # Check for images in the document structure
        if 'body' in doc and 'content' in doc['body']:
            for element in doc['body']['content']:
                if 'inlineObjectElement' in element:
                    structure['has_images'] = True
                    break
                    
        # Extract original headings
        headings = []
        for match in re.finditer(r'^(#+)\s+(.+?)$', content, re.MULTILINE):
            level = len(match.group(1))
            text = match.group(2).strip()
            headings.append({'level': level, 'text': text})
            
        # Also look for underlined headings
        underline_headings = re.findall(r'(.+)\n[=\-]{3,}', content)
        for heading in underline_headings:
            level = 1 if '=' in heading else 2
            headings.append({'level': level, 'text': heading.strip()})
            
        structure['headings'] = headings
        
        # Split into sections and analyze them
        sections = self._split_into_sections(content)
        structure['section_count'] = len(sections)
        
        # Analyze each section and create inferred sections
        inferred_sections = []
        for section in sections:
            # Skip empty sections
            if not section.strip():
                continue
                
            section_info = self._classify_section_content(section)
            inferred_sections.append(section_info)
            
        structure['inferred_sections'] = inferred_sections
        
        return structure

    def _classify_section_content(self, section: str) -> Dict[str, Any]:
        """
        Classify a section's content and infer its purpose.
        
        Args:
            section: The section text
            
        Returns:
            Dictionary with classification information
        """
        section_info = {
            'type': 'general',
            'tags': [],
            'is_boilerplate': False,
            'is_legal': False,
            'confidence': 0.6,
            'word_count': len(re.findall(r'\b\w+\b', section))
        }
        
        # Check if it's a heading
        if len(section.strip().split('\n')) == 1 and len(section) < 100:
            section_info['type'] = 'heading'
            section_info['confidence'] = 0.9
            return section_info
            
        # Check for legal boilerplate
        legal_patterns = [
            r'confidential',
            r'all rights reserved',
            r'terms and conditions',
            r'privacy policy',
            r'disclaimer',
            r'© \d{4}',
            r'copyright',
            r'trademark',
            r'intellectual property',
            r'legal notice'
        ]
        
        legal_matches = 0
        for pattern in legal_patterns:
            if re.search(pattern, section.lower()):
                legal_matches += 1
                
        if legal_matches >= 2:
            section_info['is_legal'] = True
            section_info['is_boilerplate'] = True
            section_info['type'] = 'legal'
            section_info['tags'].append('LEGAL')
            section_info['confidence'] = 0.8
            
        # Check for specific section types
        section_lower = section.lower()
        
        # Check for key insights
        insight_patterns = [
            r'key (takeaways|insights|findings|points|observations)',
            r'main (points|findings|takeaways)',
            r'summary of (findings|results)',
            r'important (points|observations)'
        ]
        
        for pattern in insight_patterns:
            if re.search(pattern, section_lower):
                section_info['type'] = 'insights'
                section_info['tags'].append('KEY INSIGHTS')
                section_info['confidence'] = 0.85
                break
                
        # Check for action items
        action_patterns = [
            r'action items',
            r'next steps',
            r'to-do',
            r'action plan',
            r'tasks',
            r'assignments',
            r'follow[ -]up (items|tasks)',
            r'deliverables'
        ]
        
        for pattern in action_patterns:
            if re.search(pattern, section_lower):
                section_info['type'] = 'action_items'
                section_info['tags'].append('ACTION ITEMS')
                section_info['confidence'] = 0.85
                break
                
        # Check for conclusions
        conclusion_patterns = [
            r'conclusion',
            r'in summary',
            r'to summarize',
            r'in conclusion',
            r'to conclude',
            r'closing thoughts',
            r'final (thoughts|remarks|observations)',
            r'summary'
        ]
        
        for pattern in conclusion_patterns:
            if re.search(pattern, section_lower):
                section_info['type'] = 'conclusion'
                section_info['tags'].append('CONCLUSION')
                section_info['confidence'] = 0.8
                break
                
        # Check for contact information
        contact_patterns = [
            r'contact (us|information|details)',
            r'(email|phone|address):.+',
            r'get in touch',
            r'for more information'
        ]
        
        for pattern in contact_patterns:
            if re.search(pattern, section_lower):
                section_info['type'] = 'contact'
                section_info['tags'].append('CONTACT')
                section_info['confidence'] = 0.75
                break
                
        # Additional section type classifications can be added here
        
        return section_info

    def format_for_llm(self, data: Dict[str, Any]) -> str:
        """Format the fetched Google Docs data for LLM consumption with metadata control protocol."""
        # Handle non-dictionary data argument
        if not isinstance(data, dict):
            return f"Error: Google Docs data is not in expected format. Received: {type(data)}"

        if not data or isinstance(data, dict) and 'error' in data:
            return f"Error retrieving Google Docs data: {data.get('error', 'Unknown error')}"
        
        formatted = []
        formatted.append("# GOOGLE DOCUMENTS DATA")
        formatted.append("The following sections contain content from Google Docs documents.")
        
        for title, doc_data in data.items():
            # Check if doc_data is a dictionary before trying to access keys
            if not isinstance(doc_data, dict):
                formatted.append(f"\n## ERROR: Document '{title}'")
                formatted.append(f"```error\nInvalid document data type: {type(doc_data)}\n```")
                continue
                
            if 'error' in doc_data:
                formatted.append(f"\n## ERROR: Document '{title}'")
                formatted.append(f"```error\n{doc_data['error']}\n```")
                continue
            
            # Add document title and enhanced metadata section
            formatted.append(f"\n## DOCUMENT: {title}")
            formatted.append("```metadata")
            formatted.append(f"title: {doc_data.get('title', 'Unknown')}")
            formatted.append(f"last_modified: {doc_data.get('last_modified', 'Unknown')}")
            
            # Extract structure info
            structure = doc_data.get('structure', {})
            
            # Enhanced metadata: Add has_tables, has_bullets, section_count
            formatted.append(f"has_tables: {structure.get('has_tables', False)}")
            formatted.append(f"has_bullets: {structure.get('has_bullets', False)}")
            formatted.append(f"has_images: {structure.get('has_images', False)}")
            formatted.append(f"section_count: {structure.get('section_count', 0)}")
            
            # Extract and add content metrics
            content = doc_data.get('content', '')
            if content:
                words = re.findall(r'\w+', content)
                sentences = re.split(r'[.!?]+', content)
                paragraphs = [p for p in content.split('\n\n') if p.strip()]
                
                formatted.append(f"word_count: {len(words)}")
                formatted.append(f"sentence_count: {len(sentences)}")
                formatted.append(f"paragraph_count: {len(paragraphs)}")
                formatted.append(f"character_count: {len(content)}")
            formatted.append("```")
            
            # Add headings map
            headings = structure.get('headings', [])
            if headings:
                formatted.append("```headings-map")
                for heading in headings:
                    indent = "  " * (heading.get('level', 1) - 1)
                    formatted.append(f"{indent}- {heading.get('text', '')}")
                formatted.append("```")
            
            # Add structured content sections
            if content:
                # Get all inferred sections
                inferred_sections = structure.get('inferred_sections', [])
                
                # If we have properly analyzed sections, use them
                if inferred_sections:
                    for i, section_info in enumerate(inferred_sections):
                        section_type = section_info.get('type', 'general')
                        tags = section_info.get('tags', [])
                        is_legal = section_info.get('is_legal', False)
                        is_boilerplate = section_info.get('is_boilerplate', False)
                        
                        # Skip empty sections and heading-only sections that aren't in headings list
                        if section_type == 'heading' and not any(h.get('text') == section_info.get('text', '') for h in headings):
                            continue
                            
                        # Add section heading with tags if available
                        if tags:
                            tag_str = ', '.join(tags)
                            formatted.append(f"\n### SECTION: {tag_str}")
                        else:
                            formatted.append(f"\n### SECTION: {i+1}")
                            
                        # Add section metadata
                        formatted.append("```section-metadata")
                        formatted.append(f"type: {section_type}")
                        if is_legal:
                            formatted.append("legal: true")
                        if is_boilerplate:
                            formatted.append("boilerplate: true")
                        formatted.append(f"word_count: {section_info.get('word_count', 0)}")
                        formatted.append(f"confidence: {section_info.get('confidence', 0.5)}")
                        formatted.append("```")
                        
                        # Add content block
                        formatted.append("```content")
                        formatted.append(section_info.get('text', ''))
                        formatted.append("```")
                else:
                    # Fall back to basic section splitting if we don't have analyzed sections
                    sections = self._split_into_sections(content)
                    
                    for i, section in enumerate(sections):
                        # Skip empty sections
                        if not section.strip():
                            continue
                            
                        # Determine if it's a header
                        is_header = len(section.strip().split('\n')) == 1 and len(section) < 100
                        
                        if is_header:
                            formatted.append(f"\n### {section.strip()}")
                        else:
                            # Add content blocks with contextual annotations
                            formatted.append("```content")
                            formatted.append(section)
                            formatted.append("```")
            
            formatted.append("\n---")  # Separator between documents
        
        final_result = "\n".join(formatted)
        logging.info(f"Formatted Google Docs data: {len(final_result)} characters, {len(data)} documents")
        
        return final_result
        
    def _split_into_sections(self, content: str) -> List[str]:
        """Split document content into logical sections based on formatting."""
        # First try to split by common Google Docs header patterns
        import re
        
        # Look for header patterns like "# Header" or "Header\n---" or lines with all caps
        header_patterns = [
            r'#{1,6}\s+.+',  # Markdown headers
            r'.+\n[-=]+',    # Underlined headers
            r'^[A-Z][A-Z\s]+$'  # ALL CAPS headers
        ]
        
        headers_found = False
        sections = []
        current_section = []
        
        for line in content.split('\n'):
            is_header = any(re.match(pattern, line) for pattern in header_patterns)
            
            if is_header and current_section:
                # Save the previous section
                headers_found = True
                sections.append('\n'.join(current_section))
                current_section = [line]
            else:
                current_section.append(line)
        
        # Don't forget the last section
        if current_section:
            sections.append('\n'.join(current_section))
            
        # If we didn't find headers, fall back to paragraph-based sections
        if not headers_found:
            sections = [p for p in re.split(r'\n\s*\n', content) if p.strip()]
            
            # If we have too many small sections, group them into larger chunks
            if len(sections) > 10:
                grouped_sections = []
                current_group = []
                
                for section in sections:
                    current_group.append(section)
                    # Group paragraphs into chunks of about 300-500 words
                    if sum(len(s.split()) for s in current_group) > 300:
                        grouped_sections.append('\n\n'.join(current_group))
                        current_group = []
                
                # Add the last group if not empty
                if current_group:
                    grouped_sections.append('\n\n'.join(current_group))
                
                sections = grouped_sections
        
        return sections

    def calculate_relevance(self, query: str, doc_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculate relevance scores between user query and document sections.
        
        Args:
            query: User's query string
            doc_data: The document data
            
        Returns:
            Dictionary of relevance scores by section
        """
        if not query or not doc_data or not isinstance(doc_data, dict):
            return {}
            
        query_terms = set(re.findall(r'\b\w+\b', query.lower()))
        if not query_terms:
            return {}
            
        relevance_scores = {}
        
        # Check if we have structure with inferred sections
        structure = doc_data.get('structure', {})
        inferred_sections = structure.get('inferred_sections', [])
        
        if inferred_sections:
            # Score each inferred section
            for i, section_info in enumerate(inferred_sections):
                section_text = section_info.get('text', '')
                section_type = section_info.get('type', 'general')
                
                # Skip headings
                if section_type == 'heading':
                    continue
                    
                # Calculate score based on term overlap
                section_terms = set(re.findall(r'\b\w+\b', section_text.lower()))
                term_overlap = len(query_terms.intersection(section_terms))
                
                # Calculate basic term frequency score
                if section_terms:
                    score = term_overlap / len(query_terms)
                    
                    # Boost score for key sections
                    if section_type in ['insights', 'action_items', 'conclusion']:
                        score *= 1.3
                    
                    # Reduce score for legal/boilerplate sections
                    if section_info.get('is_legal', False) or section_info.get('is_boilerplate', False):
                        score *= 0.3
                        
                    relevance_scores[f"section_{i}"] = {
                        'score': min(1.0, score),  # Cap at 1.0
                        'type': section_type,
                        'tags': section_info.get('tags', [])
                    }
        else:
            # Fallback: score the whole document
            content = doc_data.get('content', '')
            content_terms = set(re.findall(r'\b\w+\b', content.lower()))
            
            if content_terms:
                term_overlap = len(query_terms.intersection(content_terms))
                score = term_overlap / len(query_terms)
                relevance_scores['whole_document'] = {
                    'score': min(1.0, score),
                    'type': 'document',
                    'tags': []
                }
                
        return relevance_scores

    def get_form_fields(self) -> List[Dict[str, Any]]:
        """Return the configuration fields required for Google Docs."""
        return [{
            'type': 'text',
            'label': 'Document URLs or IDs',
            'name': 'document_urls',
            'placeholder': 'Enter Google Doc URLs or IDs (comma-separated)',
            'required': True
        }]

    def _extract_doc_ids(self, urls_or_ids: str) -> List[str]:
        """Extract document IDs from URLs or return as is if they're IDs."""
        ids = []
        
        # If input is None or empty, return empty list
        if not urls_or_ids:
            logging.warning("No document URLs or IDs provided")
            return ids
            
        # Log the input for debugging
        logging.info(f"Extracting document IDs from: {urls_or_ids}")
        
        # Handle both string and list inputs
        url_list = []
        if isinstance(urls_or_ids, str):
            # If it's a string, split by comma
            url_list = [item.strip() for item in urls_or_ids.split(',') if item.strip()]
        elif isinstance(urls_or_ids, list):
            # If it's already a list, use it directly
            url_list = urls_or_ids
        else:
            logging.error(f"Unexpected type for urls_or_ids: {type(urls_or_ids)}")
            return ids
            
        # Process each URL or ID
        for item in url_list:
            if not item:
                continue
                
            # Convert to string if not already
            if not isinstance(item, str):
                item = str(item)
                
            # Extract ID from URL if it's a URL
            # Check for standard Google Docs URL pattern
            match = re.search(r'/document/d/([a-zA-Z0-9-_]+)', item)
            if match:
                doc_id = match.group(1)
                # Remove any URL parameters or additional path segments
                doc_id = doc_id.split('/')[0]
                doc_id = doc_id.split('?')[0]
                ids.append(doc_id)
                logging.info(f"Extracted document ID: {doc_id} from URL: {item}")
                continue
                
            # Alternative pattern for direct links
            alt_match = re.search(r'docs.google.com/document/d/([a-zA-Z0-9-_]+)', item)
            if alt_match:
                doc_id = alt_match.group(1)
                # Remove any URL parameters or additional path segments
                doc_id = doc_id.split('/')[0]
                doc_id = doc_id.split('?')[0]
                ids.append(doc_id)
                logging.info(f"Extracted document ID: {doc_id} from URL: {item}")
                continue
                
            # Check if it looks like a direct document ID (alphanumeric with dashes/underscores, 25-44 chars)
            if re.match(r'^[a-zA-Z0-9-_]{25,44}$', item):
                ids.append(item)
                logging.info(f"Using direct document ID: {item}")
                continue
                
            # Last resort - if it contains a document ID pattern anywhere in the string
            last_resort_match = re.search(r'([a-zA-Z0-9-_]{25,44})', item)
            if last_resort_match:
                doc_id = last_resort_match.group(1)
                ids.append(doc_id)
                logging.info(f"Extracted possible document ID: {doc_id} from text: {item}")
                continue
                
            logging.warning(f"Could not extract document ID from: {item}")
            
        # Log the final result
        if ids:
            logging.info(f"Extracted {len(ids)} document IDs: {ids}")
        else:
            logging.error("Failed to extract any valid document IDs")
            
        return ids

    def _extract_document_content(self, document: Dict[str, Any]) -> str:
        """Extract readable content from a Google Doc with improved structure handling."""
        logging.info(f"Extracting content from document: {document.get('title', 'Unknown')}")
        
        content = []
        if 'body' not in document or 'content' not in document['body']:
            logging.warning("Document body or content not found in document structure")
            return "Document content could not be extracted properly"
            
        elements = document['body']['content']
        logging.info(f"Found {len(elements)} elements in document body")
        
        # Track document structure for better formatting
        current_list_id = None
        current_list_items = []
        list_nesting_level = 0
        has_bullet_points = False
        has_tables = False
        
        for element in elements:
            # Handle paragraphs (most common element)
            if 'paragraph' in element:
                paragraph = element['paragraph']
                paragraph_style = paragraph.get('paragraphStyle', {})
                paragraph_text = ""
                
                # Check if this is a heading
                if 'namedStyleType' in paragraph_style:
                    style_type = paragraph_style['namedStyleType']
                    if 'HEADING' in style_type:
                        heading_level = int(style_type.replace('HEADING_', '')) if style_type.replace('HEADING_', '').isdigit() else 1
                        heading_prefix = '#' * heading_level + ' '
                    else:
                        heading_prefix = ''
                else:
                    heading_prefix = ''
                
                # Extract text from paragraph
                for part in paragraph.get('elements', []):
                    if 'textRun' in part and 'content' in part['textRun']:
                        text_content = part['textRun']['content']
                        text_style = part['textRun'].get('textStyle', {})
                        
                        # Apply basic formatting
                        if text_style.get('bold'):
                            text_content = f"**{text_content}**"
                        if text_style.get('italic'):
                            text_content = f"*{text_content}*"
                        
                        paragraph_text += text_content
                
                # Handle list formatting
                if 'bullet' in paragraph:
                    has_bullet_points = True
                    bullet_info = paragraph['bullet']
                    list_id = bullet_info.get('listId')
                    nesting_level = bullet_info.get('nestingLevel', 0)
                    
                    # If new list or nesting level changed, add proper formatting
                    if list_id != current_list_id or nesting_level != list_nesting_level:
                        current_list_id = list_id
                        list_nesting_level = nesting_level
                        
                    # Add bullet indentation based on nesting level
                    indent = "  " * nesting_level
                    paragraph_text = f"{indent}• {paragraph_text}"
                    
                # Add the paragraph with proper formatting
                if heading_prefix:
                    content.append(f"\n{heading_prefix}{paragraph_text}")
                else:
                    content.append(paragraph_text)
                    
            # Handle tables (convert to markdown-style tables)
            elif 'table' in element:
                has_tables = True
                table = element['table']
                rows = table.get('tableRows', [])
                
                # Start table
                content.append("\n")  # Space before table
                
                for row_index, row in enumerate(rows):
                    cells = row.get('tableCells', [])
                    row_content = []
                    
                    for cell in cells:
                        cell_text = ""
                        for cell_element in cell.get('content', []):
                            if 'paragraph' in cell_element:
                                for part in cell_element['paragraph'].get('elements', []):
                                    if 'textRun' in part:
                                        cell_text += part['textRun'].get('content', '')
                        
                        row_content.append(cell_text.strip() or " ")
                    
                    # Add row as pipe-separated values
                    content.append("| " + " | ".join(row_content) + " |")
                    
                    # After first row (header), add separator
                    if row_index == 0:
                        content.append("| " + " | ".join(["---"] * len(cells)) + " |")
                
                content.append("\n")  # Space after table
        
        result = "\n".join(content)
        logging.info(f"Extracted {len(result)} characters of content from the document")
        
        # If content is too short, it might indicate an issue
        if len(result) < 50 and len(elements) > 5:
            logging.warning(f"Document content seems suspiciously short ({len(result)} chars) for {len(elements)} elements")
        
        return result 