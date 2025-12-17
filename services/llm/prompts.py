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
import time as time_module


# =============================================================================
# DATE/TIME CONTEXT HELPER
# =============================================================================

def get_current_datetime_context() -> str:
    """Generate current date/time context string for LLM prompts.
    
    Includes explicit timezone information to prevent UTC/local time confusion.
    
    Returns:
        str: Formatted string with current date, time, timezone, and temporal context
        
    Examples:
        >>> context = get_current_datetime_context()
        >>> "Current Date:" in context
        True
    """
    now = datetime.now()
    # Get timezone info
    local_tz = now.astimezone()
    tz_offset = local_tz.strftime('%z')  # e.g., "+0530"
    tz_offset_formatted = f"{tz_offset[:3]}:{tz_offset[3:]}"  # e.g., "+05:30"
    
    # Get timezone name (IST, PST, etc.)
    try:
        tz_name = time_module.tzname[time_module.daylight] if time_module.daylight else time_module.tzname[0]
    except Exception:
        tz_name = "Local"
    
    iso_datetime = local_tz.isoformat()
    
    return f"""
## Current Date and Time Context
- **Today's Date**: {now.strftime('%A, %B %d, %Y')}
- **Current Time**: {now.strftime('%I:%M %p')} ({tz_name}, UTC{tz_offset_formatted})
- **Day of Week**: {now.strftime('%A')}
- **Week Number**: {now.isocalendar()[1]} of {now.year}
- **ISO DateTime with Timezone**: {iso_datetime}
- **Timezone**: {tz_name} (UTC{tz_offset_formatted})
- **Current Year**: {now.year}

## CRITICAL: Timezone Handling for Actions
When outputting times for actions (scheduling meetings, etc.):
1. ALWAYS use ISO 8601 format with timezone offset: YYYY-MM-DDTHH:MM:SS{tz_offset}
2. Interpret user's times as LOCAL time ({tz_name}), NOT UTC
3. Example: If user says "2pm tomorrow", output: "{(now + timedelta(days=1)).strftime('%Y-%m-%d')}T14:00:00{tz_offset}"

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

{conversation_context}

## Available Data Sources
- **calendar**: Meetings, events, schedules, availability, appointments
- **email**: Emails, messages, threads, attachments, senders
- **files**: Documents, spreadsheets, presentations, PDFs, Drive files
- **slack**: Slack messages, team discussions, channel conversations, DMs
- **devrev**: Tickets, issues, bugs, tasks, work items, sprint/backlog items

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

## CRITICAL: Follow-up Response Detection
**If conversation history shows the assistant previously asked for specific information related to an ACTION (like scheduling a meeting or sending email), and the user's current query is a short response, it is VERY LIKELY the user is providing that information - NOT making a new query.**

Examples of follow-up responses that should CONTINUE the previous action:
- Previous: "Would you like to specify a location?" → User says: "in arena building" → This is providing the LOCATION for the meeting, NOT a Slack search
- Previous: "What time should I schedule this?" → User says: "3pm tomorrow" → This is the TIME for the action
- Previous: "What should I include in the email body?" → User says: "please review and approve" → This is the EMAIL CONTENT

When you detect a follow-up response to an action:
- Set query_type to the SAME action type as the previous conversation (e.g., "action")
- Set action_type to the SAME action (e.g., "create_event", "send_email")
- The entities should include both the new info AND reference to previous context

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

Query: "Show me all open tickets assigned to me"
Output: {{"query_type": "read", "primary_intent": "devrev", "sources_needed": ["devrev"], "action_type": null, "time_reference": null, "entities": ["open tickets", "assigned to me"], "requires_confirmation": false, "complexity": "simple"}}

Query: "What's the status of the login bug?"
Output: {{"query_type": "read", "primary_intent": "devrev", "sources_needed": ["devrev"], "action_type": null, "time_reference": null, "entities": ["login bug"], "requires_confirmation": false, "complexity": "simple"}}

Query: "Find issues related to payment processing"
Output: {{"query_type": "read", "primary_intent": "devrev", "sources_needed": ["devrev"], "action_type": null, "time_reference": null, "entities": ["payment processing"], "requires_confirmation": false, "complexity": "simple"}}

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


DEVREV_RELEVANCE_TEMPLATE = """You are the DevRev relevance evaluator for a personal data assistant.

