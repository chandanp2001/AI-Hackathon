"""Conversation memory manager with token counting and summarization.

Handles:
- Token counting for conversation history
- Automatic summarization when token limit approached
- Building context for LLM with history
"""

import logging
from typing import Optional, Any

import tiktoken

from models.session import Message, MessageRole, SessionSummary
from storage.session_store import SessionStore

logger = logging.getLogger(__name__)

# Token budget for conversation history
MAX_HISTORY_TOKENS = 4000
# Reserve tokens for summary
SUMMARY_TOKEN_BUDGET = 500
# Threshold to trigger summarization (percentage of max)
SUMMARIZATION_THRESHOLD = 0.8


class ConversationManager:
    """Manages conversation history with token-aware summarization.
    
    Tracks message history per session, counts tokens, and automatically
    summarizes older messages when approaching the token limit.
    
    Args:
        session_store: SessionStore for persistence
        llm_service: OpenAI service for summarization
        max_tokens: Maximum tokens for conversation history
        
    Examples:
        >>> manager = ConversationManager(session_store, llm_service)
        >>> history = await manager.get_conversation_context(session_id)
        >>> # history contains formatted messages within token budget
    """
    
    def __init__(
        self,
        session_store: SessionStore,
        llm_service: Optional[Any] = None,
        max_tokens: int = MAX_HISTORY_TOKENS
    ):
        self._session_store = session_store
        self._llm_service = llm_service
        self._max_tokens = max_tokens
        
        # Use gpt-4 encoding for token counting
        try:
            self._encoding = tiktoken.encoding_for_model("gpt-4")
        except KeyError:
            self._encoding = tiktoken.get_encoding("cl100k_base")
            
    def set_llm_service(self, llm_service: Any) -> None:
        """Set the LLM service for summarization.
        
        Args:
            llm_service: OpenAI service instance
        """
        self._llm_service = llm_service
            
    def count_tokens(self, text: str) -> int:
        """Count tokens in a text string.
        
        Args:
            text: Text to count tokens for
            
        Returns:
            int: Token count
        """
        return len(self._encoding.encode(text))
        
    def count_message_tokens(self, messages: list[dict[str, str]]) -> int:
        """Count tokens in a list of messages.
        
        Args:
            messages: List of message dicts with role and content
            
        Returns:
            int: Total token count
        """
        total = 0
        for msg in messages:
            # Account for message overhead (role, separators)
            total += 4  # Approximate overhead per message
            total += self.count_tokens(msg.get("content", ""))
            total += self.count_tokens(msg.get("role", ""))
        return total
        
    async def get_conversation_context(
        self,
        session_id: str,
        include_system_prompt: bool = True
    ) -> list[dict[str, str]]:
        """Get conversation history formatted for LLM context.
        
        Retrieves messages from the session, includes any summary of older
        messages, and ensures the total stays within token budget.
        
        Args:
            session_id: Session identifier
            include_system_prompt: Whether to leave room for system prompt
            
        Returns:
            List of message dicts with role and content
        """
        # Get all messages and summary
        messages = await self._session_store.get_messages(session_id)
        summary = await self._session_store.get_summary(session_id)
        
        if not messages:
            return []
            
        # Calculate available tokens
        available_tokens = self._max_tokens
        if include_system_prompt:
            available_tokens -= 500  # Reserve for system prompt
            
        # Build message list
        formatted_messages: list[dict[str, str]] = []
        
        # Add summary if exists
        if summary and summary.summary:
            summary_msg = {
                "role": "system",
                "content": f"Previous conversation summary:\n{summary.summary}"
            }
            formatted_messages.append(summary_msg)
            available_tokens -= self.count_message_tokens([summary_msg])
            
            # Only include messages after the summarized point
            if summary.summarized_up_to:
                # Find the index of the last summarized message
                summary_idx = -1
                for i, msg in enumerate(messages):
                    if msg.message_id == summary.summarized_up_to:
                        summary_idx = i
                        break
                if summary_idx >= 0:
                    messages = messages[summary_idx + 1:]
        
        # Add messages from oldest to newest, tracking tokens
        message_tokens = []
        for msg in messages:
            msg_dict = {"role": msg.role.value, "content": msg.content}
            tokens = self.count_message_tokens([msg_dict])
            message_tokens.append((msg, msg_dict, tokens))
            
        # Calculate total tokens needed
        total_needed = sum(t[2] for t in message_tokens)
        
        # If we need to summarize, do it
        if total_needed > available_tokens * SUMMARIZATION_THRESHOLD:
            await self._summarize_old_messages(session_id, messages, available_tokens)
            # Recursively get context after summarization
            return await self.get_conversation_context(session_id, include_system_prompt)
            
        # Add all messages that fit
        for msg, msg_dict, tokens in message_tokens:
            if tokens <= available_tokens:
                formatted_messages.append(msg_dict)
                available_tokens -= tokens
            else:
                # Truncate message content if needed
                max_content_tokens = available_tokens - 10  # Leave some buffer
                if max_content_tokens > 50:
                    truncated_content = self._truncate_to_tokens(
                        msg.content, max_content_tokens
                    )
                    formatted_messages.append({
                        "role": msg.role.value,
                        "content": truncated_content + "... [truncated]"
                    })
                break
                
        return formatted_messages
        
    def _truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate text to fit within token limit.
        
        Args:
            text: Text to truncate
            max_tokens: Maximum tokens
            
        Returns:
            str: Truncated text
        """
        tokens = self._encoding.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return self._encoding.decode(tokens[:max_tokens])
        
    async def _summarize_old_messages(
        self,
        session_id: str,
        messages: list[Message],
        available_tokens: int
    ) -> None:
        """Summarize older messages to free up token budget.
        
        Takes the oldest messages and summarizes them, storing the summary
        for future context building.
        
        Args:
            session_id: Session identifier
            messages: All messages in session
            available_tokens: Available token budget
        """
        if not self._llm_service:
            logger.warning("No LLM service available for summarization")
            return
            
        if len(messages) < 4:
            # Not enough messages to summarize
            return
            
        # Determine how many messages to summarize (keep last 4-6 recent)
        keep_recent = min(6, len(messages) // 2)
        to_summarize = messages[:-keep_recent]
        
        if not to_summarize:
            return
            
        # Get existing summary
        existing_summary = await self._session_store.get_summary(session_id)
        
        # Build text to summarize
        summary_input = ""
        if existing_summary and existing_summary.summary:
            summary_input = f"Previous summary:\n{existing_summary.summary}\n\nNew messages to incorporate:\n"
            
        for msg in to_summarize:
            summary_input += f"{msg.role.value}: {msg.content}\n"
            
        # Generate summary using LLM
        try:
            summary_prompt = f"""Summarize this conversation concisely, preserving key information, decisions, and context that would be needed to continue the conversation naturally.

