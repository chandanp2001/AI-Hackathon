"""Google Calendar data connector agent with action capabilities.

This agent handles:
- Reading: queries related to calendar events, meetings, schedules
- Actions: creating, updating, and deleting calendar events
"""

import logging
import re
import time
import time as time_module
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from models.agent_response import (
    AgentResult,
    AgentType,
    CalendarEvent,
    RelevanceScore,
)
from models.action import (
    ActionResult,
    ActionStepResult,
    ActionType,
    CreateEventParams,
    UpdateEventParams,
)
from services.agents.base_agent import BaseDataAgent
from services.llm.openai_service import OpenAIService

logger = logging.getLogger(__name__)


def is_date_related_term(word: str) -> bool:
    """Check if a word is a date/time related term that should be filtered.
    
    Args:
        word: The word to check (should be lowercase)
        
    Returns:
        bool: True if the word is date-related and should be filtered
    """
    # Day names (full and abbreviated)
    day_names = {
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "mon", "tue", "wed", "thu", "fri", "sat", "sun"
    }
    
    # Month names (full and abbreviated)
    month_names = {
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"
    }
    
    if word in day_names or word in month_names:
        return True
    
    # Check for year patterns (4-digit numbers between 1900-2100)
    if re.match(r'^(19|20)\d{2}$', word):
        return True
    
    # Check for day numbers (1-31) including ordinals like 1st, 2nd, 3rd, 21st
    if re.match(r'^(\d{1,2})(st|nd|rd|th)?$', word):
        try:
            num = int(re.match(r'^(\d{1,2})', word).group(1))
            if 1 <= num <= 31:
                return True
        except (ValueError, AttributeError):
            pass
    
    # Check for time patterns (12:30, 2pm, 14:00, etc.)
    if re.match(r'^\d{1,2}(:\d{2})?(am|pm)?$', word):
        return True
    
    # Check for date ranges like "22-28", "1-15", "Dec-Jan"
    if re.match(r'^\d{1,2}-\d{1,2}$', word):
        return True
    
    # Check for date formats like "12/25", "25/12", "2025/12"
    if re.match(r'^\d{1,4}[/\-]\d{1,2}([/\-]\d{1,4})?$', word):
        return True
    
    return False


# Generic terms to filter from search queries (applicable to calendar queries)
CALENDAR_GENERIC_TERMS = {
    # Generic calendar/meeting words
    "meetings", "meeting", "events", "event", "schedule", 
    "scheduled", "calendar", "today", "tomorrow", "week",
    "today's", "tomorrow's", "entries", "appointments", "appointment",
    "weekly", "daily", "monthly", "current", "this", "next",
    "upcoming", "my", "all", "show", "list", "get", "find",
    "for", "the", "in", "on", "at", "with", "have", "i",
    "view", "what", "do", "are", "is", "a", "an", "date",
    "time", "times", "day", "days", "month", "year", "years",
    "morning", "afternoon", "evening", "night", "noon", "midnight",
    "am", "pm", "hour", "hours", "minute", "minutes",
    "yesterday", "ago", "later", "from", "to", "until", "by",
    "starting", "ending", "begins", "ends", "last", "first",
    # Common prepositions and conjunctions
    "between", "and", "or", "during", "within", "before", "after",
    "of", "that", "which", "when", "where", "how", "why",
    "tell", "me", "can", "you", "please", "give", "show",
}