{datetime_context}

## Your Data Source: DevRev
DevRev contains:
- Tickets and issues (bugs, feature requests, support tickets)
- Work items and tasks with status, priority, and assignments
- Sprint and backlog items
- Customer support conversations linked to tickets
- Issue descriptions, comments, and resolution details
- Assignees, reporters, and stakeholders
- Ticket stages (open, in_progress, resolved, closed)
- Part/component associations
- Tags and labels for categorization
- Timeline and activity history

Best for: ticket lookups, issue tracking, bug reports, support requests, work item status, sprint queries

## User Query
"{query}"

## Evaluation Process (Think step-by-step)
1. **Query Analysis**: What is the user specifically asking for?
2. **Source Match**: Does my data source contain this type of information?
3. **Directness**: Can I directly answer this, or only provide supporting info?
4. **Retrieval Plan**: What specific data would I search for?

## Scoring Guidelines
- **0.9-1.0**: Query explicitly requests tickets, issues, bugs, or DevRev data
- **0.7-0.8**: Query strongly implies need for issue tracking or work item data
- **0.5-0.6**: Query might benefit from ticket/issue data as supporting info
- **0.3-0.4**: Weak connection, unlikely to be helpful
- **0.0-0.2**: No relevance to ticket/issue tracking data

## Keywords that boost score:
- "ticket", "issue", "bug", "task", "work item", "devrev"
- "sprint", "backlog", "assigned", "priority", "status"
- "support request", "feature request", "reported", "resolved"
- "open tickets", "my issues", "pending bugs"

## Output Format (JSON only)
{{"reasoning": "Brief step-by-step analysis", "score": 0.85, "confidence": "high", "retrieval_plan": "What I would search for", "search_terms": ["term1", "term2"]}}

Respond with ONLY valid JSON."""


DEVREV_CLARIFICATION_TEMPLATE = """You are a DevRev query clarifier. Analyze the user's query and determine if clarification is needed.

{datetime_context}

## User Query
"{query}"

## DevRev Data Types
- **Work Items**: Tickets, issues, bugs, tasks (have ID like ISS-1234, TKT-567)
- **Conversations**: Customer support chats and discussions
- **Articles**: Knowledge base articles and documentation
- **Parts/Boards**: Product components and team boards
- **Accounts**: Customer accounts and organizations

## Clarification Rules
1. If query has a specific ticket ID (ISS-1234), NO clarification needed
2. If query mentions "my tickets" or "assigned to me", NO clarification needed
3. If query is vague like "show tickets" or "devrev", ASK for clarification
4. If query doesn't specify whose tickets, ASK
5. If query doesn't specify status filter for large queries, SUGGEST filters

## Output Format (JSON only)
{{
    "needs_clarification": true/false,
    "clarity_score": 0.0-1.0,
    "reason": "Why clarification is needed (or why not)",
    "questions": ["Question 1?", "Question 2?"],
    "suggested_refinements": ["Show my open tickets", "ISS-1234 status"]
}}

## Examples

Query: "tickets"
Output: {{"needs_clarification": true, "clarity_score": 0.2, "reason": "Query is too vague - no filters specified", "questions": ["Would you like to see tickets assigned to you?", "Should I filter by status (open, in progress, resolved)?", "Which part/board should I search in?"], "suggested_refinements": ["my open tickets", "tickets in [board-name]"]}}

Query: "ISS-1234 status"
Output: {{"needs_clarification": false, "clarity_score": 1.0, "reason": "Specific ticket ID provided", "questions": [], "suggested_refinements": []}}

Query: "open bugs assigned to chandan"
Output: {{"needs_clarification": false, "clarity_score": 0.9, "reason": "Query has assignee and status filter", "questions": [], "suggested_refinements": []}}

