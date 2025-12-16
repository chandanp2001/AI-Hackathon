"""Google Calendar data connector agent.

This agent handles queries related to calendar events, meetings,
schedules, and time-based queries.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from models.agent_response import (
    AgentResult,
    AgentType,
    CalendarEvent,
    RelevanceScore,
)
from services.agents.base_agent import BaseDataAgent
from services.llm.openai_service import OpenAIService

logger = logging.getLogger(__name__)


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
        search_terms: Optional[list[str]] = None
    ) -> AgentResult:
        """Fetch calendar events based on the query.
        
        Args:
            query: User's natural language query
            credentials: Google OAuth credentials
            search_terms: Optional search terms from relevance evaluation
            
        Returns:
            AgentResult: Contains list of CalendarEvent objects
            
        Raises:
            HttpError: If Google Calendar API call fails
        """
        start_time = time.time()
        
        try:
            service = build("calendar", "v3", credentials=credentials)
            
            # Determine time range from query
            time_min, time_max = self._parse_time_range(query)
            
            # Build search query from terms
            search_query = " ".join(search_terms) if search_terms else None
            
            # Fetch events
            events_result = service.events().list(
                calendarId="primary",
                timeMin=time_min.isoformat() + "Z",
                timeMax=time_max.isoformat() + "Z",
                maxResults=self._max_results,
                singleEvents=True,
                orderBy="startTime",
                q=search_query
            ).execute()
            
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
            
    def _parse_time_range(self, query: str) -> tuple[datetime, datetime]:
        """Parse time range from query using keyword detection.
        
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
            
        # This week
        elif "this week" in query_lower or "week" in query_lower:
            # Start from today, go to end of week (Sunday)
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
            days_until_sunday = 6 - now.weekday()
            time_max = time_min + timedelta(days=days_until_sunday + 1)
            
        # Next week
        elif "next week" in query_lower:
            # Start from next Monday
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            time_min = (now + timedelta(days=days_until_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            time_max = time_min + timedelta(days=7)
            
        # This month
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

