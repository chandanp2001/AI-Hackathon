from typing import Dict, Any, List, Tuple, Optional, Deque
import json
from services.data_sources.base import DataSource, DataSourceRegistry
import logging
import requests
import certifi
from config import OPENAI_API_KEY, OPENAI_API_URL, MAX_TOKENS_PROMPT, MAX_TOKENS_RESPONSE, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION

import asyncio
import re
import random
from collections import deque
import time
import uuid

logger = logging.getLogger(__name__)

class ConversationHistory:
    """Class to manage conversation history for maintaining context between queries"""
    
    def __init__(self, max_history: int = 5):
        self.history: Deque[Dict[str, Any]] = deque(maxlen=max_history)
        self.conversation_id = str(uuid.uuid4())
        self.created_at = time.time()
        self.last_updated = time.time()
        
    def add_exchange(self, query: str, response: str, sources: Dict[str, Any], 
                     confidence: float, metadata: Dict[str, Any]) -> None:
        """Add a new query-response exchange to the history"""
        exchange = {
            'timestamp': time.time(),
            'query': query,
            'response': response,
            'sources': list(sources.keys()),
            'confidence': confidence,
            'metadata': metadata
        }
        self.history.append(exchange)
        self.last_updated = time.time()
        
    def get_history_as_context(self, max_items: int = 3) -> str:
        """Format conversation history as context for the LLM"""
        if not self.history:
            return ""
            
        formatted = ["### PREVIOUS CONVERSATION HISTORY ###"]
        # Get the most recent exchanges up to max_items
        recent_history = list(self.history)[-max_items:]
        
        for i, exchange in enumerate(recent_history):
            formatted.append(f"[Exchange {i+1}]")
            formatted.append(f"User: {exchange['query']}")
            formatted.append(f"Assistant: {exchange['response'][:250]}..." if len(exchange['response']) > 250 
                           else f"Assistant: {exchange['response']}")
            formatted.append(f"Sources used: {', '.join(exchange['sources'])}")
            formatted.append("")
            
        return "\n".join(formatted)
        
    def find_related_exchanges(self, query: str) -> List[Dict[str, Any]]:
        """Find exchanges related to the current query"""
        if not self.history:
            return []
            
        related = []
        query_tokens = set(query.lower().split())
        
        for exchange in self.history:
            prev_query_tokens = set(exchange['query'].lower().split())
            # Calculate token overlap as a simple relevance measure
            overlap = len(query_tokens.intersection(prev_query_tokens))
            if overlap > 2:  # Threshold for considering related
                related.append(exchange)
                
        return related
    
    def is_empty(self) -> bool:
        """Check if history is empty"""
        return len(self.history) == 0