class CalendarAgent(BaseDataAgent):
    """Google Calendar data connector agent.
    
    Handles queries about:
    - Meetings and events
    - Schedules and availability
    - Time-based queries (today, tomorrow, this week)
    - Attendees and organizers
    - Event locations and meeting links
    
    Args:
        llm_service: Shared LLM service for OpenAI operations
        
    Examples:
        >>> agent = CalendarAgent()
        >>> score = await agent.evaluate_relevance("What meetings do I have tomorrow?")
        >>> if score.score >= 0.5:
        ...     result = await agent.fetch_data(query, credentials)
    """
    
    def __init__(self, llm_service: Optional[OpenAIService] = None):
        super().__init__(llm_service)
        self._max_results = 50
        self._default_days_ahead = 7
        
    @property
    def agent_name(self) -> str:
        """Return the agent identifier."""
        return "calendar"
    
    @property
    def agent_type(self) -> AgentType:
        """Return the agent type."""
        return AgentType.CALENDAR
    
    @property
    def data_source_description(self) -> str:
        """Return description of the Calendar data source."""
        return """Google Calendar - Contains:
- Scheduled meetings and events
- Event titles, descriptions, and locations
- Start and end times for events
- Meeting attendees and organizers
- Video conferencing links (Google Meet, Zoom, etc.)
- Recurring event patterns
- Event status (confirmed, tentative, cancelled)
- All-day events and time-blocked events
- Calendar availability information"""

    async def fetch_data(
        self,
        query: str,
        credentials: Credentials,
        search_terms: Optional[list[str]] = None,
        date_range: Optional[dict[str, Any]] = None
    ) -> AgentResult:
        """Fetch calendar events based on the query.
        
        Args:
            query: User's natural language query
            credentials: Google OAuth credentials
            search_terms: Optional search terms from relevance evaluation
            date_range: Optional LLM-extracted date range with start/end ISO strings
            
        Returns:
            AgentResult: Contains list of CalendarEvent objects
            
        Raises:
            HttpError: If Google Calendar API call fails
        """
        start_time = time.time()
        
        try:
            service = build("calendar", "v3", credentials=credentials)
            
            # Determine time range - prefer LLM-extracted range, fallback to keyword parsing
            time_min, time_max = self._parse_time_range_with_llm(query, date_range)
            
            # Build search query from terms - but only use specific entity names
            # Don't use generic terms like "meetings" "events" "schedule" as they're too broad
            # and won't match actual event titles
            search_query = None
            if search_terms:
                # Split each term into words and filter
                all_words = []
                for term in search_terms:
                    # Normalize: remove possessive 's and split
                    normalized = term.lower().replace("'s", "").replace("'", "")
                    words = normalized.split()
                    all_words.extend(words)
                
                # Only keep specific words (names, topics, etc.)
                # Filter out: generic calendar terms, date-related terms, and short words
                specific_words = [
                    word for word in all_words 
                    if word not in CALENDAR_GENERIC_TERMS 
                    and not is_date_related_term(word)
                    and len(word) > 2
                ]
                
                # Deduplicate while preserving order
                seen = set()
                unique_words = []
                for word in specific_words:
                    if word not in seen:
                        seen.add(word)
                        unique_words.append(word)
                        
                if unique_words:
                    search_query = " ".join(unique_words)
                    
            logger.debug(f"Calendar search - time range: {time_min} to {time_max}, search_query: {search_query}")
            
            # Fetch events - use time range for filtering, only add q if specific terms
            list_params = {
                "calendarId": "primary",
                "timeMin": time_min.isoformat() + "Z",
                "timeMax": time_max.isoformat() + "Z",
                "maxResults": self._max_results,
                "singleEvents": True,
                "orderBy": "startTime",
            }
            if search_query:
                list_params["q"] = search_query
                
            events_result = service.events().list(**list_params).execute()
            
            events = events_result.get("items", [])
            
            # Convert to structured format
            calendar_events = [
                self._parse_event(event) for event in events
            ]
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(
                f"Calendar agent fetched {len(calendar_events)} events "
                f"(took {execution_time:.1f}ms)"
            )
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=[event.model_dump() for event in calendar_events],
                metadata={
                    "time_range": {
                        "start": time_min.isoformat(),
                        "end": time_max.isoformat()
                    },
                    "total_events": len(calendar_events),
                    "search_query": search_query
                },
                execution_time_ms=execution_time,
                query_used=search_query
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Calendar API error: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Calendar API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error in Calendar agent: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    def _parse_time_range_with_llm(
        self, 
        query: str, 
        date_range: Optional[dict[str, Any]] = None
    ) -> tuple[datetime, datetime]:
        """Parse time range using LLM-extracted dates with keyword fallback.
        
        This method first tries to use the LLM-extracted date range (which can
        handle complex date expressions like "December 22-28", "next Friday",
        "Jan 15 2025"), and falls back to keyword-based parsing if unavailable.
        
        Args:
            query: User query (used for fallback)
            date_range: Optional LLM-extracted date range with:
                - start: ISO datetime string
                - end: ISO datetime string
                - type: Date reference type
                
        Returns:
            tuple: (time_min, time_max) datetime objects
        """
        # Try LLM-extracted date range first
        if date_range and date_range.get("start") and date_range.get("end"):
            try:
                start_str = date_range["start"]
                end_str = date_range["end"]
                
                # Parse ISO format dates
                time_min = datetime.fromisoformat(start_str.replace("Z", "+00:00").replace("+00:00", ""))
                time_max = datetime.fromisoformat(end_str.replace("Z", "+00:00").replace("+00:00", ""))
                
                logger.info(
                    f"Using LLM-extracted date range: {time_min.isoformat()} to {time_max.isoformat()} "
                    f"(type: {date_range.get('type', 'unknown')})"
                )
                return time_min, time_max
                
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse LLM date range: {e}, falling back to keyword parsing")
        
        # Fallback to keyword-based parsing
        return self._parse_time_range(query)
    
    def _parse_time_range(self, query: str) -> tuple[datetime, datetime]:
        """Parse time range from query using keyword detection (fallback method).
        
        Note: More specific phrases must be checked BEFORE generic ones.
        E.g., "next week" must be checked before "week".
        
        Args:
            query: User query to parse
            
        Returns:
            tuple: (time_min, time_max) datetime objects
        """
        now = datetime.utcnow()
        query_lower = query.lower()
        
        # Today
        if "today" in query_lower:
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
            time_max = time_min + timedelta(days=1)
            
        # Tomorrow
        elif "tomorrow" in query_lower:
            time_min = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            time_max = time_min + timedelta(days=1)
            
        # Next week (MUST come before "this week" / "week" check!)
        elif "next week" in query_lower:
            # Start from next Monday
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            time_min = (now + timedelta(days=days_until_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            time_max = time_min + timedelta(days=7)
            
        # This week (generic "week" also falls here)
        elif "this week" in query_lower or "week" in query_lower:
            # Start from today, go to end of week (Sunday)
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
            days_until_sunday = 6 - now.weekday()
            time_max = time_min + timedelta(days=days_until_sunday + 1)
            
        # Next month (MUST come before "this month" / "month" check!)
        elif "next month" in query_lower:
            # Start from 1st of next month
            if now.month == 12:
                time_min = now.replace(
                    year=now.year + 1, month=1, day=1,
                    hour=0, minute=0, second=0, microsecond=0
                )
                time_max = now.replace(
                    year=now.year + 1, month=2, day=1,
                    hour=0, minute=0, second=0, microsecond=0
                )
            else:
                time_min = now.replace(
                    month=now.month + 1, day=1,
                    hour=0, minute=0, second=0, microsecond=0
                )
                if now.month + 1 == 12:
                    time_max = now.replace(
                        year=now.year + 1, month=1, day=1,
                        hour=0, minute=0, second=0, microsecond=0
                    )
                else:
                    time_max = now.replace(
                        month=now.month + 2, day=1,
                        hour=0, minute=0, second=0, microsecond=0
                    )
            
        # This month (generic "month" also falls here)
        elif "this month" in query_lower or "month" in query_lower:
            time_min = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            # Go to next month
            if now.month == 12:
                time_max = now.replace(
                    year=now.year + 1, month=1, day=1,
                    hour=0, minute=0, second=0, microsecond=0
                )
            else:
                time_max = now.replace(
                    month=now.month + 1, day=1,
                    hour=0, minute=0, second=0, microsecond=0
                )
                
        # Default: next N days
        else:
            time_min = now
            time_max = now + timedelta(days=self._default_days_ahead)
            
        return time_min, time_max
        
    def _parse_event(self, event: dict[str, Any]) -> CalendarEvent:
        """Parse a Google Calendar event into structured format.
        
        Args:
            event: Raw event data from Google Calendar API
            
        Returns:
            CalendarEvent: Structured event object
        """
        # Handle all-day events vs timed events
        start = event.get("start", {})
        end = event.get("end", {})
        
        is_all_day = "date" in start
        
        if is_all_day:
            start_time = datetime.fromisoformat(start["date"])
            end_time = datetime.fromisoformat(end["date"])
        else:
            start_time = datetime.fromisoformat(
                start.get("dateTime", "").replace("Z", "+00:00")
            )
            end_time = datetime.fromisoformat(
                end.get("dateTime", "").replace("Z", "+00:00")
            )
            
        # Extract attendees
        attendees = [
            attendee.get("email", "")
            for attendee in event.get("attendees", [])
            if attendee.get("email")
        ]
        
        # Extract meeting link
        meeting_link = None
        if "hangoutLink" in event:
            meeting_link = event["hangoutLink"]
        elif "conferenceData" in event:
            entry_points = event["conferenceData"].get("entryPoints", [])
            for ep in entry_points:
                if ep.get("entryPointType") == "video":
                    meeting_link = ep.get("uri")
                    break
                    
        return CalendarEvent(
            event_id=event.get("id", ""),
            title=event.get("summary", "No Title"),
            start_time=start_time,
            end_time=end_time,
            location=event.get("location"),
            description=event.get("description"),
            attendees=attendees,
            organizer=event.get("organizer", {}).get("email"),
            meeting_link=meeting_link,
            status=event.get("status", "confirmed"),
            is_all_day=is_all_day
        )
        
    # =========================================================================
    # ACTION METHODS
    # =========================================================================
    
    async def create_event(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Create a new calendar event.
        
        Args:
            credentials: Google OAuth credentials
            params: Event parameters including:
                - title: Event title (required)
                - start_time: Start datetime or string (required)
                - end_time: End datetime (optional)
                - duration_minutes: Duration if no end_time (default: 60)
                - attendees: List of attendee emails
                - location: Event location
                - description: Event description
                - add_meet_link: Whether to add Google Meet
                - send_invites: Whether to send invite emails
                
        Returns:
            ActionStepResult: Result with event data or error
        """
        start_time = time.time()
        
        try:
            service = build("calendar", "v3", credentials=credentials)
            
            # Parse start time
            event_start = self._parse_datetime(params.get("start_time"))
            if not event_start:
                return ActionStepResult(
                    step_number=1,
                    success=False,
                    error_message="Invalid or missing start_time",
                    execution_time_ms=(time.time() - start_time) * 1000
                )
                
            # Calculate end time
            duration = params.get("duration_minutes", 60)
            event_end = params.get("end_time")
            if event_end:
                event_end = self._parse_datetime(event_end)
            if not event_end:
                event_end = event_start + timedelta(minutes=duration)
            
            # Determine timezone - use local timezone if datetime is naive
            # If datetime is timezone-aware, use its timezone
            local_tz = self._get_local_timezone()
            
            if event_start.tzinfo is not None:
                # Timezone-aware datetime - extract the timezone
                # For Google Calendar API, we need to provide the datetime in ISO format
                start_iso = event_start.isoformat()
                end_iso = event_end.isoformat()
            else:
                # Naive datetime - assume local time
                start_iso = event_start.isoformat()
                end_iso = event_end.isoformat()
            
            logger.info(f"Creating event: start={start_iso}, end={end_iso}, timezone={local_tz}")
                
            # Build event body - use local timezone for proper display
            event_body = {
                "summary": params.get("title", "New Event"),
                "start": {
                    "dateTime": start_iso,
                    "timeZone": local_tz,
                },
                "end": {
                    "dateTime": end_iso,
                    "timeZone": local_tz,
                },
            }
            
            # Add optional fields
            if params.get("location"):
                event_body["location"] = params["location"]
                
            if params.get("description"):
                event_body["description"] = params["description"]
                
            if params.get("attendees"):
                attendees = params["attendees"]
                if isinstance(attendees, str):
                    attendees = [attendees]
                event_body["attendees"] = [
                    {"email": email} for email in attendees
                ]
                
            # Add Google Meet if requested
            if params.get("add_meet_link", False):
                event_body["conferenceData"] = {
                    "createRequest": {
                        "requestId": f"meet-{int(time.time())}",
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                }
                
            # Determine if we should send invites
            send_updates = "all" if params.get("send_invites", True) else "none"
            
            # Create the event
            created_event = service.events().insert(
                calendarId="primary",
                body=event_body,
                conferenceDataVersion=1 if params.get("add_meet_link") else 0,
                sendUpdates=send_updates
            ).execute()
            
            execution_time = (time.time() - start_time) * 1000
            
            # Extract meeting link if available
            meeting_link = created_event.get("hangoutLink") or \
                created_event.get("conferenceData", {}).get("entryPoints", [{}])[0].get("uri")
            
            logger.info(f"Created calendar event: {created_event.get('id')}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "event_id": created_event.get("id"),
                    "html_link": created_event.get("htmlLink"),
                    "meeting_link": meeting_link,
                    "title": created_event.get("summary"),
                    "start": created_event.get("start"),
                    "end": created_event.get("end"),
                },
                can_rollback=True,
                rollback_info={"event_id": created_event.get("id")},
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Calendar API error creating event: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Calendar API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error creating event: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def update_event(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Update an existing calendar event.
        
        Args:
            credentials: Google OAuth credentials
            params: Update parameters including:
                - event_id: Event to update (required)
                - title: New title
                - start_time: New start time
                - end_time: New end time
                - location: New location
                - description: New description
                - attendees_to_add: Emails to add
                - attendees_to_remove: Emails to remove
                
        Returns:
            ActionStepResult: Result with updated event or error
        """
        start_time = time.time()
        
        try:
            service = build("calendar", "v3", credentials=credentials)
            
            event_id = params.get("event_id")
            if not event_id:
                return ActionStepResult(
                    step_number=1,
                    success=False,
                    error_message="Missing event_id",
                    execution_time_ms=(time.time() - start_time) * 1000
                )
                
            # Get current event
            current_event = service.events().get(
                calendarId="primary",
                eventId=event_id
            ).execute()
            
            # Store original for rollback
            original_event = dict(current_event)
            
            # Apply updates
            if params.get("title"):
                current_event["summary"] = params["title"]
                
            if params.get("start_time"):
                new_start = self._parse_datetime(params["start_time"])
                if new_start:
                    current_event["start"] = {
                        "dateTime": new_start.isoformat(),
                        "timeZone": "UTC",
                    }
                    
            if params.get("end_time"):
                new_end = self._parse_datetime(params["end_time"])
                if new_end:
                    current_event["end"] = {
                        "dateTime": new_end.isoformat(),
                        "timeZone": "UTC",
                    }
                    
            if params.get("location"):
                current_event["location"] = params["location"]
                
            if params.get("description"):
                current_event["description"] = params["description"]
                
            # Handle attendee changes
            current_attendees = {
                a.get("email"): a 
                for a in current_event.get("attendees", [])
            }
            
            for email in params.get("attendees_to_add", []):
                if email not in current_attendees:
                    current_attendees[email] = {"email": email}
                    
            for email in params.get("attendees_to_remove", []):
                current_attendees.pop(email, None)
                
            current_event["attendees"] = list(current_attendees.values())
            
            # Update the event
            updated_event = service.events().update(
                calendarId="primary",
                eventId=event_id,
                body=current_event,
                sendUpdates="all"
            ).execute()
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Updated calendar event: {event_id}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "event_id": updated_event.get("id"),
                    "html_link": updated_event.get("htmlLink"),
                    "title": updated_event.get("summary"),
                },
                can_rollback=True,
                rollback_info={"event_id": event_id, "original": original_event},
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Calendar API error updating event: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Calendar API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error updating event: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def delete_event(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Delete a calendar event.
        
        Args:
            credentials: Google OAuth credentials
            params: Parameters including:
                - event_id: Event to delete (required)
                - send_updates: Whether to notify attendees (default: True)
                
        Returns:
            ActionStepResult: Result indicating success or error
        """
        start_time = time.time()
        
        try:
            service = build("calendar", "v3", credentials=credentials)
            
            event_id = params.get("event_id")
            if not event_id:
                return ActionStepResult(
                    step_number=1,
                    success=False,
                    error_message="Missing event_id",
                    execution_time_ms=(time.time() - start_time) * 1000
                )
                
            # Get event before deletion for rollback info
            try:
                event_backup = service.events().get(
                    calendarId="primary",
                    eventId=event_id
                ).execute()
            except HttpError:
                event_backup = None
                
            # Delete the event
            send_updates = "all" if params.get("send_updates", True) else "none"
            
            service.events().delete(
                calendarId="primary",
                eventId=event_id,
                sendUpdates=send_updates
            ).execute()
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Deleted calendar event: {event_id}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={"deleted_event_id": event_id},
                can_rollback=event_backup is not None,
                rollback_info={"event_data": event_backup} if event_backup else None,
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Calendar API error deleting event: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Calendar API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error deleting event: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def check_availability(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Check availability for given attendees.
        
        Args:
            credentials: Google OAuth credentials
            params: Parameters including:
                - attendees: List of email addresses to check
                - start_time: Start of time range
                - end_time: End of time range
                - duration_minutes: Desired meeting duration
                
        Returns:
            ActionStepResult: Result with free/busy info
        """
        start_time_exec = time.time()
        
        try:
            service = build("calendar", "v3", credentials=credentials)
            
            attendees = params.get("attendees", [])
            if isinstance(attendees, str):
                attendees = [attendees]
                
            time_min = self._parse_datetime(params.get("start_time")) or datetime.utcnow()
            time_max = self._parse_datetime(params.get("end_time")) or \
                (time_min + timedelta(days=7))
                
            # Build freebusy query
            body = {
                "timeMin": time_min.isoformat() + "Z",
                "timeMax": time_max.isoformat() + "Z",
                "items": [{"id": email} for email in attendees] + [{"id": "primary"}]
            }
            
            freebusy = service.freebusy().query(body=body).execute()
            
            # Parse results
            availability = {}
            for calendar_id, calendar_data in freebusy.get("calendars", {}).items():
                busy_times = calendar_data.get("busy", [])
                availability[calendar_id] = {
                    "busy_periods": len(busy_times),
                    "busy_times": [
                        {"start": b["start"], "end": b["end"]}
                        for b in busy_times[:10]  # Limit for response size
                    ]
                }
                
            execution_time = (time.time() - start_time_exec) * 1000
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "time_range": {
                        "start": time_min.isoformat(),
                        "end": time_max.isoformat()
                    },
                    "availability": availability
                },
                can_rollback=False,
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time_exec) * 1000
            logger.error(f"Calendar API error checking availability: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Calendar API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time_exec) * 1000
            logger.error(f"Unexpected error checking availability: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    def _get_local_timezone(self) -> str:
        """Get the local timezone name for Google Calendar API.
        
        Returns:
            str: IANA timezone name (e.g., 'Asia/Kolkata')
        """
        try:
            # Try to get IANA timezone from system
            import subprocess
            result = subprocess.run(['date', '+%Z'], capture_output=True, text=True)
            tz_abbrev = result.stdout.strip()
            
            # Common mappings from abbreviation to IANA name
            tz_mapping = {
                'IST': 'Asia/Kolkata',
                'PST': 'America/Los_Angeles',
                'PDT': 'America/Los_Angeles',
                'EST': 'America/New_York',
                'EDT': 'America/New_York',
                'CST': 'America/Chicago',
                'CDT': 'America/Chicago',
                'MST': 'America/Denver',
                'MDT': 'America/Denver',
                'UTC': 'UTC',
                'GMT': 'UTC',
            }
            return tz_mapping.get(tz_abbrev, 'Asia/Kolkata')  # Default to IST
        except Exception:
            return 'Asia/Kolkata'  # Default to IST
    
    def _parse_datetime(self, dt_input: Any) -> Optional[datetime]:
        """Parse a datetime from various input formats.
        
        Uses LOCAL time, not UTC, to correctly interpret user's time intentions.
        
        Args:
            dt_input: Datetime, string, or None
            
        Returns:
            datetime or None (timezone-aware when possible)
        """
        if dt_input is None:
            return None
            
        if isinstance(dt_input, datetime):
            # If naive datetime, assume local time
            if dt_input.tzinfo is None:
                return dt_input
            return dt_input
            
        if isinstance(dt_input, str):
            # Try ISO format first (handles timezone-aware strings like "2024-12-18T14:00:00+05:30")
            try:
                parsed = datetime.fromisoformat(dt_input.replace("Z", "+00:00"))
                logger.info(f"Parsed ISO datetime: {parsed.isoformat()}")
                return parsed
            except ValueError:
                pass
                
            # Try common formats (interpreted as local time)
            formats = [
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M",
            ]
            for fmt in formats:
                try:
                    parsed = datetime.strptime(dt_input, fmt)
                    logger.info(f"Parsed datetime (local): {parsed.isoformat()}")
                    return parsed
                except ValueError:
                    continue
                    
            # Handle relative times (basic) - use LOCAL time, not UTC
            dt_lower = dt_input.lower()
            now = datetime.now()  # LOCAL time, not utcnow()
            
            if "tomorrow" in dt_lower:
                base = now + timedelta(days=1)
                base = base.replace(hour=9, minute=0, second=0, microsecond=0)
                
                # Look for time
                if "at" in dt_lower:
                    time_part = dt_lower.split("at")[-1].strip()
                    base = self._apply_time_to_date(base, time_part)
                
                logger.info(f"Parsed relative datetime (tomorrow): {base.isoformat()}")
                return base
                
            if "today" in dt_lower:
                base = now.replace(second=0, microsecond=0)
                if "at" in dt_lower:
                    time_part = dt_lower.split("at")[-1].strip()
                    base = self._apply_time_to_date(base, time_part)
                logger.info(f"Parsed relative datetime (today): {base.isoformat()}")
                return base
                
        return None
        
    def _apply_time_to_date(self, dt: datetime, time_str: str) -> datetime:
        """Apply a time string to a date.
        
        Args:
            dt: Base datetime
            time_str: Time string like "2pm", "14:00", "2:30 pm"
            
        Returns:
            datetime with applied time
        """
        time_str = time_str.strip().lower()
        
        # Parse "2pm", "2:30pm" style
        is_pm = "pm" in time_str
        is_am = "am" in time_str
        time_str = time_str.replace("pm", "").replace("am", "").strip()
        
        parts = time_str.replace(":", " ").split()
        
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            
            if is_pm and hour < 12:
                hour += 12
            elif is_am and hour == 12:
                hour = 0
                
            return dt.replace(hour=hour, minute=minute)
        except (ValueError, IndexError):
            return dt
            
    async def execute_action(
        self,
        action_type: ActionType,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Execute a calendar action by type.
        
        Args:
            action_type: Type of action to execute
            credentials: Google OAuth credentials
            params: Action parameters
            
        Returns:
            ActionStepResult: Result of the action
        """
        action_handlers = {
            ActionType.CREATE_EVENT: self.create_event,
            ActionType.UPDATE_EVENT: self.update_event,
            ActionType.DELETE_EVENT: self.delete_event,
            ActionType.CHECK_AVAILABILITY: self.check_availability,
        }
        
        handler = action_handlers.get(action_type)
        if not handler:
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unsupported action type: {action_type}",
                execution_time_ms=0.0
            )
            
        return await handler(credentials, params)

