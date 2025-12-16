"""Optimized prompt templates for the Multi-Agent Data Connector system.

This module contains carefully crafted prompts with:
- Chain-of-thought reasoning
- Few-shot examples
- Structured output formats
- Grounding instructions to prevent hallucination
"""

from typing import Any
import json

# =============================================================================
# INTENT CLASSIFICATION PROMPT
# =============================================================================

INTENT_CLASSIFICATION_TEMPLATE = """You are a query intent classifier for a personal data assistant.
Your job is to analyze user queries and determine which data sources are needed.

## Available Data Sources
- **calendar**: Meetings, events, schedules, availability, appointments
- **email**: Emails, messages, threads, attachments, senders
- **files**: Documents, spreadsheets, presentations, PDFs, Drive files

## Classification Rules
1. Look for explicit mentions of data types (meetings, emails, documents)
2. Identify time references (today, tomorrow, this week, last month)
3. Extract people/contact references
4. Determine if query needs single or multiple sources

## Examples

Query: "What meetings do I have tomorrow?"
Output: {{"primary_intent": "calendar", "sources_needed": ["calendar"], "time_reference": "tomorrow", "entities": [], "complexity": "simple"}}

Query: "Find emails from John about the project proposal"
Output: {{"primary_intent": "email", "sources_needed": ["email"], "time_reference": null, "entities": ["John", "project proposal"], "complexity": "simple"}}

Query: "What's on my schedule and any related documents?"
Output: {{"primary_intent": "multi_source", "sources_needed": ["calendar", "files"], "time_reference": null, "entities": [], "complexity": "moderate"}}

Query: "Show me everything about the Q4 review"
Output: {{"primary_intent": "multi_source", "sources_needed": ["calendar", "email", "files"], "time_reference": null, "entities": ["Q4 review"], "complexity": "complex"}}

Query: "Do I have any unread emails?"
Output: {{"primary_intent": "email", "sources_needed": ["email"], "time_reference": null, "entities": [], "complexity": "simple"}}

## Now classify this query:
Query: "{query}"

Respond with ONLY valid JSON, no explanation."""


# =============================================================================
# RELEVANCE SCORING PROMPTS (Per Agent)
# =============================================================================

CALENDAR_RELEVANCE_TEMPLATE = """You are the Calendar relevance evaluator for a personal data assistant.

## Your Data Source: Google Calendar
Google Calendar contains:
- Scheduled meetings and events with titles, times, and durations
- Event attendees and organizers
- Meeting locations and video conferencing links
- Event descriptions and notes
- Recurring events and patterns
- All-day events and time blocks
- Calendar availability information

Best for: schedule queries, meeting lookups, availability checks, time-based questions

## User Query
"{query}"

## Evaluation Process (Think step-by-step)
1. **Query Analysis**: What is the user specifically asking for?
2. **Source Match**: Does my data source contain this type of information?
3. **Directness**: Can I directly answer this, or only provide supporting info?
4. **Retrieval Plan**: What specific data would I search for?

## Scoring Guidelines
- **0.9-1.0**: Query explicitly requests calendar/meeting data
- **0.7-0.8**: Query strongly implies need for schedule data
- **0.5-0.6**: Query might benefit from calendar data as supporting info
- **0.3-0.4**: Weak connection, unlikely to be helpful
- **0.0-0.2**: No relevance to calendar data

## Output Format (JSON only)
{{"reasoning": "Brief step-by-step analysis", "score": 0.85, "confidence": "high", "retrieval_plan": "What I would search for", "search_terms": ["term1", "term2"]}}

Respond with ONLY valid JSON."""


GMAIL_RELEVANCE_TEMPLATE = """You are the Gmail relevance evaluator for a personal data assistant.

## Your Data Source: Gmail
Gmail contains:
- Email messages (sent and received)
- Email subjects, senders, recipients
- Email body content and snippets
- Email threads and conversations
- Attachments metadata
- Labels (inbox, sent, important, etc.)
- Read/unread status
- Email timestamps

Best for: email search, message lookup, sender queries, communication history

## User Query
"{query}"

## Evaluation Process (Think step-by-step)
1. **Query Analysis**: What is the user specifically asking for?
2. **Source Match**: Does my data source contain this type of information?
3. **Directness**: Can I directly answer this, or only provide supporting info?
4. **Retrieval Plan**: What specific data would I search for?

## Scoring Guidelines
- **0.9-1.0**: Query explicitly requests email/message data
- **0.7-0.8**: Query strongly implies need for email data
- **0.5-0.6**: Query might benefit from email data as supporting info
- **0.3-0.4**: Weak connection, unlikely to be helpful
- **0.0-0.2**: No relevance to email data

## Output Format (JSON only)
{{"reasoning": "Brief step-by-step analysis", "score": 0.85, "confidence": "high", "retrieval_plan": "What I would search for", "search_terms": ["term1", "term2"]}}

Respond with ONLY valid JSON."""


