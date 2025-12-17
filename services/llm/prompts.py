"""Optimized prompt templates for the Multi-Agent Data Connector system.

This module contains carefully crafted prompts with:
- Chain-of-thought reasoning
- Few-shot examples
- Structured output formats
- Grounding instructions to prevent hallucination
- Action intent classification
- Current date/time awareness for temporal queries
- Dynamic date/time extraction from natural language
"""

from typing import Any, Optional
from datetime import datetime, timedelta
import json


# =============================================================================
# DATE/TIME CONTEXT HELPER
# =============================================================================

def get_current_datetime_context() -> str:
    """Generate current date/time context string for LLM prompts.
    
    Returns:
        str: Formatted string with current date, time, and temporal context
        
    Examples:
        >>> context = get_current_datetime_context()
        >>> "Current Date:" in context
        True
    """
    now = datetime.now()
    return f"""
## Current Date and Time Context
- **Today's Date**: {now.strftime('%A, %B %d, %Y')}
- **Current Time**: {now.strftime('%I:%M %p')} (Local Time)
- **Day of Week**: {now.strftime('%A')}
- **Week Number**: {now.isocalendar()[1]} of {now.year}
- **ISO Date**: {now.strftime('%Y-%m-%d')}
- **Current Year**: {now.year}

Use this context to interpret relative time references like "today", "tomorrow", "this week", "next Monday", etc.
When a year is not specified, assume the current year ({now.year}) or the next occurrence of the date.
"""


# =============================================================================
# DATE EXTRACTION TEMPLATE
# =============================================================================

DATE_EXTRACTION_TEMPLATE = """You are a date/time extraction specialist. Extract date and time ranges from natural language queries.

{datetime_context}

## User Query
"{query}"

## Extraction Rules
1. Convert all relative references (today, tomorrow, next week, last Monday) to absolute dates
2. If only a start date is mentioned, infer a reasonable end date:
   - Single day mentions → end = start + 1 day
   - Week mentions → end = start + 7 days
   - Month mentions → end = start + 30 days
3. If no year is specified, use current year or next occurrence
4. For time ranges like "December 22-28", extract both start and end
5. For vague queries with no date reference, return null values
6. Parse specific dates like "Dec 22", "December 22nd", "22/12/2024", "2024-12-22"

## Time Reference Types
- **specific_date**: A specific date mentioned (e.g., "December 22", "Jan 15 2025")
- **specific_range**: A date range (e.g., "December 22-28", "from Monday to Friday")
- **relative_day**: Relative single day (e.g., "today", "tomorrow", "yesterday")
- **relative_range**: Relative range (e.g., "this week", "next month", "last 7 days")
- **none**: No clear time reference in query

## Output Format (JSON only)
{{
    "has_date_reference": true/false,
    "time_reference_type": "specific_date|specific_range|relative_day|relative_range|none",
    "original_reference": "the exact text from query referring to time, or null",
    "start_date": "YYYY-MM-DDTHH:MM:SS" or null,
    "end_date": "YYYY-MM-DDTHH:MM:SS" or null,
    "confidence": "high|medium|low"
}}

## Examples

Query: "What meetings do I have on December 22nd?"
Output: {{"has_date_reference": true, "time_reference_type": "specific_date", "original_reference": "December 22nd", "start_date": "2024-12-22T00:00:00", "end_date": "2024-12-23T00:00:00", "confidence": "high"}}

Query: "Show me emails from December 22 to 28"
Output: {{"has_date_reference": true, "time_reference_type": "specific_range", "original_reference": "December 22 to 28", "start_date": "2024-12-22T00:00:00", "end_date": "2024-12-29T00:00:00", "confidence": "high"}}

Query: "What's on my calendar for next Friday?"
Output: {{"has_date_reference": true, "time_reference_type": "relative_day", "original_reference": "next Friday", "start_date": "2024-12-20T00:00:00", "end_date": "2024-12-21T00:00:00", "confidence": "high"}}

Query: "Find emails from last week"
Output: {{"has_date_reference": true, "time_reference_type": "relative_range", "original_reference": "last week", "start_date": "2024-12-09T00:00:00", "end_date": "2024-12-16T00:00:00", "confidence": "high"}}

Query: "What meetings do I have with John?"
Output: {{"has_date_reference": false, "time_reference_type": "none", "original_reference": null, "start_date": null, "end_date": null, "confidence": "high"}}

Query: "Show calendar for Jan 15-20, 2025"
Output: {{"has_date_reference": true, "time_reference_type": "specific_range", "original_reference": "Jan 15-20, 2025", "start_date": "2025-01-15T00:00:00", "end_date": "2025-01-21T00:00:00", "confidence": "high"}}

Respond with ONLY valid JSON."""


