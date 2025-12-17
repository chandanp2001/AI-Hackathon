"""Gmail data connector agent with action capabilities.

This agent handles:
- Reading: email search, thread lookup, message metadata
- Actions: sending emails, creating drafts, replying, forwarding
"""

import base64
import logging
import re
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from models.agent_response import (
    AgentResult,
    AgentType,
    EmailMessage,
)
from models.action import (
    ActionStepResult,
    ActionType,
    SendEmailParams,
    CreateDraftParams,
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


# Generic terms to filter from search queries (applicable to email queries)
GMAIL_GENERIC_TERMS = {
    # Generic email words
    "emails", "email", "messages", "message", "inbox", "mail",
    "recent", "latest", "new", "show", "find", "get", "my", "me",
    "the", "a", "an", "about", "with", "from", "to", "all",
    "sent", "received", "read", "unread", "important", "starred",
    "list", "view", "what", "are", "is", "have", "i", "for",
    # Time-related generic words
    "today", "tomorrow", "yesterday", "week", "month", "year",
    "this", "last", "next", "ago", "recent", "old", "older", "newer",
    "morning", "afternoon", "evening", "night", "time", "date",
    "day", "days", "hours", "hour", "minutes", "minute",
    "report", "reports",  # Often generic unless paired with specific term
}


class GmailAgent(BaseDataAgent):
    """Gmail data connector agent.
    
    Handles queries about:
    - Email search and retrieval
    - Email threads and conversations
    - Sender and recipient information
    - Email subjects and content
    - Labels and categories
    - Attachments
    
    Args:
        llm_service: Shared LLM service for OpenAI operations
        
    Examples:
        >>> agent = GmailAgent()
        >>> score = await agent.evaluate_relevance("Find emails from John about the project")
        >>> if score.score >= 0.5:
        ...     result = await agent.fetch_data(query, credentials)
    """
    
    def __init__(self, llm_service: Optional[OpenAIService] = None):
        super().__init__(llm_service)
        self._max_results = 25
        self._body_preview_length = 500
        
    @property
    def agent_name(self) -> str:
        """Return the agent identifier."""
        return "gmail"
    
    @property
    def agent_type(self) -> AgentType:
        """Return the agent type."""
        return AgentType.GMAIL
    
    @property
    def data_source_description(self) -> str:
        """Return description of the Gmail data source."""
        return """Gmail - Contains:
- Email messages (sent and received)
- Email subjects and body content
- Sender and recipient information
- Email threads and conversations
- Attachments and file references
- Email labels (inbox, sent, drafts, custom labels)
- Email dates and timestamps
- Read/unread status
- Starred and important emails
- Email search across all folders"""

    async def fetch_data(
        self,
        query: str,
        credentials: Credentials,
        search_terms: Optional[list[str]] = None,
        date_range: Optional[dict[str, Any]] = None
    ) -> AgentResult:
        """Fetch emails based on the query.
        
        Args:
            query: User's natural language query
            credentials: Google OAuth credentials
            search_terms: Optional search terms from relevance evaluation
            date_range: Optional LLM-extracted date range with start/end ISO strings
            
        Returns:
            AgentResult: Contains list of EmailMessage objects
            
        Raises:
            HttpError: If Gmail API call fails
        """
        start_time = time.time()
        
        try:
            service = build("gmail", "v1", credentials=credentials)
            
            # Build Gmail search query with optional date range
            gmail_query = self._build_gmail_query(query, search_terms, date_range)
            
            # Search for messages
            results = service.users().messages().list(
                userId="me",
                q=gmail_query,
                maxResults=self._max_results
            ).execute()
            
            messages = results.get("messages", [])
            
            # Fetch full message details
            email_messages = []
            for msg in messages:
                try:
                    full_msg = service.users().messages().get(
                        userId="me",
                        id=msg["id"],
                        format="full"
                    ).execute()
                    email_messages.append(self._parse_message(full_msg))
                except HttpError as e:
                    logger.warning(f"Failed to fetch message {msg['id']}: {e}")
                    continue
                    
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(
                f"Gmail agent fetched {len(email_messages)} emails "
                f"(took {execution_time:.1f}ms)"
            )
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=[email.model_dump() for email in email_messages],
                metadata={
                    "gmail_query": gmail_query,
                    "total_messages": len(email_messages),
                    "max_results": self._max_results
                },
                execution_time_ms=execution_time,
                query_used=gmail_query
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Gmail API error: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Gmail API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error in Gmail agent: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    def _build_gmail_query(
        self,
        query: str,
        search_terms: Optional[list[str]] = None,
        date_range: Optional[dict[str, Any]] = None
    ) -> str:
        """Build a Gmail search query from natural language.
        
        This method prioritizes LLM-extracted date ranges for precise filtering,
        and falls back to keyword-based date detection if unavailable.
        
        Args:
            query: Natural language query
            search_terms: Suggested search terms from LLM
            date_range: Optional LLM-extracted date range with:
                - start: ISO datetime string
                - end: ISO datetime string
                - type: Date reference type
            
        Returns:
            str: Gmail search query string
        """
        query_parts = []
        query_lower = query.lower()
        used_llm_dates = False
        
        # Add suggested search terms - but filter out generic and date-related ones
        if search_terms:
            # Split each term into words and filter
            all_words = []
            for term in search_terms:
                # Normalize: remove possessive 's and split
                normalized = term.lower().replace("'s", "").replace("'", "")
                words = normalized.split()
                all_words.extend(words)
            
            # Only keep specific words (names, topics, etc.)
            # Filter out: generic email terms, date-related terms, and short words
            specific_words = [
                word for word in all_words 
                if word not in GMAIL_GENERIC_TERMS 
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
                query_parts.append(" ".join(unique_words))
            
        # Parse common patterns for Gmail-specific filters
        # Unread emails
        if "unread" in query_lower:
            query_parts.append("is:unread")
            
        # Starred emails
        if "starred" in query_lower:
            query_parts.append("is:starred")
        
        # Important emails    
        if "important" in query_lower:
            query_parts.append("is:important")
            
        # With attachments
        if "attachment" in query_lower:
            query_parts.append("has:attachment")
        
        # Try LLM-extracted date range first (handles complex dates like "December 22-28")
        if date_range and date_range.get("start") and date_range.get("end"):
            try:
                start_str = date_range["start"]
                end_str = date_range["end"]
                
                # Parse ISO dates to Gmail format (YYYY/MM/DD)
                start_dt = datetime.fromisoformat(start_str.replace("Z", "").replace("+00:00", ""))
                end_dt = datetime.fromisoformat(end_str.replace("Z", "").replace("+00:00", ""))
                
                # Gmail uses after: and before: with YYYY/MM/DD format
                gmail_start = start_dt.strftime("%Y/%m/%d")
                gmail_end = end_dt.strftime("%Y/%m/%d")
                
                query_parts.append(f"after:{gmail_start}")
                query_parts.append(f"before:{gmail_end}")
                
                used_llm_dates = True
                logger.info(
                    f"Using LLM-extracted Gmail date range: after:{gmail_start} before:{gmail_end} "
                    f"(type: {date_range.get('type', 'unknown')})"
                )
                
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse LLM date range for Gmail: {e}")
        
        # Fallback: Time-based filters using keywords (only if LLM dates not used)
        if not used_llm_dates:
            if "today" in query_lower:
                query_parts.append("newer_than:1d")
            elif "yesterday" in query_lower:
                query_parts.append("newer_than:2d older_than:1d")
            elif "this week" in query_lower or "week" in query_lower:
                query_parts.append("newer_than:7d")
            elif "this month" in query_lower or "month" in query_lower:
                query_parts.append("newer_than:30d")
            elif "recent" in query_lower:
                # "Recent" emails - default to last 7 days
                query_parts.append("newer_than:7d")
                    
        logger.debug(f"Gmail query built: {query_parts}")
        return " ".join(query_parts) if query_parts else ""
        
    def _parse_message(self, message: dict[str, Any]) -> EmailMessage:
        """Parse a Gmail message into structured format.
        
        Args:
            message: Raw message data from Gmail API
            
        Returns:
            EmailMessage: Structured email object
        """
        headers = message.get("payload", {}).get("headers", [])
        
        # Extract headers
        header_dict = {h["name"].lower(): h["value"] for h in headers}
        
        subject = header_dict.get("subject", "No Subject")
        sender = header_dict.get("from", "Unknown")
        date_str = header_dict.get("date", "")
        
        # Parse recipients
        recipients = []
        if "to" in header_dict:
            recipients = [r.strip() for r in header_dict["to"].split(",")]
            
        # Parse date
        try:
            # Gmail date format varies, try common patterns
            internal_date = message.get("internalDate")
            if internal_date:
                date = datetime.fromtimestamp(int(internal_date) / 1000)
            else:
                date = datetime.utcnow()
        except (ValueError, TypeError):
            date = datetime.utcnow()
            
        # Get snippet (preview)
        snippet = message.get("snippet", "")
        
        # Get body preview
        body_preview = self._extract_body_preview(message)
        
        # Get labels
        labels = message.get("labelIds", [])
        
        # Check for attachments
        has_attachments = self._has_attachments(message.get("payload", {}))
        
        # Check read status
        is_unread = "UNREAD" in labels
        
        return EmailMessage(
            message_id=message.get("id", ""),
            thread_id=message.get("threadId", ""),
            subject=subject,
            sender=sender,
            recipients=recipients,
            date=date,
            snippet=snippet,
            body_preview=body_preview,
            labels=labels,
            has_attachments=has_attachments,
            is_unread=is_unread
        )
        
    def _extract_body_preview(self, message: dict[str, Any]) -> Optional[str]:
        """Extract a preview of the email body.
        
        Args:
            message: Raw message data
            
        Returns:
            str: Body preview text or None
        """
        payload = message.get("payload", {})
        
        # Try to get text/plain body
        body_data = None
        
        if "body" in payload and payload["body"].get("data"):
            body_data = payload["body"]["data"]
        elif "parts" in payload:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain":
                    body_data = part.get("body", {}).get("data")
                    break
                elif "parts" in part:
                    # Handle nested parts
                    for subpart in part["parts"]:
                        if subpart.get("mimeType") == "text/plain":
                            body_data = subpart.get("body", {}).get("data")
                            break
                            
        if body_data:
            try:
                decoded = base64.urlsafe_b64decode(body_data).decode("utf-8")
                return decoded[:self._body_preview_length]
            except Exception:
                pass
                
        return None
        
    def _has_attachments(self, payload: dict[str, Any]) -> bool:
        """Check if message has attachments.
        
        Args:
            payload: Message payload
            
        Returns:
            bool: True if message has attachments
        """
        if "parts" in payload:
            for part in payload["parts"]:
                if part.get("filename"):
                    return True
                if "parts" in part:
                    for subpart in part["parts"]:
                        if subpart.get("filename"):
                            return True
        return False
        
    # =========================================================================
    # ACTION METHODS
    # =========================================================================
    
    async def send_email(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Send an email.
        
        Args:
            credentials: Google OAuth credentials
            params: Email parameters including:
                - to: List of recipient emails (required)
                - subject: Email subject (required)
                - body: Email body (required)
                - cc: List of CC recipients
                - bcc: List of BCC recipients
                - is_html: Whether body is HTML
                - reply_to_message_id: For replies
                - thread_id: Thread to add to
                
        Returns:
            ActionStepResult: Result with sent message info or error
        """
        start_time = time.time()
        
        try:
            service = build("gmail", "v1", credentials=credentials)
            
            # Validate required params
            to_list = params.get("to", [])
            if isinstance(to_list, str):
                to_list = [to_list]
            if not to_list:
                return ActionStepResult(
                    step_number=1,
                    success=False,
                    error_message="Missing 'to' recipients",
                    execution_time_ms=(time.time() - start_time) * 1000
                )
                
            subject = params.get("subject", "")
            body = params.get("body", "")
            
            # Create message
            if params.get("is_html"):
                message = MIMEMultipart("alternative")
                message.attach(MIMEText(body, "html"))
            else:
                message = MIMEText(body)
                
            message["to"] = ", ".join(to_list)
            message["subject"] = subject
            
            if params.get("cc"):
                cc_list = params["cc"]
                if isinstance(cc_list, str):
                    cc_list = [cc_list]
                message["cc"] = ", ".join(cc_list)
                
            if params.get("bcc"):
                bcc_list = params["bcc"]
                if isinstance(bcc_list, str):
                    bcc_list = [bcc_list]
                message["bcc"] = ", ".join(bcc_list)
                
            # Encode message
            raw_message = base64.urlsafe_b64encode(
                message.as_bytes()
            ).decode("utf-8")
            
            body_payload = {"raw": raw_message}
            
            # Add thread ID if replying
            if params.get("thread_id"):
                body_payload["threadId"] = params["thread_id"]
                
            # Send the message
            sent_message = service.users().messages().send(
                userId="me",
                body=body_payload
            ).execute()
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Sent email: {sent_message.get('id')}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "message_id": sent_message.get("id"),
                    "thread_id": sent_message.get("threadId"),
                    "to": to_list,
                    "subject": subject,
                },
                can_rollback=False,  # Can't unsend
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Gmail API error sending email: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Gmail API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error sending email: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def create_draft(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Create an email draft.
        
        Args:
            credentials: Google OAuth credentials
            params: Draft parameters including:
                - to: List of recipient emails
                - subject: Email subject
                - body: Email body
                - cc: List of CC recipients
                - is_html: Whether body is HTML
                
        Returns:
            ActionStepResult: Result with draft info or error
        """
        start_time = time.time()
        
        try:
            service = build("gmail", "v1", credentials=credentials)
            
            # Build message
            to_list = params.get("to", [])
            if isinstance(to_list, str):
                to_list = [to_list]
                
            subject = params.get("subject", "")
            body = params.get("body", "")
            
            if params.get("is_html"):
                message = MIMEMultipart("alternative")
                message.attach(MIMEText(body, "html"))
            else:
                message = MIMEText(body)
                
            if to_list:
                message["to"] = ", ".join(to_list)
            message["subject"] = subject
            
            if params.get("cc"):
                cc_list = params["cc"]
                if isinstance(cc_list, str):
                    cc_list = [cc_list]
                message["cc"] = ", ".join(cc_list)
                
            # Encode message
            raw_message = base64.urlsafe_b64encode(
                message.as_bytes()
            ).decode("utf-8")
            
            # Create draft
            draft = service.users().drafts().create(
                userId="me",
                body={"message": {"raw": raw_message}}
            ).execute()
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Created draft: {draft.get('id')}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "draft_id": draft.get("id"),
                    "message_id": draft.get("message", {}).get("id"),
                    "to": to_list,
                    "subject": subject,
                },
                can_rollback=True,
                rollback_info={"draft_id": draft.get("id")},
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Gmail API error creating draft: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Gmail API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error creating draft: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def reply_email(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Reply to an existing email.
        
        Args:
            credentials: Google OAuth credentials
            params: Reply parameters including:
                - message_id: Original message ID to reply to (required)
                - body: Reply body (required)
                - reply_all: Whether to reply to all recipients
                
        Returns:
            ActionStepResult: Result with sent reply info or error
        """
        start_time = time.time()
        
        try:
            service = build("gmail", "v1", credentials=credentials)
            
            message_id = params.get("message_id")
            if not message_id:
                return ActionStepResult(
                    step_number=1,
                    success=False,
                    error_message="Missing message_id to reply to",
                    execution_time_ms=(time.time() - start_time) * 1000
                )
                
            # Get original message
            original = service.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID"]
            ).execute()
            
            headers = {
                h["name"].lower(): h["value"]
                for h in original.get("payload", {}).get("headers", [])
            }
            
            thread_id = original.get("threadId")
            
            # Determine recipients
            reply_to = headers.get("from", "")
            recipients = [reply_to]
            
            if params.get("reply_all"):
                # Add original To and Cc
                if headers.get("to"):
                    recipients.extend(
                        r.strip() for r in headers["to"].split(",")
                    )
                if headers.get("cc"):
                    recipients.extend(
                        r.strip() for r in headers["cc"].split(",")
                    )
                # Remove duplicates
                recipients = list(set(recipients))
                
            # Build reply subject
            original_subject = headers.get("subject", "")
            if not original_subject.lower().startswith("re:"):
                subject = f"Re: {original_subject}"
            else:
                subject = original_subject
                
            # Build reply message
            body = params.get("body", "")
            message = MIMEText(body)
            message["to"] = ", ".join(recipients)
            message["subject"] = subject
            
            # Add In-Reply-To header
            if headers.get("message-id"):
                message["In-Reply-To"] = headers["message-id"]
                message["References"] = headers["message-id"]
                
            # Encode and send
            raw_message = base64.urlsafe_b64encode(
                message.as_bytes()
            ).decode("utf-8")
            
            sent_message = service.users().messages().send(
                userId="me",
                body={
                    "raw": raw_message,
                    "threadId": thread_id
                }
            ).execute()
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Sent reply: {sent_message.get('id')}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "message_id": sent_message.get("id"),
                    "thread_id": sent_message.get("threadId"),
                    "to": recipients,
                    "subject": subject,
                    "in_reply_to": message_id,
                },
                can_rollback=False,
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Gmail API error replying to email: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Gmail API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error replying to email: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def forward_email(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Forward an email to new recipients.
        
        Args:
            credentials: Google OAuth credentials
            params: Forward parameters including:
                - message_id: Message to forward (required)
                - to: Recipients to forward to (required)
                - additional_message: Message to add above forwarded content
                
        Returns:
            ActionStepResult: Result with forwarded message info or error
        """
        start_time = time.time()
        
        try:
            service = build("gmail", "v1", credentials=credentials)
            
            message_id = params.get("message_id")
            to_list = params.get("to", [])
            if isinstance(to_list, str):
                to_list = [to_list]
                
            if not message_id or not to_list:
                return ActionStepResult(
                    step_number=1,
                    success=False,
                    error_message="Missing message_id or recipients",
                    execution_time_ms=(time.time() - start_time) * 1000
                )
                
            # Get original message
            original = service.users().messages().get(
                userId="me",
                id=message_id,
                format="full"
            ).execute()
            
            headers = {
                h["name"].lower(): h["value"]
                for h in original.get("payload", {}).get("headers", [])
            }
            
            # Get original subject
            original_subject = headers.get("subject", "")
            if not original_subject.lower().startswith("fwd:"):
                subject = f"Fwd: {original_subject}"
            else:
                subject = original_subject
                
            # Build forwarded content
            original_body = self._extract_body_preview(original) or ""
            additional = params.get("additional_message", "")
            
            forward_body = f"{additional}\n\n---------- Forwarded message ----------\n"
            forward_body += f"From: {headers.get('from', 'Unknown')}\n"
            forward_body += f"Date: {headers.get('date', 'Unknown')}\n"
            forward_body += f"Subject: {original_subject}\n"
            forward_body += f"To: {headers.get('to', 'Unknown')}\n\n"
            forward_body += original_body
            
            # Build and send message
            message = MIMEText(forward_body)
            message["to"] = ", ".join(to_list)
            message["subject"] = subject
            
            raw_message = base64.urlsafe_b64encode(
                message.as_bytes()
            ).decode("utf-8")
            
            sent_message = service.users().messages().send(
                userId="me",
                body={"raw": raw_message}
            ).execute()
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Forwarded email: {sent_message.get('id')}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "message_id": sent_message.get("id"),
                    "thread_id": sent_message.get("threadId"),
                    "to": to_list,
                    "subject": subject,
                    "forwarded_from": message_id,
                },
                can_rollback=False,
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Gmail API error forwarding email: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Gmail API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error forwarding email: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def execute_action(
        self,
        action_type: ActionType,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Execute a Gmail action by type.
        
        Args:
            action_type: Type of action to execute
            credentials: Google OAuth credentials
            params: Action parameters
            
        Returns:
            ActionStepResult: Result of the action
        """
        action_handlers = {
            ActionType.SEND_EMAIL: self.send_email,
            ActionType.CREATE_DRAFT: self.create_draft,
            ActionType.REPLY_EMAIL: self.reply_email,
            ActionType.FORWARD_EMAIL: self.forward_email,
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