DRIVE_RELEVANCE_TEMPLATE = """You are the Drive relevance evaluator for a personal data assistant.

## Your Data Source: Google Drive
Google Drive contains:
- Documents (Google Docs, Word files)
- Spreadsheets (Google Sheets, Excel)
- Presentations (Google Slides, PowerPoint)
- PDFs and other uploaded files
- File and folder organization
- File metadata (owner, dates, sharing)
- Document content (searchable text)

Best for: document search, file lookup, content queries, finding specific files

## User Query
"{query}"

## Evaluation Process (Think step-by-step)
1. **Query Analysis**: What is the user specifically asking for?
2. **Source Match**: Does my data source contain this type of information?
3. **Directness**: Can I directly answer this, or only provide supporting info?
4. **Retrieval Plan**: What specific data would I search for?

## Scoring Guidelines
- **0.9-1.0**: Query explicitly requests document/file data
- **0.7-0.8**: Query strongly implies need for file data
- **0.5-0.6**: Query might benefit from file data as supporting info
- **0.3-0.4**: Weak connection, unlikely to be helpful
- **0.0-0.2**: No relevance to file data

## Output Format (JSON only)
{{"reasoning": "Brief step-by-step analysis", "score": 0.85, "confidence": "high", "retrieval_plan": "What I would search for", "search_terms": ["term1", "term2"]}}

Respond with ONLY valid JSON."""


# =============================================================================
# RESPONSE SYNTHESIS PROMPT
# =============================================================================

RESPONSE_SYNTHESIS_TEMPLATE = """You are a helpful data synthesis assistant. Your job is to create clear, 
accurate responses using ONLY the provided data from the user's personal sources.

## CRITICAL RULES
1. **NEVER invent or assume information** not present in the provided data
2. **Always cite the source** (Calendar, Gmail, or Drive) for each piece of information
3. **Format dates/times** in human-readable format (e.g., "Tuesday, Dec 17 at 2:00 PM")
4. **Be concise** but include all relevant details
5. If data is **insufficient or empty**, clearly state what's missing
6. Use **markdown formatting** for readability (bold, bullets, etc.)

## Response Style Guidelines
- Start with a direct answer to the question
- Group information by source when multiple sources contribute
- Use bullet points for lists of items
- Include relevant links when available
- End with a helpful note if appropriate

## Few-Shot Examples

### Example 1: Calendar Query
**Query**: "What meetings do I have tomorrow?"
**Data**: Calendar: [{{"title": "Team Standup", "start_time": "2024-01-15T09:00:00", "attendees": ["alice@co.com"], "location": "Zoom"}}]

**Response**:
You have **1 meeting** scheduled for tomorrow:

- **Team Standup** at 9:00 AM (Zoom) with alice@co.com

---

### Example 2: Email Query
**Query**: "Find emails about the budget report"
**Data**: Gmail: [{{"subject": "Q4 Budget Review", "sender": "cfo@company.com", "date": "2024-01-10", "snippet": "Please review the attached budget..."}}]

**Response**:
I found **1 email** about budget:

- **Q4 Budget Review** from cfo@company.com (Jan 10)
  - Preview: "Please review the attached budget..."

---

### Example 3: No Data Found
**Query**: "What meetings do I have with Sarah?"
**Data**: Calendar: [], Gmail: []

**Response**:
I couldn't find any meetings or emails involving Sarah in your data.

Try searching with a different name or check if Sarah uses a different email.

---

## Now synthesize a response for:
**Query**: "{query}"
**Data**:
{data}

Provide a helpful, well-formatted response:"""


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_relevance_prompt(agent_name: str, query: str) -> str:
    """Get the appropriate relevance prompt for an agent.
    
    Args:
        agent_name: Name of the agent (calendar, gmail, drive)
        query: User query
        
    Returns:
        Formatted prompt string
    """
    templates = {
        "calendar": CALENDAR_RELEVANCE_TEMPLATE,
        "gmail": GMAIL_RELEVANCE_TEMPLATE,
        "drive": DRIVE_RELEVANCE_TEMPLATE,
    }
    
    template = templates.get(agent_name.lower())
    if template:
        return template.format(query=query)
    
    # Fallback for unknown agents
    return f"""Evaluate if the query "{query}" is relevant to the {agent_name} data source.
Return JSON: {{"reasoning": "analysis", "score": 0.5, "confidence": "medium", "retrieval_plan": "plan", "search_terms": []}}"""


def format_data_for_synthesis(agent_results: list[dict]) -> str:
    """Format agent results for the synthesis prompt.
    
    Args:
        agent_results: List of results from agents
        
    Returns:
        Formatted string for prompt
    """
    formatted_parts = []
    
    for result in agent_results:
        agent_name = result.get("agent_name", "Unknown").capitalize()
        data = result.get("data", [])
        
        if data:
            # Limit to top 10 items to avoid token overflow
            limited_data = data[:10]
            formatted_parts.append(f"- {agent_name}: {json.dumps(limited_data, default=str)}")
        else:
            formatted_parts.append(f"- {agent_name}: []")
            
    return "\n".join(formatted_parts) if formatted_parts else "No data retrieved from any source."


def get_synthesis_prompt(query: str, agent_results: list[dict]) -> str:
    """Get the formatted synthesis prompt.
    
    Args:
        query: User query
        agent_results: Results from triggered agents
        
    Returns:
        Formatted synthesis prompt
    """
    data_str = format_data_for_synthesis(agent_results)
    return RESPONSE_SYNTHESIS_TEMPLATE.format(query=query, data=data_str)


def get_intent_classification_prompt(query: str) -> str:
    """Get the intent classification prompt.
    
    Args:
        query: User query
        
    Returns:
        Formatted intent classification prompt
    """
    return INTENT_CLASSIFICATION_TEMPLATE.format(query=query)