def get_date_extraction_prompt(query: str) -> str:
    """Get the date extraction prompt with current datetime context.
    
    Args:
        query: User query to extract dates from
        
    Returns:
        Formatted date extraction prompt
    """
    return DATE_EXTRACTION_TEMPLATE.format(
        query=query,
        datetime_context=get_current_datetime_context()
    )

# =============================================================================
# INTENT CLASSIFICATION PROMPT
# =============================================================================

INTENT_CLASSIFICATION_TEMPLATE = """You are a query intent classifier for a personal data assistant.
Your job is to analyze user queries and determine:
1. Whether this is a READ query (retrieving info) or an ACTION query (doing something)
2. Which data sources are needed
3. What specific action to take (if applicable)

{datetime_context}

## Available Data Sources
- **calendar**: Meetings, events, schedules, availability, appointments
- **email**: Emails, messages, threads, attachments, senders
- **files**: Documents, spreadsheets, presentations, PDFs, Drive files
- **slack**: Slack messages, team discussions, channel conversations, DMs

## Query Types
- **read**: User wants to retrieve/view information
- **action**: User wants to perform an action (create, send, update, delete, share)
- **workflow**: User wants a multi-step operation combining actions

## Available Actions
Calendar: create_event, update_event, delete_event, check_availability
Email: send_email, create_draft, reply_email, forward_email  
Files: create_document, create_spreadsheet, share_file, create_folder

## Classification Rules
1. Look for action verbs: "schedule", "create", "send", "reply", "share", "delete", "update"
2. Distinguish reads: "show", "find", "what", "list", "search" = read queries
3. Identify time references and entities
4. Determine if confirmation is needed (sends, deletes = yes; drafts = no)

## Examples

Query: "What meetings do I have tomorrow?"
Output: {{"query_type": "read", "primary_intent": "calendar", "sources_needed": ["calendar"], "action_type": null, "time_reference": "tomorrow", "entities": [], "requires_confirmation": false, "complexity": "simple"}}

Query: "Schedule a meeting with john@company.com tomorrow at 2pm about project review"
Output: {{"query_type": "action", "primary_intent": "calendar", "sources_needed": ["calendar"], "action_type": "create_event", "time_reference": "tomorrow 2pm", "entities": ["john@company.com", "project review"], "requires_confirmation": true, "complexity": "simple"}}

Query: "Send an email to Sarah saying I'll review the document by Friday"
Output: {{"query_type": "action", "primary_intent": "email", "sources_needed": ["email"], "action_type": "send_email", "time_reference": "Friday", "entities": ["Sarah", "document review"], "requires_confirmation": true, "complexity": "simple"}}

Query: "Reply to the last email from John confirming the meeting"
Output: {{"query_type": "action", "primary_intent": "email", "sources_needed": ["email"], "action_type": "reply_email", "time_reference": null, "entities": ["John", "meeting confirmation"], "requires_confirmation": true, "complexity": "moderate"}}

Query: "Create a draft email to the team about the new project timeline"
Output: {{"query_type": "action", "primary_intent": "email", "sources_needed": ["email"], "action_type": "create_draft", "time_reference": null, "entities": ["team", "project timeline"], "requires_confirmation": false, "complexity": "simple"}}

Query: "Find emails about the budget report"
Output: {{"query_type": "read", "primary_intent": "email", "sources_needed": ["email"], "time_reference": null, "entities": ["budget report"], "action_type": null, "requires_confirmation": false, "complexity": "simple"}}

Query: "Set up a weekly standup with the engineering team starting next Monday"
Output: {{"query_type": "workflow", "primary_intent": "multi_source", "sources_needed": ["calendar", "files"], "action_type": "create_event", "time_reference": "next Monday, recurring weekly", "entities": ["engineering team", "standup"], "requires_confirmation": true, "complexity": "complex"}}

Query: "Share the Q4 report with marketing@company.com"
Output: {{"query_type": "action", "primary_intent": "files", "sources_needed": ["files"], "action_type": "share_file", "time_reference": null, "entities": ["Q4 report", "marketing@company.com"], "requires_confirmation": true, "complexity": "simple"}}

Query: "Delete my meeting with Sarah tomorrow"
Output: {{"query_type": "action", "primary_intent": "calendar", "sources_needed": ["calendar"], "action_type": "delete_event", "time_reference": "tomorrow", "entities": ["Sarah"], "requires_confirmation": true, "complexity": "simple"}}

Query: "What did the team discuss about hackon in slack?"
Output: {{"query_type": "read", "primary_intent": "slack", "sources_needed": ["slack"], "action_type": null, "time_reference": null, "entities": ["hackon"], "requires_confirmation": false, "complexity": "simple"}}

Query: "Search slack for deployment updates"
Output: {{"query_type": "read", "primary_intent": "slack", "sources_needed": ["slack"], "action_type": null, "time_reference": null, "entities": ["deployment updates"], "requires_confirmation": false, "complexity": "simple"}}

Query: "What messages are in the engineering channel?"
Output: {{"query_type": "read", "primary_intent": "slack", "sources_needed": ["slack"], "action_type": null, "time_reference": null, "entities": ["engineering channel"], "requires_confirmation": false, "complexity": "simple"}}

## Now classify this query:
Query: "{query}"

Respond with ONLY valid JSON, no explanation."""