{summary_input}

Provide a concise summary (max 200 words) that captures:
- Main topics discussed
- Key decisions or conclusions
- Important context for future messages
- Any pending questions or action items"""

            summary_text = await self._llm_service.generate(
                prompt=summary_prompt,
                max_tokens=SUMMARY_TOKEN_BUDGET,
                temperature=0.3
            )
            
            # Save summary
            last_summarized_id = to_summarize[-1].message_id
            token_count = self.count_tokens(summary_text)
            
            await self._session_store.save_summary(
                session_id=session_id,
                summary=summary_text.strip(),
                summarized_up_to=last_summarized_id,
                token_count=token_count
            )
            
            logger.info(
                f"Summarized {len(to_summarize)} messages for session {session_id} "
                f"({token_count} tokens)"
            )
            
        except Exception as e:
            logger.error(f"Failed to summarize messages: {e}")
            
    async def add_message_to_session(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None
    ) -> Message:
        """Add a message to a session and check if summarization needed.
        
        Args:
            session_id: Session identifier
            role: Message role (user, assistant, system)
            content: Message content
            metadata: Optional metadata
            
        Returns:
            Message: The created message
        """
        message = await self._session_store.add_message(
            session_id=session_id,
            role=role,
            content=content,
            metadata=metadata
        )
        
        return message
        
    async def generate_session_title(
        self,
        session_id: str,
        first_message: str
    ) -> str:
        """Generate a title for a session based on the first message.
        
        Args:
            session_id: Session identifier
            first_message: The first user message
            
        Returns:
            str: Generated title
        """
        if not self._llm_service:
            # Fallback: use first 50 chars of message
            return first_message[:50] + ("..." if len(first_message) > 50 else "")
            
        try:
            prompt = f"""Generate a short, descriptive title (max 6 words) for a conversation that starts with this message:

"{first_message}"

Return only the title, no quotes or punctuation at the end."""

            title = await self._llm_service.generate(
                prompt=prompt,
                max_tokens=20,
                temperature=0.7
            )
            
            title = title.strip().strip('"\'')
            
            # Update session title
            await self._session_store.update_session_title(session_id, title)
            
            return title
            
        except Exception as e:
            logger.error(f"Failed to generate session title: {e}")
            fallback = first_message[:50] + ("..." if len(first_message) > 50 else "")
            await self._session_store.update_session_title(session_id, fallback)
            return fallback

