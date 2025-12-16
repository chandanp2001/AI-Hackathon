"""Gmail data connector agent.

This agent handles queries related to emails, threads,
message search, and email metadata.
"""

import base64
import logging
import time
from datetime import datetime
from typing import Optional, Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from models.agent_response import (
    AgentResult,
    AgentType,
    EmailMessage,
)
from services.agents.base_agent import BaseDataAgent
from services.llm.openai_service import OpenAIService

logger = logging.getLogger(__name__)


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
        search_terms: Optional[list[str]] = None
    ) -> AgentResult:
        """Fetch emails based on the query.
        
        Args:
            query: User's natural language query
            credentials: Google OAuth credentials
            search_terms: Optional search terms from relevance evaluation
            
        Returns:
            AgentResult: Contains list of EmailMessage objects
            
        Raises:
            HttpError: If Gmail API call fails
        """
        start_time = time.time()
        
        try:
            service = build("gmail", "v1", credentials=credentials)
            
            # Build Gmail search query
            gmail_query = self._build_gmail_query(query, search_terms)
            
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
        search_terms: Optional[list[str]] = None
    ) -> str:
        """Build a Gmail search query from natural language.
        
        Args:
            query: Natural language query
            search_terms: Suggested search terms from LLM
            
        Returns:
            str: Gmail search query string
        """
        query_parts = []
        query_lower = query.lower()
        
        # Add suggested search terms
        if search_terms:
            # Join terms for general search
            terms = " ".join(search_terms)
            query_parts.append(terms)
            
        # Parse common patterns
        # From specific sender
        if "from " in query_lower:
            # Try to extract sender name/email after "from"
            pass  # Let search_terms handle this
            
        # Unread emails
        if "unread" in query_lower:
            query_parts.append("is:unread")
            
        # Starred emails
        if "starred" in query_lower or "important" in query_lower:
            query_parts.append("is:starred")
            
        # With attachments
        if "attachment" in query_lower:
            query_parts.append("has:attachment")
            
        # Time-based filters
        if "today" in query_lower:
            query_parts.append("newer_than:1d")
        elif "yesterday" in query_lower:
            query_parts.append("newer_than:2d older_than:1d")
        elif "this week" in query_lower or "week" in query_lower:
            query_parts.append("newer_than:7d")
        elif "this month" in query_lower or "month" in query_lower:
            query_parts.append("newer_than:30d")
            
        # If no specific query built, use search terms or original query
        if not query_parts:
            if search_terms:
                query_parts.append(" ".join(search_terms))
            else:
                # Extract key words from original query
                # Remove common words
                stop_words = {
                    "find", "search", "show", "get", "my", "me", "the", "a", "an",
                    "emails", "email", "messages", "message", "about", "with"
                }
                words = query.split()
                key_words = [w for w in words if w.lower() not in stop_words]
                if key_words:
                    query_parts.append(" ".join(key_words))
                    
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