# =============================================================================
# RELEVANCE SCORING PROMPTS (Per Agent)
# =============================================================================

CALENDAR_RELEVANCE_TEMPLATE = """You are the Calendar relevance evaluator for a personal data assistant.

{datetime_context}

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
3. **Date/Time Extraction**: Extract any date/time references from the query and convert to absolute dates
4. **Directness**: Can I directly answer this, or only provide supporting info?
5. **Retrieval Plan**: What specific data would I search for?

## Date Extraction Rules
- Convert relative dates (today, tomorrow, next week) to absolute ISO dates
- If a date range is mentioned (Dec 22-28), extract both start and end
- If no year specified, use current year or next occurrence
- If no date mentioned, set date_range to null

## Scoring Guidelines
- **0.9-1.0**: Query explicitly requests calendar/meeting data
- **0.7-0.8**: Query strongly implies need for schedule data
- **0.5-0.6**: Query might benefit from calendar data as supporting info
- **0.3-0.4**: Weak connection, unlikely to be helpful
- **0.0-0.2**: No relevance to calendar data

## Output Format (JSON only)
{{
    "reasoning": "Brief step-by-step analysis",
    "score": 0.85,
    "confidence": "high",
    "retrieval_plan": "What I would search for",
    "search_terms": ["term1", "term2"],
    "date_range": {{
        "start": "YYYY-MM-DDTHH:MM:SS",
        "end": "YYYY-MM-DDTHH:MM:SS",
        "type": "specific_date|specific_range|relative_day|relative_range|none"
    }}
}}

Note: date_range can be null if no time reference in query, or an object with start/end dates.

## Examples

Query: "What meetings do I have on December 22nd?"
Output: {{"reasoning": "User explicitly asks about calendar meetings for a specific date", "score": 0.95, "confidence": "high", "retrieval_plan": "Fetch all events for December 22", "search_terms": [], "date_range": {{"start": "2024-12-22T00:00:00", "end": "2024-12-23T00:00:00", "type": "specific_date"}}}}

Query: "Show my schedule from Dec 22 to 28"
Output: {{"reasoning": "User wants calendar schedule for a date range", "score": 0.95, "confidence": "high", "retrieval_plan": "Fetch all events between Dec 22-28", "search_terms": [], "date_range": {{"start": "2024-12-22T00:00:00", "end": "2024-12-29T00:00:00", "type": "specific_range"}}}}

Query: "Do I have any meetings with Sarah?"
Output: {{"reasoning": "User asks about meetings with a specific person, no date specified", "score": 0.85, "confidence": "high", "retrieval_plan": "Search for events with Sarah as attendee", "search_terms": ["Sarah"], "date_range": null}}

Respond with ONLY valid JSON."""