Query: "show all issues"
Output: {{"needs_clarification": true, "clarity_score": 0.4, "reason": "No assignee or status filter - may return too many results", "questions": ["Would you like to filter by assignee?", "Should I show only open issues?"], "suggested_refinements": ["my open issues", "issues assigned to [name]"]}}

Respond with ONLY valid JSON."""


def get_devrev_clarification_prompt(query: str) -> str:
    """Get the DevRev clarification prompt with datetime context.
    
    Args:
        query: User query to analyze
        
    Returns:
        Formatted clarification prompt
    """
    return DEVREV_CLARIFICATION_TEMPLATE.format(
        query=query,
        datetime_context=get_current_datetime_context()
    )


ALPHA_DOCS_RELEVANCE_TEMPLATE = """You are the Alpha Docs relevance evaluator for a personal data assistant.

{datetime_context}

## Your Data Source: Alpha Docs
Alpha Docs contains:
- Razorpay documentation and guides
- Payment integration documentation
- API references and specifications
- Technical documentation
- Product documentation

Best for: payment integration queries, Razorpay documentation, API references

## User Query
"{query}"

## Evaluation Process (Think step-by-step)
1. **Query Analysis**: What is the user specifically asking for?
2. **Source Match**: Does my data source contain this type of information?
3. **Directness**: Can I directly answer this, or only provide supporting info?
4. **Retrieval Plan**: What specific data would I search for?

## Scoring Guidelines
- **0.9-1.0**: Query explicitly requests Razorpay docs or payment integration info
- **0.7-0.8**: Query strongly implies need for payment/API documentation
- **0.5-0.6**: Query might benefit from documentation as supporting info
- **0.3-0.4**: Weak connection, unlikely to be helpful
- **0.0-0.2**: No relevance to documentation data

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
- Alpha Docs (Razorpay documentation, payment integration guides, API references)
- DevRev (tickets, issues, bugs, tasks, work items, customer conversations, knowledge base articles, parts/boards)

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
5. For DevRev queries, consider if user wants: tickets/issues, conversations, articles, or parts

## Response Format (JSON only)
{{
    "needs_clarification": true/false,
    "clarity_score": 0.0-1.0,
    "reason": "Why clarification is needed (or not)",
    "suggested_questions": ["Question 1?", "Question 2?"],
    "likely_sources": ["calendar", "gmail", "drive", "slack", "devrev"],
    "refined_query": "A more specific version of the query if possible"
}}

## Examples

Query: "What's happening?"
Response: {{"needs_clarification": true, "clarity_score": 0.2, "reason": "Query is too vague - unclear what information the user seeks", "suggested_questions": ["Are you looking for upcoming calendar events?", "Would you like to see recent emails or messages?", "Are you looking for DevRev tickets or issues?"], "likely_sources": [], "refined_query": null}}

Query: "What meetings do I have tomorrow?"
Response: {{"needs_clarification": false, "clarity_score": 0.95, "reason": "Query clearly asks for calendar events for a specific time", "suggested_questions": [], "likely_sources": ["calendar"], "refined_query": "meetings tomorrow"}}

Query: "Find the budget document"
Response: {{"needs_clarification": false, "clarity_score": 0.8, "reason": "Query asks for a specific document", "suggested_questions": [], "likely_sources": ["drive"], "refined_query": "budget document"}}

Query: "What did John say?"
Response: {{"needs_clarification": true, "clarity_score": 0.4, "reason": "Query mentions a person but unclear context - could be email, Slack, or meeting notes", "suggested_questions": ["Are you looking for emails from John?", "Would you like to search Slack messages from John?", "Are you looking for meeting notes involving John?"], "likely_sources": ["gmail", "slack"], "refined_query": null}}

Query: "Show me tickets"
Response: {{"needs_clarification": true, "clarity_score": 0.4, "reason": "Query asks for tickets but no specific filter", "suggested_questions": ["Would you like to see tickets assigned to you?", "Are you looking for tickets in a specific board/part?", "Would you like to filter by status (open, in progress, closed)?"], "likely_sources": ["devrev"], "refined_query": null}}