class ContextManager:
    """Manages context building and prompt engineering for LLM"""
    
    def __init__(self, source_registry: DataSourceRegistry, llm_service):
        self.source_registry = source_registry
       
        self.max_tokens = 8000  # GPT-4 context window
        self.max_tokens_prompt = MAX_TOKENS_PROMPT
        self.max_response_tokens = MAX_TOKENS_RESPONSE
        # self.encoding = tiktoken.get_encoding("cl100k_base")
        self.llm_service = llm_service
        self.prompt_stats = {}  # Store stats for A/B testing
        
        # Conversation history tracking
        self.conversation_histories = {}  # Map user_id -> ConversationHistory
        self.history_expiry = 30 * 60  # 30 minutes in seconds

    # Initialize prompt templates for different intents
    PROMPT_TEMPLATES = {
        "factual": {
            "description": "Direct factual questions requiring precise answers",
            "formal": "Respond in a professional, precise manner with direct facts",
            "casual": "Respond conversationally with straightforward facts"
        },
        "analytical": {
            "description": "Questions requiring analysis across data sources",
            "formal": "Provide thorough, structured analysis with professional tone",
            "casual": "Explain your analysis in a conversational, accessible way"
        },
        "comparative": {
            "description": "Questions comparing multiple items or aspects",
            "formal": "Present a structured comparison with objective assessment",
            "casual": "Compare these in a straightforward, conversational way"
        },
        "list": {
            "description": "Requests for lists or collections of items",
            "formal": "Present a well-organized list with precise categorization",
            "casual": "List these items in a friendly, readable format"
        }
    }

    def build_context(self, source_data: Dict[str, Any]) -> str:
        """Build context from multiple data sources"""
        contexts = []
        
        for source_name, data in source_data.items():
            source = self.source_registry.get_source(source_name)
            if source:
                try:
                    # Handle case where data is already a string
                    if isinstance(data, str):
                        formatted_data = data
                    else:
                        formatted_data = source.format_for_llm(data)
                    contexts.append(f"=== {source_name.upper()} DATA ===\n{formatted_data}")
                except Exception as e:
                    logger.error(f"Error formatting data for source {source_name}: {str(e)}")
                    contexts.append(f"=== {source_name.upper()} DATA ===\nError: Could not format data")
        
        if not contexts:
            return "No data available from sources"
            
        return "\n\n".join(contexts)
    
    def _determine_question_type(self, query: str) -> Dict[str, str]:
        """Determine the type of question being asked to optimize response format
        
        Args:
            query: The user's question or query
            
        Returns:
            Dictionary with question type info including 'type', 'intent', etc.
        """
        query_lower = query.lower()
        
        # Default to analytical for very short queries
        if len(query_lower.split()) < 3:
            return {
                "type": "short_query",
                "intent": "analytical"
            }
            
        # Check for list/enumeration questions
        list_patterns = [
            r'\blist\b', r'\benumerate\b', r'\bwhat are\b', 
            r'\bwhat were\b', r'\bshow me\b', r'\ball\b',
            r'\bmain\b.*\bitems\b', r'\bmain\b.*\bpoints\b',
            r'\bwhat issues\b'
        ]
        for pattern in list_patterns:
            if re.search(pattern, query_lower):
                return {
                    "type": "list_question",
                    "intent": "list"
                }
        
        # Check for comparison questions
        comparison_patterns = [
            r'\bcompare\b', r'\bversus\b', r'\bvs\b', 
            r'\bdifference\b', r'\bsimilarities\b',
            r'\bhow does\b.*\bcompare\b', r'\bhow do\b.*\bcompare\b',
            r'\bwhich is\b.*\bbetter\b', r'\bwhich are\b.*\bbetter\b'
        ]
        for pattern in comparison_patterns:
            if re.search(pattern, query_lower):
                return {
                    "type": "comparison_question",
                    "intent": "comparative"  
                }
        
        # Check for factual/direct questions
        factual_patterns = [
            r'\bwho\b', r'\bwhen\b', r'\bwhere\b',
            r'\bhow many\b', r'\bhow much\b', 
            r'\bis there\b', r'\bare there\b',
            r'\bfind\b.*\bexact\b', r'\bspecific\b'
        ]
        for pattern in factual_patterns:
            if re.search(pattern, query_lower):
                return {
                    "type": "factual_question",
                    "intent": "factual"
                }
        
        # Check for analytical questions (why, how, tell me about, explain, etc.)
        analytical_patterns = [
            r'\bwhy\b', r'\bhow\b', r'\bexplain\b', 
            r'\bdescribe\b', r'\binsights\b', r'\bunderstand\b',
            r'\banalysis\b', r'\banalyze\b', r'\bwhat is\b',
            r'\btell me about\b', r'\bsummarize\b'
        ]
        for pattern in analytical_patterns:
            if re.search(pattern, query_lower):
                return {
                    "type": "analytical_question",
                    "intent": "analytical"
                }
                
        # Default: treat as analytical
        return {
            "type": "general_question",
            "intent": "analytical"
        }

    def _is_comparison_request(self, query: str) -> bool:
        """Detect if a query is requesting a comparison or tabular format
        
        Args:
            query: The user query
            
        Returns:
            Boolean indicating if this is likely a comparison request
        """
        query_lower = query.lower()
        
        # Patterns that suggest comparison requests
        comparison_patterns = [
            r'compare', r'comparison', r'versus', r'\bvs\b', r'similarities',
            r'differences', r'table', r'tabular', r'side by side',
            r'pros and cons', r'advantages', r'disadvantages', r'which is better',
            r'differences between', r'how do.*differ', r'contrast', r'matrix'
        ]
        
        # Check if query matches any comparison patterns
        for pattern in comparison_patterns:
            if re.search(pattern, query_lower):
                return True
                
        return False
        
    def _create_comparison_prompt(self, query: str, source_data: Dict[str, Any], intent: str, 
                                 tone: str = "formal", conversation_history: ConversationHistory = None) -> str:
        """Create a specialized prompt for comparison queries with improved formatting
        
        Args:
            query: The user's query
            source_data: Dictionary of data from different sources
            intent: The detected intent
            tone: The desired tone (formal or casual)
            conversation_history: Optional conversation history for context
            
        Returns:
            A specialized prompt for handling comparison queries with clean formatting
        """
        # Format context data
        formatted_context = self.build_context(source_data)
        mentioned_sources = list(source_data.keys())
        
        # Add conversation history if available
        history_context = ""
        if conversation_history and not conversation_history.is_empty():
            history_context = conversation_history.get_history_as_context()
        
        # Prompt version for tracking
        prompt_version = "v1.0-comparison-specialized"
        
        # Build the comparison prompt
        prompt = f"""PROMPT VERSION: {prompt_version}

You are an intelligent assistant responding to a request for comparison or tabular information. Focus on creating clear, well-formatted output.

QUERY INTENT: comparison/tabular analysis

Please follow these strict formatting guidelines:
1. DO NOT use Markdown formatting with asterisks (*) for bold or emphasis
2. For section headers, use UPPERCASE or clear labels like "SECTION:" instead of asterisks
3. For tables:
   - Use plain text formatting with clear column separation
   - Use hyphens (-) for horizontal lines between header and content
   - Align columns properly for readability
   - Ensure all column headers are clearly labeled without asterisks
4. For comparisons:
   - Use clear section labels without asterisks
   - Organize information into clear categories
   - Use numbered or bulleted lists without asterisks
   - Present information in a readable, structured format

"""

        # Add conversation history if available
        if history_context:
            prompt += f"""CONVERSATION HISTORY:
{history_context}

"""

        prompt += f"""AVAILABLE DATA SOURCES:
{formatted_context}

QUERY: {query}

RESPONSE GUIDANCE:
- Structure the response in a clean, easy-to-read format
- For tables, use consistent column widths and proper alignment
- For comparisons, use clear section headers without asterisks
- Organize information logically
- Focus on clarity and readability over fancy formatting
- If creating multiple sections, use clear dividers like "---" between sections
- Include a confidence level at the end

"""
        
        return prompt        

    def _format_response_for_readability(self, response_text: str, is_comparison: bool = False) -> str:
        """Format the response text for better readability, especially for tables and comparisons
        
        Args:
            response_text: The raw response text from the LLM
            is_comparison: Whether this is a comparison/table response
            
        Returns:
            Cleaned and formatted response text
        """
        # Remove any confidence indicator first (we'll add it back at the end)
        confidence_pattern = r'\[Confidence:?\s*(High|Medium|Low|[\d\.]+)\]'
        confidence_match = re.search(confidence_pattern, response_text)
        confidence_text = ""
        
        if confidence_match:
            confidence_text = confidence_match.group(0)
            response_text = re.sub(confidence_pattern, '', response_text).strip()
        
        # Remove Markdown bold/italic formatting (asterisks)
        response_text = re.sub(r'\*\*(.*?)\*\*', r'\1', response_text)  # Remove bold
        response_text = re.sub(r'\*(.*?)\*', r'\1', response_text)      # Remove italic
        
        # Handle table formatting 
        # First, identify table sections in the response
        lines = response_text.split('\n')
        formatted_lines = []
        in_table = False
        table_lines = []
        
        for i, line in enumerate(lines):
            # Detect table headers (lines with multiple | characters)
            if '|' in line and line.count('|') >= 2:
                # If this is the start of a new table
                if not in_table:
                    in_table = True
                    table_lines = []
                
                # Add the line to the current table
                table_lines.append(line)
            # Detect separator lines that shouldn't be included
            elif in_table and (line.strip().startswith('|------') or 
                              line.strip().startswith('------') or 
                              line.strip() == '|-----------|-----------|-----------|-----------|-----------|' or
                              line.strip().startswith('|---') or
                              line.strip() == ''):
                # Skip these malformed separator lines
                continue
            # If we're in a table but this line isn't part of it, end the table
            elif in_table:
                # Format the collected table lines
                formatted_table = self._format_table(table_lines)
                formatted_lines.extend(formatted_table)
                formatted_lines.append('')  # Add empty line after table
                in_table = False
                formatted_lines.append(line)  # Add the current non-table line
            else:
                formatted_lines.append(line)
        
        # Handle any table that goes to the end of the response
        if in_table:
            formatted_table = self._format_table(table_lines)
            formatted_lines.extend(formatted_table)
        
        # Standardize section headers (replace **Header:** pattern with HEADER:)
        response_text = '\n'.join(formatted_lines)
        response_text = re.sub(r'\*\*([^:]+):\*\*', r'\1:', response_text)
        
        # Replace Markdown-style headers (# Header) with plain headers
        response_text = re.sub(r'^#\s+(.+)$', r'\1:', response_text, flags=re.MULTILINE)
        
        # Add the confidence score back at the end
        if confidence_text:
            response_text = response_text.strip() + "\n\n" + confidence_text
            
        return response_text
        
    def _format_table(self, table_lines: List[str]) -> List[str]:
        """Format a table for better readability in Slack
        
        Args:
            table_lines: List of lines containing a table
            
        Returns:
            Formatted table lines
        """
        if not table_lines:
            return []
            
        # Clean up the table lines
        cleaned_lines = []
        
        # First, standardize the table format
        for line in table_lines:
            # Skip empty lines and separator-only lines within the table
            if not line.strip() or line.strip() == '---' or all(c in '|-' for c in line.strip()):
                continue
                
            # Fix lines missing initial or final pipe
            if not line.strip().startswith('|'):
                line = '| ' + line.strip()
            if not line.strip().endswith('|'):
                line = line.strip() + ' |'
                
            cleaned_lines.append(line)
            
        if not cleaned_lines:
            return []
            
        # Identify column count and positions
        header_line = cleaned_lines[0]
        columns = header_line.count('|') - 1  # Number of '|' minus 1 gives column count
        
        # Fix cell padding for proper alignment
        formatted_lines = []
        for line in cleaned_lines:
            # Split line by pipes, excluding the first and last empty elements
            cells = line.split('|')[1:-1]
            
            # Ensure we have the right number of cells, pad if necessary
            if len(cells) < columns:
                cells += [''] * (columns - len(cells))
            elif len(cells) > columns:
                cells = cells[:columns]
                
            # Re-join with proper spacing
            formatted_line = '| ' + ' | '.join(cell.strip() for cell in cells) + ' |'
            formatted_lines.append(formatted_line)
            
        # If there are at least 2 lines, add a separator after the header
        if len(formatted_lines) >= 2:
            # Create a separator line with proper length for each column
            header_cells = header_line.split('|')[1:-1]
            separator_cells = []
            
            for cell in header_cells:
                cell_len = len(cell.strip())
                separator_cells.append('-' * max(3, cell_len))
                
            separator_line = '| ' + ' | '.join(separator_cells) + ' |'
            formatted_lines.insert(1, separator_line)
            
        # Add a code block around the table for better Slack rendering
        result = ['```']
        result.extend(formatted_lines)
        result.append('```')
        
        return result

    def _is_general_knowledge_query(self, query: str) -> bool:
        """Detect if a query is likely a general knowledge question not specific to available data sources
        
        Args:
            query: The user query
            
        Returns:
            Boolean indicating if this is likely a general knowledge query
        """
        query_lower = query.lower()
        
        # Patterns that suggest general knowledge questions
        general_patterns = [
            r'what (is|are|does) [\w\s]+( mean| definition)?',
            r'explain [\w\s]+',
            r'define [\w\s]+',
            r'meaning of [\w\s]+',
            r'who (is|was) [\w\s]+',
            r'where is [\w\s]+',
            r'how (do|does|did) [\w\s]+',
            r'tell me about [\w\s]+',
            r'why (is|are|do|does) [\w\s]+',
            r'history of [\w\s]+'
        ]
        
        # Check if query matches any general knowledge patterns
        for pattern in general_patterns:
            if re.search(pattern, query_lower):
                return True
        
        # Check for single word or very short queries (likely term definitions)
        words = query_lower.split()
        if len(words) <= 3 and not any(term in query_lower for term in ['jira', 'slack', 'document', 'sheet']):
            return True
            
        return False
    
    def _evaluate_data_source_relevance(self, query: str, source_data: Dict[str, Any]) -> float:
        """Evaluate how relevant the available data sources are to the query
        
        Args:
            query: The user query
            source_data: Dictionary of data from different sources
            
        Returns:
            Relevance score between 0 and 1
        """
        # Default to medium relevance
        if not source_data:
            return 0.0
            
        # Extract key terms from query
        query_terms = set(term.lower() for term in re.findall(r'\b\w{3,}\b', query.lower()))
        if not query_terms:
            return 0.3  # Default medium-low relevance for queries without extractable terms
        
        # Count sources that contain query terms
        relevant_sources = 0
        term_mentions = 0
        
        for source_name, data in source_data.items():
            source_text = ""
            # Convert data to text for analysis based on type
            if isinstance(data, str):
                source_text = data.lower()
            elif isinstance(data, dict) and '_metadata' in data:
                # Skip metadata for content analysis
                content_items = [v for k, v in data.items() if k != '_metadata']
                source_text = ' '.join(str(item).lower() for item in content_items)
            else:
                source_text = str(data).lower()
            
            # Count query terms found in this source
            source_matches = 0
            for term in query_terms:
                if term in source_text:
                    source_matches += 1
                    term_mentions += source_text.count(term)
            
            # Consider source relevant if it has sufficient term matches
            if source_matches > 0:
                term_coverage = source_matches / len(query_terms)
                if term_coverage > 0.3:  # If source covers at least 30% of query terms
                    relevant_sources += 1
        
        # Calculate overall relevance based on:
        # 1. Percentage of sources that are relevant
        # 2. Percentage of query terms found across all sources
        # 3. Frequency of term mentions (normalized)
        
        source_coverage = relevant_sources / len(source_data) if len(source_data) > 0 else 0
        term_coverage = min(term_mentions / (len(query_terms) * 3), 1.0) if len(query_terms) > 0 else 0  # Cap at 1.0
        
        # Combined relevance with source coverage weighted more heavily
        relevance = (source_coverage * 0.7) + (term_coverage * 0.3)
        
        return relevance
    
    def _create_general_knowledge_prompt(self, query: str, source_data: Dict[str, Any], intent: str, 
                                        tone: str = "formal", conversation_history: ConversationHistory = None) -> str:
        """Create a prompt for general knowledge queries not covered by available data sources
        
        Args:
            query: The user's query
            source_data: Dictionary of data from different sources (might be irrelevant)
            intent: The detected intent
            tone: The desired tone (formal or casual)
            conversation_history: Optional conversation history for context
            
        Returns:
            A prompt for handling general knowledge queries
        """
        # Format any context data that might be tangentially relevant
        formatted_context = self.build_context(source_data)
        
        # Add conversation history if available
        history_context = ""
        if conversation_history and not conversation_history.is_empty():
            history_context = conversation_history.get_history_as_context()
        
        # Prompt version for tracking
        prompt_version = "v1.0-general-knowledge"
        
        # Build the general knowledge prompt
        prompt = f"""PROMPT VERSION: {prompt_version}

You are an intelligent assistant responding to a general knowledge query. The query appears to be about general information rather than specific to the provided data sources.

QUERY INTENT: general knowledge - {intent}

Please follow these guidelines:
1. First check if ANY information in the provided data sources is relevant to the query
2. If relevant information exists in the data sources, use it to answer the query
3. If no relevant information exists, use your general knowledge to provide a helpful response
4. Always indicate clearly when you're using general knowledge versus data source information
5. Keep responses accurate, informative, and concise

6. When providing general knowledge:
   - Stick to widely accepted facts
   - Avoid making claims about specific people, places, or events without clear general knowledge
   - Qualify uncertain information with phrases like "generally," "typically," or "commonly"
   - Be upfront about knowledge limitations
   - For highly specific questions, state when you don't have enough information
   - When providing factual information, focus on well-established knowledge rather than obscure details
   - Never make up information to appear more helpful

"""

        # Add conversation history if available
        if history_context:
            prompt += f"""CONVERSATION HISTORY:
{history_context}

"""

        prompt += f"""AVAILABLE DATA SOURCES (which may or may not be relevant):
{formatted_context}

QUERY: {query}

RESPONSE GUIDANCE:
- If you find relevant information in the data sources, begin with: "Based on the available data sources..."
- If you do NOT find relevant information, begin with: "[GENERAL KNOWLEDGE]" and then provide a helpful response
- Be clear and direct in your answer
- If using general knowledge, provide a factual, educational response
- Include a confidence level at the end

"""
        
        return prompt

    async def generate_analysis(self, source_data: Dict[str, Any], query: str, user_id: str = None, 
                               tone: str = "formal", enable_ab_testing: bool = True) -> Dict[str, Any]:
        """Generate analysis from source data with enhanced features like A/B testing, confidence scoring, and conversation history.
        
        Args:
            source_data: Dictionary of data from different sources
            query: The user's query
            user_id: Unique identifier for the user (for conversation tracking)
            tone: "formal" or "casual" tone for response
            enable_ab_testing: Whether to enable A/B testing between prompt approaches
            
        Returns:
            Dictionary containing response text, confidence score, and metadata
        """
        try:
            if not source_data:
                return {"response": "No data sources provided for analysis.", "confidence": 0.0, "metadata": {}}
                
            # Log the data sources available
            logger.info(f"Generating analysis for query: '{query}'")
            logger.info(f"Available data sources: {', '.join(source_data.keys())}")
            
            # Get or create conversation history
            conversation_history = self._get_or_create_history(user_id)
            
            # Find related previous exchanges
            related_exchanges = conversation_history.find_related_exchanges(query)
            has_related_history = len(related_exchanges) > 0
            
            # Determine question intent for routing
            question_info = self._determine_question_type(query)
            intent = question_info.get('intent', 'analytical')
            
            # Detect if this is a general knowledge query that might not be in data sources
            is_general_knowledge_query = self._is_general_knowledge_query(query)
            
            # Check if this is likely a request for a comparison or table
            is_comparison_query = self._is_comparison_request(query)
            
            # If this is a follow-up query, note that in the question info
            if has_related_history:
                question_info['is_followup'] = True
                logger.info(f"Detected follow-up query related to {len(related_exchanges)} previous exchanges")
            
            if is_general_knowledge_query:
                logger.info(f"Detected potential general knowledge query: '{query}'")
                question_info['is_general_knowledge'] = True
                
            if is_comparison_query:
                logger.info(f"Detected comparison or table request: '{query}'")
                question_info['is_comparison'] = True
            
            logger.info(f"Detected query intent: {intent}")
            
            # A/B Testing: Decide which prompt approach to use
            prompt_version = "default"
            if enable_ab_testing:
                # Choose between specialized intent-based prompts or general analysis prompt
                version_choice = random.random()
                if version_choice < 0.5:
                    prompt_version = "intent_specialized"
                else:
                    prompt_version = "general_analysis"
                logger.info(f"A/B Testing: Selected prompt version {prompt_version}")
            else:
                # If not A/B testing, use intent-based routing
                prompt_version = "intent_specialized"
            
            # For general knowledge queries, check if data sources have sufficient relevance
            using_general_knowledge = False
            if is_general_knowledge_query:
                # Check data sources for relevance to the query
                relevance_score = self._evaluate_data_source_relevance(query, source_data)
                if relevance_score < 0.3:  # Threshold for deciding to use general knowledge
                    logger.info(f"Data sources have low relevance ({relevance_score:.2f}) for this query. Using general knowledge mode.")
                    using_general_knowledge = True
                    prompt = self._create_general_knowledge_prompt(query, source_data, intent, tone, conversation_history)
                    prompt_version = "general_knowledge"
            
            # Only route to the standard prompts if not using general knowledge mode
            if not using_general_knowledge:
                # For comparison queries, use a specialized formatting approach
                if is_comparison_query:
                    prompt = self._create_comparison_prompt(query, source_data, intent, tone, conversation_history)
                    prompt_version = "comparison_specialized"
                # Route to the appropriate prompt based on intent and A/B test selection
                elif prompt_version == "intent_specialized":
                    prompt = self._create_intent_based_prompt(query, source_data, intent, tone, conversation_history)
                else:
                    # Use the general analysis prompt
                    formatted_context = self.build_context(source_data)
                    history_context = "" if conversation_history.is_empty() else conversation_history.get_history_as_context()
                    prompt = self._build_analysis_prompt(formatted_context, query, list(source_data.keys()), history_context)
            
            # Generate response
            response_text = await self.llm_service.generate_completion(
                prompt, 
                max_tokens=self.max_response_tokens
            )
            
            # Extract confidence score from response if available
            confidence_score, cleaned_response = self._extract_confidence_score(response_text)
            
            # For general knowledge responses, ensure we're clear about the source
            if using_general_knowledge and "[GENERAL KNOWLEDGE]" not in cleaned_response:
                cleaned_response = f"[GENERAL KNOWLEDGE] {cleaned_response}"
                
            # Clean up the formatting of the response, especially for tables and comparisons
            cleaned_response = self._format_response_for_readability(cleaned_response, is_comparison_query)
            
            # Update A/B testing stats
            self._update_prompt_stats(prompt_version, intent, confidence_score)
            
            # Add this exchange to conversation history
            conversation_history.add_exchange(
                query=query,
                response=cleaned_response,
                sources=source_data,
                confidence=confidence_score,
                metadata={
                    "prompt_version": prompt_version,
                    "intent": intent,
                    "tone": tone,
                    "source_count": len(source_data),
                    "using_general_knowledge": using_general_knowledge
                }
            )
            
            # Return enhanced response with confidence and metadata
            return {
                "response": cleaned_response, 
                "confidence": confidence_score,
                "metadata": {
                    "prompt_version": prompt_version,
                    "intent": intent,
                    "tone": tone,
                    "source_count": len(source_data),
                    "is_followup": has_related_history,
                    "conversation_id": conversation_history.conversation_id,
                    "using_general_knowledge": using_general_knowledge
                }
            }
            
        except Exception as e:
            logger.error(f"Error generating analysis: {str(e)}", exc_info=True)
            return {
                "response": f"Error generating analysis: {str(e)}", 
                "confidence": 0.0,
                "metadata": {"error": str(e)}
            }

    def _get_or_create_history(self, user_id: str) -> ConversationHistory:
        """Get existing conversation history or create a new one"""
        # Clean up expired conversations first
        self._cleanup_expired_histories()
        
        # Anonymous users get a new history each time
        if not user_id:
            return ConversationHistory()
            
        # Create or retrieve existing history
        if user_id not in self.conversation_histories:
            self.conversation_histories[user_id] = ConversationHistory()
            
        return self.conversation_histories[user_id]
        
    def _cleanup_expired_histories(self) -> None:
        """Remove conversation histories that have expired"""
        current_time = time.time()
        expired_ids = []
        
        for user_id, history in self.conversation_histories.items():
            if current_time - history.last_updated > self.history_expiry:
                expired_ids.append(user_id)
                
        for user_id in expired_ids:
            del self.conversation_histories[user_id]
            
        if expired_ids:
            logger.info(f"Cleaned up {len(expired_ids)} expired conversation histories")

    def _create_intent_based_prompt(self, query: str, source_data: Dict[str, Any], intent: str, 
                                   tone: str = "formal", conversation_history: ConversationHistory = None) -> str:
        """Create a specialized prompt based on detected query intent
        
        Args:
            query: The user's query
            source_data: Dictionary of data from different sources
            intent: The detected intent (factual, analytical, comparative, list, etc.)
            tone: The desired tone (formal or casual)
            conversation_history: Optional conversation history for context
            
        Returns:
            A specialized prompt tailored to the intent
        """
        # Format context data
        formatted_context = self.build_context(source_data)
        mentioned_sources = list(source_data.keys())
        
        # Add conversation history if available
        history_context = ""
        if conversation_history and not conversation_history.is_empty():
            history_context = conversation_history.get_history_as_context()
        
        # Define prompt templates for different intents and tones
        PROMPT_TEMPLATES = {
            "factual": {
                "formal": (
                    "PROMPT VERSION: v1.6-factual-formal\n\n"
                    "QUERY INTENT: factual\n\n"
                    "You are a precise research assistant responding to factual queries. "
                    "Focus on specific, verifiable information from the provided context. "
                    "For factual questions, provide direct, clear answers with specific data points. "
                    "Cite source identifiers when available."
                ),
                "casual": (
                    "PROMPT VERSION: v1.6-factual-casual\n\n"
                    "QUERY INTENT: factual\n\n"
                    "You're a helpful teammate answering a factual question. "
                    "Keep it straightforward and conversational, but make sure "
                    "your facts are spot-on. Mention where the info comes from when it helps."
                )
            },
            "analytical": {
                "formal": (
                    "PROMPT VERSION: v1.6-analytical-formal\n\n"
                    "QUERY INTENT: analytical\n\n"
                    "You are an analytical assistant synthesizing multiple sources of information. "
                    "For this analysis question, evaluate patterns, connections, and implications. "
                    "Organize insights logically, focusing on the most significant findings first. "
                    "Provide a balanced perspective based solely on the available information."
                ),
                "casual": (
                    "PROMPT VERSION: v1.6-analytical-casual\n\n"
                    "QUERY INTENT: analytical\n\n"
                    "Think of yourself as the team's go-to analyst, but keep it conversational. "
                    "For this analysis question, connect the dots between different pieces of info, "
                    "highlight what matters most, and share meaningful insights without getting too formal. "
                    "Use examples when they help make your point."
                )
            },
            "comparative": {
                "formal": (
                    "PROMPT VERSION: v1.6-comparative-formal\n\n"
                    "QUERY INTENT: comparative\n\n"
                    "You are a specialized assistant focusing on comparison and contrast. "
                    "For this comparative question, methodically evaluate similarities and differences "
                    "between the items in question. Organize by relevant dimensions, emphasize significant "
                    "distinctions, and provide a balanced assessment."
                ),
                "casual": (
                    "PROMPT VERSION: v1.6-comparative-casual\n\n"
                    "QUERY INTENT: comparative\n\n"
                    "You're helping a teammate understand how things compare. "
                    "Break down the key similarities and differences in a friendly, "
                    "accessible way. Focus on what matters most for practical decision-making, "
                    "and use simple examples when they help."
                )
            },
            "list": {
                "formal": (
                    "PROMPT VERSION: v1.6-list-formal\n\n"
                    "QUERY INTENT: list\n\n"
                    "You are a structured research assistant responding to a list-based query. "
                    "Organize information into clear, categorized lists with concise items. "
                    "Prioritize by relevance or chronology as appropriate. "
                    "Ensure comprehensive coverage of relevant items from the provided sources."
                ),
                "casual": (
                    "PROMPT VERSION: v1.6-list-casual\n\n"
                    "QUERY INTENT: list\n\n" 
                    "You're putting together a helpful list for a teammate. "
                    "Keep it scannable and easy to digest, with the most important "
                    "stuff at the top. Group similar items together when it makes sense, "
                    "and keep each point brief but clear."
                )
            }
        }
        
        # Default to analytical if intent not recognized
        if intent not in PROMPT_TEMPLATES:
            intent = "analytical"
            
        # Get the appropriate template
        tone_templates = PROMPT_TEMPLATES[intent]
        template = tone_templates.get(tone, tone_templates["formal"])
        
        # Build the complete prompt with context and query
        prompt = f"{template}\n\n"
        
        # Add conversation history if available
        if history_context:
            prompt += "CONVERSATION HISTORY:\n"
            prompt += history_context
            prompt += "\n\n"
        
        prompt += "CONTEXT:\n"
        prompt += formatted_context
        prompt += "\n\nQUERY: " + query + "\n\n"
        
        # Add critical instructions
        prompt += "CRITICAL GUIDELINES:\n"
        prompt += "- If data is unavailable, acknowledge it. Do not fabricate answers.\n"
        prompt += "- When useful, reference source IDs like JIRA tickets or Slack timestamps for traceability.\n"
        prompt += "- Respond only using the most relevant sources. Format the answer based on the query type.\n"
        
        # Add instruction to reference conversation history if relevant
        if history_context:
            prompt += "- When responding to follow-up questions, reference relevant information from the conversation history.\n"
            prompt += "- If the current query refers to something mentioned in a previous exchange, use that context.\n"
        
        prompt += "- Before outputting, ensure your answer clearly addresses the question. Revise if unclear.\n"
        prompt += "- Only suggest a follow-up question if it adds genuine value.\n"
        
        # Add confidence score instruction
        prompt += "- Include a confidence indicator using format [Confidence: High/Medium/Low] or [Confidence: 0.X] if data coverage is uneven.\n"
        
        return prompt

    def _extract_confidence_score(self, response_text: str) -> Tuple[float, str]:
        """Extract confidence score from response text if present
        
        Args:
            response_text: The raw response from the LLM
            
        Returns:
            Tuple of (confidence_score, cleaned_response)
        """
        # Default confidence if none found
        confidence = 0.75
        cleaned_response = response_text
        
        # Look for confidence patterns like [Confidence: High] or [Confidence: 0.8]
        confidence_pattern = r'\[Confidence:\s*(High|Medium|Low|[\d\.]+)\]'
        match = re.search(confidence_pattern, response_text)
        
        if match:
            confidence_str = match.group(1)
            # Convert text confidence to numeric
            if confidence_str == "High":
                confidence = 0.9
            elif confidence_str == "Medium":
                confidence = 0.7
            elif confidence_str == "Low":
                confidence = 0.5
            else:
                # Try to convert numeric confidence
                try:
                    confidence = float(confidence_str)
                except ValueError:
                    pass
                    
            # Remove the confidence indicator from the response
            cleaned_response = re.sub(confidence_pattern, '', response_text).strip()
            
        return confidence, cleaned_response
        
    def _update_prompt_stats(self, prompt_version: str, intent: str, confidence: float) -> None:
        """Update statistics for A/B testing different prompt approaches"""
        # In a production system, this would store metrics in a database
        # For now, just log the information
        logger.info(f"Prompt stats update: version={prompt_version}, intent={intent}, confidence={confidence:.2f}")

    def create_prompt(self, user_prompt: str, source_data: Dict[str, Any]) -> str:
        """Create an enhanced prompt with source-specific context for direct, focused responses"""
        context = self.build_context(source_data)
        question_info = self._determine_question_type(user_prompt)
        
        # Get available data sources
        available_sources = list(source_data.keys())
        
        # Add version tag for tracking and debugging
        prompt_version = "v1.5-adaptive-intent-based"
        
        # Use user's exact "Adaptive, Intent-Based Response Generation" prompt with enhancements
        system_context = f"""PROMPT VERSION: {prompt_version}

You are an intelligent assistant. You must analyze the user's query and generate a response in the most effective and natural format, depending on the nature of the question.

Step 1: Detect the Query Intent

Classify the query as factual, analytical, comparative, listing, or insight-based.

Step 2: Gather Relevant Data

Use available information from Slack, JIRA, Google Docs, and Google Sheets.

Extract only the most relevant, timely, and high-confidence data.

Step 3: Format Your Response Intelligently

Be concise and clear for direct factual questions.

Use comparative language or structures for contrast-based queries.

Use bulleted lists or tables for list-style requests.

Use summaries or explanations for analytical queries.

Provide context only when it enhances understanding—avoid unnecessary detail.

Step 4: Avoid Rigid Templates

Adapt your response style based on what would best help the user understand the answer.

Your tone should feel insightful, human, and structured only as needed.

Goal: The final output should feel natural, helpful, and tailored to the query—not boilerplate. Make it sound like a thoughtful human summarizing cross-source info smartly.

CRITICAL GUIDELINES:
- If data is unavailable, acknowledge it. Do not fabricate answers.
- When useful, reference source IDs like JIRA tickets or Slack timestamps for traceability.
- Only include sources that are clearly relevant to the query. Skip irrelevant sources.
- Do not narrate your reasoning process. Just give the most direct, helpful response."""
        
        # Build the final prompt
        prompt = f"""
{system_context}

QUERY: {user_prompt}
QUERY INTENT: {question_info["type"]}

DATA SOURCES:
{context}

Respond only using the most relevant sources. Format the answer based on the query type. Avoid irrelevant details.
"""
        
        # Log the prompt for debugging
        logger.debug(f"Generated prompt with version {prompt_version}")
        
        return prompt

    def _build_analysis_prompt(self, formatted_context: str, query: str, mentioned_sources: List[str], 
                               history_context: str = "") -> str:
        """Build a comprehensive analysis prompt optimized for synthesizing insights across multiple data sources"""
        query_lower = query.lower()
        question_info = self._determine_question_type(query)
        
        # Add version tag for tracking and debugging
        prompt_version = "v1.5-multi-source-synthesis"
        
        # Detect data sources in the context
        available_sources = []
        if 'SOURCE: SLACK' in formatted_context:
            available_sources.append('Slack')
        if 'SOURCE: JIRA' in formatted_context:
            available_sources.append('JIRA')
        if 'SOURCE: GOOGLE_DOCS' in formatted_context:
            available_sources.append('Google Docs')
        if 'SOURCE: GOOGLE_SHEETS' in formatted_context:
            available_sources.append('Google Sheets')
        
        # Use user's exact "Master Prompt for Multi-Source Query Intelligence" prompt with minor enhancements
        system_instructions = f"""PROMPT VERSION: {prompt_version}

You are an intelligent assistant that processes user queries by analyzing data across multiple sources: Slack, JIRA, Google Docs, and Google Sheets.

Step 0: Response Tone
Respond like a knowledgeable team member summarizing findings — helpful, clear, no fluff.

Follow this reasoning process to produce accurate and context-aware responses:

Step 1: Analyze the Query

Identify the intent: fact, comparison, analysis, or list.

Determine if the query is direct or open-ended.

Identify which data sources are most relevant to this query.

Pinpoint specific fields or content to extract from each relevant source.

Step 2: Source-Specific Analysis

Slack: Pull high-signal messages, decisions, blockers, mentions, and timestamps relevant to the query.

JIRA: Filter issues by status, assignee, comments, and timestamps; prioritize highly relevant ones.

Google Docs: Analyze document headings, sections, and semantic similarity to query; summarize key paragraphs.

Google Sheets: Identify relevant tabs or columns; apply logic to extract metrics, calculations, and trends.

Step 3: Formulate the Response

For direct questions: Provide concise, specific answers with brief evidence or context.

For comparisons: Highlight differences and similarities clearly.

For analytical queries: Synthesize findings across multiple sources.

For lists: Present bullet points or tables with clear categorization.

Step 4: Intelligent Adaptation

Avoid rigid formats—adapt dynamically to the nature of the query.

Only include high-confidence, high-relevance information.

Use markdown (e.g., lists, tables, bolding) where it helps clarity.

Step 5: Multi-Source Integration

Combine related information from Slack, JIRA, Docs, and Sheets.

Flag alignments or contradictions across sources.

Deliver a unified, human-readable summary that directly answers the user's query.

Output: Return the response in a format that best fits the query—paragraph, list, or table. Include source references only if they help the user.

CRITICAL GUIDELINES:
- Include a confidence score at the end in the format: [CONFIDENCE: X/10] where X is 1-10
- If data is unavailable, acknowledge it. Do not fabricate answers.
- When useful, reference source IDs like JIRA tickets or Slack timestamps for traceability.
- Only include sources that are clearly relevant to the query. Skip irrelevant sources entirely.
- Do not narrate your reasoning process. Just give the most direct, helpful response.
- Add a confidence indicator (e.g., "High confidence" or "Some data may be incomplete") if data coverage is uneven.
- Only suggest a follow-up question if it adds genuine value to the user's understanding.
- Before outputting, ensure your answer clearly addresses the question. Revise if unclear."""
        
        # Add conversation history section if available
        if history_context:
            system_instructions += "\n\nStep 6: Conversation Continuity\n"
            system_instructions += "Reference relevant information from previous conversation exchanges when answering follow-up questions.\n"
            system_instructions += "Maintain context between related queries.\n"
            system_instructions += "If the user refers to something mentioned in a previous exchange, use that context in your response."
        
        # Final instructions
        final_instructions = f"""
QUERY: {query}
QUERY INTENT: {question_info["type"]}

"""

        # Add conversation history if available
        if history_context:
            final_instructions += f"""CONVERSATION HISTORY:
{history_context}

"""

        final_instructions += f"""AVAILABLE DATA SOURCES:
{formatted_context}

ANALYSIS GUIDANCE: Return the response in a format that best fits the query—paragraph, list, or table. Include source references only if they help the user. Skip irrelevant sources. Be helpful, clear, and direct with no fluff. Add your confidence score at the end.
"""
        
        # Combine all parts into the final prompt
        full_prompt = system_instructions + final_instructions
        
        # Log the prompt for debugging
        logger.debug(f"Generated analysis prompt with version {prompt_version}")
        
        return full_prompt
    
    def format_response(self, llm_response: str, source_data: Dict[str, Any]) -> Dict[str, Any]:
        """Format the LLM response with source attribution and structure"""
        return {
            "response": llm_response,
            "sources": list(source_data.keys()),
            "metadata": {
                "timestamp": "now",  # You might want to use actual timestamp
                "source_count": len(source_data)
            }
        }

    async def _call_llm(self, prompt: str, max_tokens: int = 1500, temperature: float = 0.7) -> str:
        """Make an API call to the Azure OpenAI model with fail-safes against rate limits"""
        try:
            # Azure OpenAI headers
            headers = {
                "Content-Type": "application/json",
                "api-key": OPENAI_API_KEY,
                "Accept": "application/json"
            }
            
            # Construct Azure OpenAI URL
            azure_url = f"{AZURE_OPENAI_ENDPOINT}openai/deployments/{AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}"
            
            # Debug logging to check actual values being used
            logger.info(f"Context Manager Azure OpenAI Configuration Debug:")
            logger.info(f"  Endpoint: {AZURE_OPENAI_ENDPOINT}")
            logger.info(f"  Deployment: {AZURE_OPENAI_DEPLOYMENT}")
            logger.info(f"  API Version: {AZURE_OPENAI_API_VERSION}")
            logger.info(f"  Full URL: {azure_url}")
            logger.info(f"  API Key (first 10 chars): {OPENAI_API_KEY[:10]}...")
            
            # Enforce lower token counts for production safety
            safe_max_tokens = min(max_tokens, 1500)
            
            payload = {
                "messages": [
                    {
                        "role": "system",
                        "content": "You are an AI assistant trained to provide precise, focused responses."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "max_tokens": safe_max_tokens,
                "temperature": temperature,
                "top_p": 0.95,
                "frequency_penalty": 0,
                "presence_penalty": 0
            }
            
            response = requests.post(
                azure_url,
                headers=headers,
                json=payload,
                verify=certifi.where(),
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                return result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            elif response.status_code == 429:  # Rate limit error
                logger.error("Azure OpenAI API rate limit exceeded. Returning fallback response.")
                return "I'm sorry, I couldn't complete the analysis at this time due to system limits. Please try again later with a more specific question or fewer data sources."
            else:
                logger.error(f"Azure OpenAI API error: {response.status_code}")
                logger.error(f"Response: {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Error calling Azure OpenAI: {str(e)}")
            return None

    def _chunk_data(self, data: Dict[str, Any], prompt: str) -> List[Dict[str, Any]]:
        """Split data into chunks that fit within token limits"""
        chunks = []
        current_chunk = {}
        current_tokens = 0
        system_tokens = self.count_tokens("You are an AI assistant trained to provide precise, focused responses.")
        prompt_tokens = self.count_tokens(prompt)
        base_tokens = system_tokens + prompt_tokens + self.max_response_tokens
        max_chunk_tokens = self.max_tokens - base_tokens
        
        for source_name, source_data in data.items():
            # Create a test prompt with just this source
            test_chunk = {source_name: source_data}
            test_prompt = self.create_prompt(prompt, test_chunk)
            source_tokens = self.count_tokens(test_prompt)
            
            if source_tokens > max_chunk_tokens:
                # If a single source is too large, we need to split its content
                logger.warning(f"Source {source_name} is too large ({source_tokens} tokens). Truncating...")
                truncated_data = self._truncate_source(source_data, max_chunk_tokens)
                chunks.append({source_name: truncated_data})
            else:
                if current_tokens + source_tokens <= max_chunk_tokens:
                    current_chunk[source_name] = source_data
                    current_tokens += source_tokens
                else:
                    if current_chunk:
                        chunks.append(current_chunk)
                    current_chunk = {source_name: source_data}
                    current_tokens = source_tokens
        
        if current_chunk:
            chunks.append(current_chunk)
        
        return chunks

    def _truncate_source(self, data: Any, max_tokens: int) -> Any:
        """Intelligently truncate source data to fit within token limit"""
        if isinstance(data, str):
            encoded = self.encoding.encode(data)
            if len(encoded) > max_tokens:
                return self.encoding.decode(encoded[:max_tokens]) + "... [truncated]"
            return data
        elif isinstance(data, dict):
            result = {}
            remaining_tokens = max_tokens
            for key, value in data.items():
                value_tokens = self.count_tokens(str(value))
                if value_tokens <= remaining_tokens:
                    result[key] = value
                    remaining_tokens -= value_tokens
                else:
                    result[key] = self._truncate_source(value, remaining_tokens)
                    break
            return result
        elif isinstance(data, list):
            result = []
            remaining_tokens = max_tokens
            for item in data:
                item_tokens = self.count_tokens(str(item))
                if item_tokens <= remaining_tokens:
                    result.append(item)
                    remaining_tokens -= item_tokens
                else:
                    break
            return result
        return data 

    def _smart_truncate_jira_projects(self, formatted_text: str, budget: int, project_metadata: Dict[str, Dict]) -> str:
        """Intelligently truncate JIRA data by project, giving more tokens to more relevant projects.
        
        Args:
            formatted_text: The formatted JIRA data text
            budget: The token budget to stay within
            project_metadata: Dictionary mapping project keys to metadata dicts with relevance scores
            
        Returns:
            Truncated text that fits within budget while preserving project sections
        """
        # If we're already within budget, return as is
        if len(formatted_text.split()) <= budget:
            return formatted_text
            
        # Split by project sections
        sections = []
        current_section = []
        current_project_key = None
        header_pattern = r"## PROJECT: ([A-Z0-9_-]+)"
        
        # Parse the text into sections by project
        for line in formatted_text.split('\n'):
            import re
            match = re.search(header_pattern, line)
            
            if match:
                # If we were already building a section, save it
                if current_section:
                    sections.append({
                        'project_key': current_project_key,
                        'content': '\n'.join(current_section)
                    })
                    
                # Start a new section
                current_project_key = match.group(1)
                current_section = [line]
            elif current_section is not None:
                current_section.append(line)
        
        # Add the last section if there is one
        if current_section:
            sections.append({
                'project_key': current_project_key,
                'content': '\n'.join(current_section)
            })
            
        # Extract the overview section (always keep it)
        overview_section = None
        project_sections = []
        
        for section in sections:
            if "## Overview" in section['content']:
                overview_section = section['content']
            elif section['project_key']:
                project_sections.append(section)
                
        # If no valid project sections found, fall back to standard truncation
        if not project_sections:
            logging.warning("No valid project sections found in JIRA data. Using standard truncation.")
            return formatted_text[:budget * 4]  # Rough approximation of tokens to chars
            
        # Calculate token estimates for each section
        overview_tokens = len(overview_section.split()) if overview_section else 0
        remaining_budget = budget - overview_tokens
        
        # If we don't have budget for projects after overview, truncate overview and return
        if remaining_budget <= 0:
            logging.warning("No budget remaining for project sections after overview.")
            return overview_section[:budget * 4]  # Rough approximation
            
        # Sort sections by relevance if we have metadata
        if project_metadata:
            for section in project_sections:
                # Get relevance score for this project, default to 0.1
                project_key = section['project_key']
                if project_key in project_metadata:
                    metadata = project_metadata[project_key]
                    if isinstance(metadata, dict):
                        section['relevance'] = metadata.get('relevance', 0.1)
                    else:
                        # Handle case where metadata might be a float (for backward compatibility)
                        section['relevance'] = float(metadata) if metadata is not None else 0.1
                else:
                    section['relevance'] = 0.1
                
            # Sort by relevance (highest first)
            project_sections = sorted(project_sections, key=lambda x: x.get('relevance', 0.1), reverse=True)
            
        # Allocate tokens based on relevance
        total_relevance = sum(section.get('relevance', 1.0) for section in project_sections)
        
        # Ensure each project gets at least a minimum allocation
        min_allocation = 200  # Minimum tokens per project
        total_min_allocation = min_allocation * len(project_sections)
        
        # If we can't even give minimum allocation, reduce the number of projects
        if total_min_allocation > remaining_budget:
            max_projects = remaining_budget // min_allocation
            project_sections = project_sections[:max_projects]
            total_min_allocation = min_allocation * len(project_sections)
            
        # Distribute remaining tokens proportionally based on relevance
        remaining_after_min = remaining_budget - total_min_allocation
        
        # Allocate budgets
        for section in project_sections:
            relevance = section.get('relevance', 1.0)
            # Base allocation plus proportional share of remaining budget
            if total_relevance > 0:
                section['budget'] = min_allocation + int((relevance / total_relevance) * remaining_after_min)
            else:
                # Equal distribution if no relevance scores
                section['budget'] = remaining_budget // len(project_sections)
                
        # Truncate each section to fit its budget
        truncated_sections = []
        
        if overview_section:
            truncated_sections.append(overview_section)
            
        for section in project_sections:
            section_content = section['content']
            section_tokens = len(section_content.split())
            section_budget = section['budget']
            
            # If section fits within budget, use it as is
            if section_tokens <= section_budget:
                truncated_content = section_content
            else:
                # Truncate to fit budget, preserving important parts
                words = section_content.split()
                truncated_content = ' '.join(words[:section_budget])
                truncated_content += "\n\n*Note: This project data has been truncated to fit within token limits.*"
                
            # Add relevance score for clarity
            if 'relevance' in section:
                # Find the project header line
                lines = truncated_content.split('\n')
                for i, line in enumerate(lines):
                    if line.startswith("## PROJECT:"):
                        # Add relevance info
                        lines[i] = f"{line} (Relevance: {section['relevance']:.2f})"
                        break
                truncated_content = '\n'.join(lines)
                
            truncated_sections.append(truncated_content)
            
        return '\n\n'.join(truncated_sections)

    def _calculate_source_budget(self, source_name: str, relevance: float) -> int:
        """Calculate token budget for a source based on its relevance.
        
        Args:
            source_name: Name of the data source
            relevance: Relevance score (0 to 1)
            
        Returns:
            Token budget allocated to this source
        """
        # Base allocation is proportional to relevance
        # Higher relevance = more tokens
        max_budget = self.max_tokens_prompt - 1000  # Reserve 1000 tokens for prompt structure
        
        # Ensure minimum budget for any source
        min_budget = 1000
        
        # Calculate budget - higher relevance gets more tokens
        # Scale from min_budget to max_budget * 0.6 (never give one source all tokens)
        budget = min_budget + int((max_budget * 0.6 - min_budget) * relevance)
        
        # Special handling for different source types
        if source_name.lower() == 'jira':
            # JIRA data tends to be more structured and dense, so allocate more
            budget = int(budget * 1.3)
        elif source_name.lower() == 'slack':
            # Slack data can be verbose with many messages, allocate more
            budget = int(budget * 1.2)
        
        # Cap at 70% of max to ensure we don't use all tokens on one source
        return min(int(max_budget * 0.7), budget) 