GMAIL_RELEVANCE_TEMPLATE = """You are the Gmail relevance evaluator for a personal data assistant.

{datetime_context}

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
3. **Date/Time Extraction**: Extract any date/time references from the query and convert to absolute dates
4. **Directness**: Can I directly answer this, or only provide supporting info?
5. **Retrieval Plan**: What specific data would I search for?

## Date Extraction Rules
- Convert relative dates (today, yesterday, last week) to absolute ISO dates
- If a date range is mentioned (Dec 22-28), extract both start and end
- If no year specified, use current year or previous occurrence for "last" references
- If no date mentioned, set date_range to null

## Scoring Guidelines
- **0.9-1.0**: Query explicitly requests email/message data
- **0.7-0.8**: Query strongly implies need for email data
- **0.5-0.6**: Query might benefit from email data as supporting info
- **0.3-0.4**: Weak connection, unlikely to be helpful
- **0.0-0.2**: No relevance to email data

## Output Format (JSON only)
{{
    "reasoning": "Brief step-by-step analysis",
    "score": 0.85,
    "confidence": "high",
    "retrieval_plan": "What I would search for",
    "search_terms": ["term1", "term2"],
    "date_range": {{
        "start": "YYYY-MM-DDTHH:MM:SS",
        "end": "YYYY-MM-DDTHH:MM:SS",
        "type": "specific_date|specific_range|relative_day|relative_range|none"
    }}
}}

Note: date_range can be null if no time reference in query, or an object with start/end dates.

## Examples

Query: "Find emails from December 22 to 28"
Output: {{"reasoning": "User explicitly asks for emails in a specific date range", "score": 0.95, "confidence": "high", "retrieval_plan": "Search emails between Dec 22-28", "search_terms": [], "date_range": {{"start": "2024-12-22T00:00:00", "end": "2024-12-29T00:00:00", "type": "specific_range"}}}}

Query: "Show me yesterday's emails from John"
Output: {{"reasoning": "User asks for emails from a specific person on a relative date", "score": 0.95, "confidence": "high", "retrieval_plan": "Search emails from John sent yesterday", "search_terms": ["John"], "date_range": {{"start": "2024-12-16T00:00:00", "end": "2024-12-17T00:00:00", "type": "relative_day"}}}}

Query: "Any emails about the budget report?"
Output: {{"reasoning": "User asks about emails on a topic, no date specified", "score": 0.90, "confidence": "high", "retrieval_plan": "Search for budget report in email subjects/body", "search_terms": ["budget", "report"], "date_range": null}}

Respond with ONLY valid JSON."""


DRIVE_RELEVANCE_TEMPLATE = """You are the Drive relevance evaluator for a personal data assistant.

{datetime_context}

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


SLACK_RELEVANCE_TEMPLATE = """You are the Slack relevance evaluator for a personal data assistant.

{datetime_context}

## Your Data Source: Slack
Slack contains:
- Team messages and discussions across channels
- Channel conversations (public and private)
- Direct messages and group chats
- Message threads and replies
- User mentions and reactions
- Shared files and links in messages
- Channel topics and purposes
- Team communication history

Best for: team discussions, channel searches, finding conversations, communication history

## User Query
"{query}"