Query: "Give me tickets assigned to chandan poonacha"
Response: {{"needs_clarification": false, "clarity_score": 0.9, "reason": "Query clearly asks for DevRev tickets with specific assignee filter", "suggested_questions": [], "likely_sources": ["devrev"], "refined_query": "tickets assigned to chandan poonacha"}}

Query: "What's the status of ISS-1234?"
Response: {{"needs_clarification": false, "clarity_score": 0.95, "reason": "Query asks for a specific DevRev issue by ID", "suggested_questions": [], "likely_sources": ["devrev"], "refined_query": "ISS-1234 status"}}

Query: "payment issues"
Response: {{"needs_clarification": true, "clarity_score": 0.5, "reason": "Ambiguous - could refer to DevRev tickets about payments or documentation", "suggested_questions": ["Are you looking for DevRev tickets related to payment issues?", "Would you like to search for payment documentation in Alpha Docs?", "Are you looking for Slack discussions about payment problems?"], "likely_sources": ["devrev", "alpha_docs", "slack"], "refined_query": null}}

Respond with ONLY valid JSON."""


# =============================================================================
# RESPONSE SYNTHESIS PROMPT
# =============================================================================

RESPONSE_SYNTHESIS_TEMPLATE = """You are a helpful data synthesis assistant. Your job is to create clear, 
accurate responses using ONLY the provided data from the user's personal sources.

{datetime_context}

## CRITICAL RULES
1. **NEVER invent or assume information** not present in the provided data
2. **Always cite the source** (Calendar, Gmail, Drive, Slack, or DevRev) for each piece of information
3. **Format dates/times** in human-readable format (e.g., "Tuesday, Dec 17 at 2:00 PM")
4. **Be concise** but include all relevant details
5. If data is **insufficient or empty**, clearly state what's missing
6. Use **markdown formatting** for readability (bold, bullets, etc.)
7. **Include permalinks/links** when available so users can navigate directly to the source

## Query Type Detection & Response Formatting

Detect the query type and format your response accordingly:

### 1. SUMMARIZATION QUERIES
Keywords: "summarize", "summary", "updates", "what happened", "recap", "overview", "highlights"
Format: Use organized bullet points grouped by topic or time period
- Start with a brief executive summary (1-2 sentences)
- Group updates by topic/theme with clear headers
- Use bullet points for individual items
- End with key takeaways or action items if applicable

### 2. SEARCH QUERIES  
Keywords: "find", "search", "look for", "where is", "show me"
Format: List results grouped by relevance with snippets and links
- Show most relevant results first
- Include preview snippets for each result
- Provide direct links when available
- Show the source for each result

### 3. STATUS QUERIES
Keywords: "status of", "progress on", "how is", "update on", "state of"
Format: Timeline or progress format with current state highlighted
- Clearly state the current status upfront
- Show chronological progression if applicable
- Highlight any blockers or next steps
- Use status indicators (✅ ⏳ ❌) where appropriate

### 4. COMPARISON QUERIES
Keywords: "compare", "difference", "versus", "vs", "between"
Format: Side-by-side or tabular comparison
- Use a structured comparison format
- Highlight key differences
- Note similarities if relevant

### 5. COUNT/METRIC QUERIES
Keywords: "how many", "count", "total", "number of", "statistics"
Format: Numeric summaries with breakdowns
- Lead with the key number/metric
- Provide breakdown by category if applicable
- Include trends or comparisons if data allows

## Source-Specific Formatting Guidelines

### Calendar Data
- Format: Timeline view grouped by day
- Include: Time, title, attendees, location, meeting links
- Use: 📅 emoji for visual clarity

### Gmail Data
- Format: Thread-based grouping
- Include: Subject, sender, date, preview snippet
- Highlight: Unread status, attachments
- Use: 📧 emoji for visual clarity

### Drive Data
- Format: Group by file type
- Include: Name, type, last modified, owner
- Prioritize: Recently modified files
- Use: 📁 emoji for folders, 📄 for docs

### Slack Data
- Format: Channel-based grouping with thread context
- Include: Channel name, user, message, timestamp, permalink
- Show: Thread replies when relevant
- Use: 💬 emoji for visual clarity