## Evaluation Process (Think step-by-step)
1. **Query Analysis**: What is the user specifically asking for?
2. **Source Match**: Does my data source contain this type of information?
3. **Directness**: Can I directly answer this, or only provide supporting info?
4. **Retrieval Plan**: What specific data would I search for?

## Scoring Guidelines
- **0.9-1.0**: Query explicitly requests Slack messages, team discussions, or channel conversations
- **0.7-0.8**: Query strongly implies need for team communication data
- **0.5-0.6**: Query might benefit from Slack data as supporting info
- **0.3-0.4**: Weak connection, unlikely to be helpful
- **0.0-0.2**: No relevance to Slack/team communication data

## Keywords that boost score:
- "slack", "channel", "team", "discussed", "conversation", "message", "said", "mentioned"
- "what did [person] say", "team discussion", "channel #", "thread"

## Output Format (JSON only)
{{"reasoning": "Brief step-by-step analysis", "score": 0.85, "confidence": "high", "retrieval_plan": "What I would search for", "search_terms": ["term1", "term2"]}}

Respond with ONLY valid JSON."""


# =============================================================================
# QUERY CLARIFICATION PROMPT
# =============================================================================

CLARIFICATION_PROMPT = """You are a query analyzer for a personal data assistant that has access to:
- Google Calendar (meetings, events, schedules)
- Gmail (emails, messages)
- Google Drive (documents, files, spreadsheets)
- Slack (team messages, channel discussions)

{datetime_context}

## User Query
"{query}"

## Your Task
Analyze if this query is clear enough to search the data sources effectively.

## Analysis Rules
1. If the query is ambiguous or too vague, suggest clarifying questions
2. If the query mentions a topic but not which data source, ask where to look
3. If the query is clear and specific, indicate no clarification is needed
4. Consider whether the user might want data from multiple sources

## Response Format (JSON only)
{{
    "needs_clarification": true/false,
    "clarity_score": 0.0-1.0,
    "reason": "Why clarification is needed (or not)",
    "suggested_questions": ["Question 1?", "Question 2?"],
    "likely_sources": ["calendar", "gmail", "drive", "slack"],
    "refined_query": "A more specific version of the query if possible"
}}

## Examples

Query: "What's happening?"
Response: {{"needs_clarification": true, "clarity_score": 0.2, "reason": "Query is too vague - unclear what information the user seeks", "suggested_questions": ["Are you looking for upcoming calendar events?", "Would you like to see recent emails or messages?", "Are you interested in recent team discussions on Slack?"], "likely_sources": [], "refined_query": null}}

Query: "What meetings do I have tomorrow?"
Response: {{"needs_clarification": false, "clarity_score": 0.95, "reason": "Query clearly asks for calendar events for a specific time", "suggested_questions": [], "likely_sources": ["calendar"], "refined_query": "meetings tomorrow"}}

Query: "Find the budget document"
Response: {{"needs_clarification": false, "clarity_score": 0.8, "reason": "Query asks for a specific document", "suggested_questions": [], "likely_sources": ["drive"], "refined_query": "budget document"}}

Query: "What did John say?"
Response: {{"needs_clarification": true, "clarity_score": 0.4, "reason": "Query mentions a person but unclear context - could be email, Slack, or meeting notes", "suggested_questions": ["Are you looking for emails from John?", "Would you like to search Slack messages from John?", "Are you looking for meeting notes involving John?"], "likely_sources": ["gmail", "slack"], "refined_query": null}}

Respond with ONLY valid JSON."""


# =============================================================================
# RESPONSE SYNTHESIS PROMPT
# =============================================================================

RESPONSE_SYNTHESIS_TEMPLATE = """You are a helpful data synthesis assistant. Your job is to create clear, 
accurate responses using ONLY the provided data from the user's personal sources.

{datetime_context}

## CRITICAL RULES
1. **NEVER invent or assume information** not present in the provided data
2. **Always cite the source** (Calendar, Gmail, Drive, or Slack) for each piece of information
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

### Example 4: Slack Query
**Query**: "What did the team discuss about the deployment?"
**Data**: Slack: [{{"channel": "#engineering", "user": "alice", "text": "Deployment scheduled for Friday", "timestamp": "2024-01-14T10:30:00"}}, {{"channel": "#engineering", "user": "bob", "text": "We need to run the migrations first", "timestamp": "2024-01-14T10:32:00"}}]

**Response**:
I found **2 messages** in Slack about deployment:

**From #engineering:**
- **alice** (Jan 14, 10:30 AM): "Deployment scheduled for Friday"
- **bob** (Jan 14, 10:32 AM): "We need to run the migrations first"

The team discussed that a deployment is scheduled for Friday, with migrations needed first.

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
    """Get the appropriate relevance prompt for an agent with datetime context.
    
    Args:
        agent_name: Name of the agent (calendar, gmail, drive, slack)
        query: User query
        
    Returns:
        Formatted prompt string with current datetime awareness
    """
    templates = {
        "calendar": CALENDAR_RELEVANCE_TEMPLATE,
        "gmail": GMAIL_RELEVANCE_TEMPLATE,
        "drive": DRIVE_RELEVANCE_TEMPLATE,
        "slack": SLACK_RELEVANCE_TEMPLATE,
    }
    
    datetime_context = get_current_datetime_context()
    
    template = templates.get(agent_name.lower())
    if template:
        return template.format(query=query, datetime_context=datetime_context)
    
    # Fallback for unknown agents
    return f"""{datetime_context}

Evaluate if the query "{query}" is relevant to the {agent_name} data source.
Return JSON: {{"reasoning": "analysis", "score": 0.5, "confidence": "medium", "retrieval_plan": "plan", "search_terms": []}}"""


def get_clarification_prompt(query: str) -> str:
    """Get the query clarification prompt with datetime context.
    
    Args:
        query: User query to analyze
        
    Returns:
        Formatted clarification prompt with current datetime awareness
    """
    return CLARIFICATION_PROMPT.format(
        query=query,
        datetime_context=get_current_datetime_context()
    )


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
    """Get the formatted synthesis prompt with datetime context.
    
    Args:
        query: User query
        agent_results: Results from triggered agents
        
    Returns:
        Formatted synthesis prompt with current datetime awareness
    """
    data_str = format_data_for_synthesis(agent_results)
    return RESPONSE_SYNTHESIS_TEMPLATE.format(
        query=query,
        data=data_str,
        datetime_context=get_current_datetime_context()
    )


def get_intent_classification_prompt(query: str) -> str:
    """Get the intent classification prompt with current datetime context.
    
    Args:
        query: User query
        
    Returns:
        Formatted intent classification prompt with datetime awareness
    """
    return INTENT_CLASSIFICATION_TEMPLATE.format(
        query=query,
        datetime_context=get_current_datetime_context()
    )


# =============================================================================
# ACTION PLANNING PROMPT
# =============================================================================