### DevRev Data
- Format: Status-based grouping with priority highlighting
- Include: Ticket ID, title, status, assignee, priority
- Highlight: Open/blocked items prominently
- Use: 🎫 emoji for tickets
- For ticket lists: Use markdown tables with | ID | Title | Status | Priority | Assignee |
- For single tickets: Use detailed card format with all fields
- For status queries: Group by status with progress indicators (⏳ 🟢 🔴)
- For count queries: Show summary metrics with percentages
- Always include DevRev links when available

## Few-Shot Examples

### Example 1: Summarization Query (Slack)
**Query**: "Summarize the updates from the engineering channel"
**Data**: Slack: [{{"channel": "#engineering", "user": "alice", "text": "Deployed v2.3 to staging", "timestamp": "2024-01-14T10:30:00", "permalink": "https://slack.com/..."}}, {{"channel": "#engineering", "user": "bob", "text": "Fixed the login bug ISS-123", "timestamp": "2024-01-14T11:00:00"}}, {{"channel": "#engineering", "user": "carol", "text": "Sprint planning tomorrow at 10am", "timestamp": "2024-01-14T14:00:00"}}]

**Response**:
## 💬 Engineering Channel Summary

**Overview**: The team made deployment progress, fixed a critical bug, and has sprint planning scheduled.

### Key Updates:

**Deployments & Releases**
- ✅ **v2.3 deployed to staging** - alice (10:30 AM)

**Bug Fixes**
- ✅ **Login bug fixed** (ISS-123) - bob (11:00 AM)

**Upcoming**
- 📅 **Sprint planning** scheduled for tomorrow at 10am - carol

---

### Example 2: Search Query (Drive)
**Query**: "Find the Q4 budget documents"
**Data**: Drive: [{{"name": "Q4 Budget 2024.xlsx", "mime_type": "spreadsheet", "modified_time": "2024-01-10", "web_view_link": "https://drive.google.com/..."}}, {{"name": "Q4 Budget Presentation.pptx", "mime_type": "presentation", "modified_time": "2024-01-08", "web_view_link": "https://drive.google.com/..."}}]

**Response**:
I found **2 documents** related to Q4 budget:

📄 **Q4 Budget 2024.xlsx** (Spreadsheet)
- Last modified: Jan 10, 2024
- [Open in Drive](https://drive.google.com/...)

📊 **Q4 Budget Presentation.pptx** (Presentation)  
- Last modified: Jan 8, 2024
- [Open in Drive](https://drive.google.com/...)

---

### Example 3: Status Query (DevRev)
**Query**: "What's the status of the payment integration?"
**Data**: DevRev: [{{"ticket_id": "ISS-456", "title": "Payment Gateway Integration", "status": "in_progress", "assignee": "john", "priority": "high"}}, {{"ticket_id": "ISS-457", "title": "Payment API Documentation", "status": "completed", "assignee": "jane"}}]

**Response**:
## 🎫 Payment Integration Status

**Current State**: ⏳ In Progress

| Ticket | Title | Status | Assignee | Priority |
|--------|-------|--------|----------|----------|
| ISS-456 | Payment Gateway Integration | ⏳ In Progress | john | 🔴 High |
| ISS-457 | Payment API Documentation | ✅ Completed | jane | - |

**Summary**: The main integration work (ISS-456) is actively being worked on by john. Documentation is already complete.

---

## Now synthesize a response for:
**Query**: "{query}"
**Data**:
{data}

First, detect the query type, then format your response according to the guidelines above. Provide a helpful, well-formatted response:"""


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_relevance_prompt(agent_name: str, query: str) -> str:
    """Get the appropriate relevance prompt for an agent with datetime context.
    
    Args:
        agent_name: Name of the agent (calendar, gmail, drive, slack, alpha_docs)
        query: User query
        
    Returns:
        Formatted prompt string with current datetime awareness
    """
    templates = {
        "calendar": CALENDAR_RELEVANCE_TEMPLATE,
        "gmail": GMAIL_RELEVANCE_TEMPLATE,
        "drive": DRIVE_RELEVANCE_TEMPLATE,
        "slack": SLACK_RELEVANCE_TEMPLATE,
        "alpha_docs": ALPHA_DOCS_RELEVANCE_TEMPLATE,
        "devrev": DEVREV_RELEVANCE_TEMPLATE,
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


def get_intent_classification_prompt(
    query: str,
    conversation_history: Optional[list[dict[str, str]]] = None
) -> str:
    """Get the intent classification prompt with current datetime context.
    
    Args:
        query: User query
        conversation_history: Optional conversation history to detect follow-ups
        
    Returns:
        Formatted intent classification prompt with datetime awareness
    """
    # Build conversation context if available
    conversation_context = ""
    if conversation_history and len(conversation_history) > 0:
        # Get last 6 messages for context
        recent_history = conversation_history[-6:]
        history_text = "\n".join([
            f"{msg['role'].capitalize()}: {msg['content']}"
            for msg in recent_history
        ])
        conversation_context = f"""## Conversation History
The user has been having a conversation. Review this to detect if the current query is a follow-up response:

{history_text}

**IMPORTANT**: If the last assistant message asked for specific information (location, time, email body, etc.) and the user's query looks like a short answer, classify this as CONTINUING THE PREVIOUS ACTION, not as a new query."""
    
    return INTENT_CLASSIFICATION_TEMPLATE.format(
        query=query,
        datetime_context=get_current_datetime_context(),
        conversation_context=conversation_context
    )


# =============================================================================
# ACTION PLANNING PROMPT
# =============================================================================

ACTION_PLANNING_TEMPLATE = """You are an action planner for a personal productivity assistant.
Your job is to create a detailed, executable plan for the user's requested action.

{datetime_context}

{conversation_context}

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
- **CRITICAL**: If conversation history shows you asked for specific information (like location, time, etc.) and the user's current message is a short answer, treat it as the answer to your question
- Look for partial information in follow-up messages that completes previous requests
- Parse relative times (tomorrow, next Monday, 2pm) into specific datetime references
- Extract email addresses and names of people mentioned
- Use the current datetime to interpret relative dates correctly

## CRITICAL: NO HALLUCINATION RULE
**You must NEVER invent, assume, or fabricate information the user did not provide.**

- ONLY include fields the user EXPLICITLY mentioned
- If user did NOT mention location: set "location" to null (NOT an empty string, NOT invented)
- If user did NOT mention description/agenda: set "description" to null
- If user did NOT mention duration: set "duration_minutes" to null (system will default to 60)
- NEVER generate creative content for optional fields
- The word "about X" in "meeting about X" means X is the TITLE, not the description

Examples of what NOT to do:
- User says "meeting with John" → DON'T invent location like "Arena Building"
- User says "meeting about project review" → DON'T create description like "Discussion on project review and updates"
- User just says "schedule meeting" → DON'T assume any details

## Meeting Requirements (create_event)
When scheduling a meeting:
1. **Title** (required): Extract from "about X" or "for X" phrases
2. **Start Time** (required): When should it happen? Use current datetime context to calculate exact time.
3. **Duration** (optional): ONLY if user specifies "for 30 min" or similar. Otherwise set to null.
4. **Attendees** (optional): ONLY if user mentions names/emails
5. **Location** (optional): ONLY if user explicitly says "at [place]" or "in [room]"
6. **Description** (optional): ONLY if user explicitly provides agenda or notes

**Google Meet will be auto-added - you don't need to ask about video conferencing.**

If you're missing REQUIRED info (title or time), ask for clarification. But DON'T ask about:
- Video conferencing / Google Meet (auto-added)
- Duration if not specified (defaults to 1 hour)
- Location or description (user can add these in the UI)

## Email Requirements (send_email)
1. **To** (required): Who to send to?
2. **Subject** (required): What's the subject?
3. **Body** (required): What's the message content?

## Required Parameters by Action Type
- create_event: title, start_time (required); attendees, duration_minutes, location, description (optional)
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
        "start_time": "2024-01-15T14:00:00+05:30",
        "attendees": ["email@example.com"],
        ...
    }},
    "missing_parameters": ["list", "of", "missing", "required", "params"],
    "clarification_needed": "Question to ask user if params missing, or null",
    "preview": "Human-readable preview of the action for confirmation",
    "risk_level": "low|medium|high",
    "requires_confirmation": true
}}

## CRITICAL: Time Format for start_time
- MUST use ISO 8601 format WITH timezone offset from the datetime context
- Use the timezone from "Current Date and Time Context" section above
- Example: "2024-12-18T14:00:00+05:30" (for IST timezone)
- NEVER output just "tomorrow at 2pm" - always convert to full ISO format

## Examples

Query: "Schedule a meeting with john@company.com tomorrow at 2pm about project review"
(Assuming current timezone is IST +05:30 and today is 2024-12-17)
Output:
{{
    "summary": "Create a calendar event for project review meeting",
    "action_type": "create_event",
    "parameters": {{
        "title": "Project Review",
        "start_time": "2024-12-18T14:00:00+05:30",
        "duration_minutes": null,
        "attendees": ["john@company.com"],
        "location": null,
        "description": null
    }},
    "missing_parameters": [],
    "clarification_needed": null,
    "preview": "📅 **Project Review**\\n🕐 Tomorrow (Dec 18) at 2:00 PM IST\\n👥 Attendees: john@company.com",
    "risk_level": "medium",
    "requires_confirmation": true
}}

Query: "Schedule meeting with sarah@company.com at 3pm in Conference Room A for 30 minutes"
Output:
{{
    "summary": "Create a calendar event in Conference Room A",
    "action_type": "create_event",
    "parameters": {{
        "title": "Meeting with Sarah",
        "start_time": "2024-12-17T15:00:00+05:30",
        "duration_minutes": 30,
        "attendees": ["sarah@company.com"],
        "location": "Conference Room A",
        "description": null
    }},
    "missing_parameters": [],
    "clarification_needed": null,
    "preview": "📅 **Meeting with Sarah**\\n🕐 Today at 3:00 PM IST (30 min)\\n📍 Conference Room A\\n👥 Attendees: sarah@company.com",
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


def get_action_planning_prompt(
    query: str, 
    intent: dict,
    conversation_history: Optional[list[dict[str, str]]] = None
) -> str:
    """Get the action planning prompt with datetime context.
    
    Args:
        query: User query
        intent: Classified intent dictionary
        conversation_history: Optional conversation history for context
        
    Returns:
        Formatted action planning prompt with current datetime awareness
    """
    # Build conversation context if available
    conversation_context = ""
    if conversation_history and len(conversation_history) > 0:
        # Get last 6 messages for context
        recent_history = conversation_history[-6:]
        history_text = "\n".join([
            f"{msg['role'].capitalize()}: {msg['content']}"
            for msg in recent_history
        ])
        conversation_context = f"""## Previous Conversation Context
The user has been having a conversation with you. Here's the recent context:

{history_text}

**IMPORTANT**: Use this conversation history to understand:
1. What action the user was trying to perform
2. Any details they provided in previous messages
3. Questions you asked that they're now answering
4. The full context of their request

When extracting parameters, look for information in BOTH the current query AND the conversation history.

**CRITICAL INSTRUCTION FOR FOLLOW-UP RESPONSES**:
If you previously asked the user for specific information (like "What location?" or "What time?") and their current message is a short response, it is HIGHLY LIKELY they are answering your question. For example:
- If you asked "Would you like to specify a location?" and they say "in arena building", that's the location
- If you asked "What time?" and they say "3pm tomorrow", that's the time
- If you asked "Who should I send this to?" and they say "john@example.com", that's the recipient

DO NOT treat these follow-up answers as new queries. Instead, combine them with the original action context."""
    
    return ACTION_PLANNING_TEMPLATE.format(
        query=query,
        intent=json.dumps(intent, indent=2),
        datetime_context=get_current_datetime_context(),
        conversation_context=conversation_context
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