ACTION_PLANNING_TEMPLATE = """You are an action planner for a personal productivity assistant.
Your job is to create a detailed, executable plan for the user's requested action.

{datetime_context}

## User Query
"{query}"

## Intent Classification
{intent}

## Your Task
Create a detailed action plan with:
1. Clear summary of what will happen
2. Extracted parameters from the query
3. Any missing required parameters
4. A human-readable preview for confirmation

## Parameter Extraction Rules
- For meetings: Extract title, time, duration, attendees, location
- For emails: Extract recipients, subject, body content
- For files: Extract file name, content type, sharing permissions
- Parse relative times (tomorrow, next Monday, 2pm) into specific references
- Extract email addresses and names of people mentioned

## Required Parameters by Action Type
- create_event: title, start_time (required); attendees, duration, location (optional)
- send_email: to, subject, body (required); cc (optional)
- create_draft: to, subject, body (all optional, can be empty)
- reply_email: original_message_context, body (required)
- share_file: file_identifier, email, permission_level (required)
- create_document: title (required); content (optional)

## Output Format (JSON)
{{
    "summary": "Brief description of the action",
    "action_type": "create_event|send_email|etc",
    "parameters": {{
        "title": "Meeting Title",
        "start_time": "2024-01-15T14:00:00",
        "attendees": ["email@example.com"],
        ...
    }},
    "missing_parameters": ["list", "of", "missing", "required", "params"],
    "clarification_needed": "Question to ask user if params missing, or null",
    "preview": "Human-readable preview of the action for confirmation",
    "risk_level": "low|medium|high",
    "requires_confirmation": true
}}

## Examples

Query: "Schedule a meeting with john@company.com tomorrow at 2pm about project review"
Output:
{{
    "summary": "Create a calendar event for project review meeting",
    "action_type": "create_event",
    "parameters": {{
        "title": "Project Review",
        "start_time": "tomorrow at 2:00 PM",
        "duration_minutes": 60,
        "attendees": ["john@company.com"],
        "description": "Project review meeting"
    }},
    "missing_parameters": [],
    "clarification_needed": null,
    "preview": "📅 **Project Review**\\n🕐 Tomorrow at 2:00 PM (1 hour)\\n👥 Attendees: john@company.com",
    "risk_level": "medium",
    "requires_confirmation": true
}}

Query: "Send email to Sarah about the deadline"
Output:
{{
    "summary": "Send an email to Sarah regarding a deadline",
    "action_type": "send_email",
    "parameters": {{
        "to": ["Sarah"],
        "subject": "Regarding the deadline",
        "body": ""
    }},
    "missing_parameters": ["to (need full email)", "body"],
    "clarification_needed": "What is Sarah's email address? What would you like to say about the deadline?",
    "preview": null,
    "risk_level": "high",
    "requires_confirmation": true
}}

Respond with ONLY valid JSON."""


ACTION_PREVIEW_TEMPLATE = """Generate a user-friendly preview for the following action plan.

## Action Type: {action_type}
## Parameters:
{parameters}

## Preview Format Guidelines
- Use emojis to make it scannable (📅 for calendar, 📧 for email, 📁 for files)
- Bold the key information
- Format times in human-readable format
- List attendees/recipients clearly
- Keep it concise but complete

Generate ONLY the preview text, no JSON or other formatting."""


def get_action_planning_prompt(query: str, intent: dict) -> str:
    """Get the action planning prompt with datetime context.
    
    Args:
        query: User query
        intent: Classified intent dictionary
        
    Returns:
        Formatted action planning prompt with current datetime awareness
    """
    return ACTION_PLANNING_TEMPLATE.format(
        query=query,
        intent=json.dumps(intent, indent=2),
        datetime_context=get_current_datetime_context()
    )


def get_action_preview_prompt(action_type: str, parameters: dict) -> str:
    """Get the action preview prompt.
    
    Args:
        action_type: Type of action
        parameters: Action parameters
        
    Returns:
        Formatted preview prompt
    """
    return ACTION_PREVIEW_TEMPLATE.format(
        action_type=action_type,
        parameters=json.dumps(parameters, indent=2)
    )


# =============================================================================
# ACTION CONFIRMATION PROMPT
# =============================================================================

ACTION_CONFIRMATION_TEMPLATE = """You are generating a confirmation message for an action.

## Action to Confirm
Type: {action_type}
Summary: {summary}

## Details
{details}

## Generate a confirmation message that:
1. Clearly states what will happen
2. Lists all important details
3. Asks for user confirmation
4. Provides options: [Confirm] [Edit] [Cancel]

Keep the message concise and friendly. Use markdown formatting.

Generate ONLY the confirmation message."""


def get_confirmation_prompt(action_type: str, summary: str, details: dict) -> str:
    """Get the confirmation message prompt.
    
    Args:
        action_type: Type of action
        summary: Action summary
        details: Action details
        
    Returns:
        Formatted confirmation prompt
    """
    return ACTION_CONFIRMATION_TEMPLATE.format(
        action_type=action_type,
        summary=summary,
        details=json.dumps(details, indent=2)
    )
