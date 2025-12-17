"""MySQL-based session storage for conversation persistence.

Provides async storage for:
- Sessions (conversation containers)
- Messages within sessions
- Session summaries for memory management
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Any

import aiomysql

from config import settings
from models.session import (
    Session,
    SessionWithMessages,
    Message,
    MessageRole,
    SessionSummary,
)

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Get current UTC time with timezone info for consistent timestamp handling."""
    return datetime.now(timezone.utc)


class SessionStoreError(Exception):
    """Custom exception for session storage errors."""
    pass


class SessionStore:
    """MySQL-based session storage.
    
    Stores conversation sessions, messages, and summaries in MySQL.
    Supports async operations using aiomysql.
    
    Examples:
        >>> store = SessionStore()
        >>> await store.initialize()
        >>> session = await store.create_session("user_123")
        >>> await store.add_message(session.session_id, "user", "Hello!")
    """
    
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None
    ):
        self._host = host or settings.mysql_host
        self._port = port or settings.mysql_port
        self._user = user or settings.mysql_user
        self._password = password or settings.mysql_password
        self._database = database or settings.mysql_database
        self._pool: Optional[aiomysql.Pool] = None
        
    async def initialize(self) -> None:
        """Initialize the database connection pool and create tables.
        
        Creates the database if it doesn't exist, then creates tables.
        """
        # First connect without database to create it
        try:
            conn = await aiomysql.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
            )
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS {self._database}"
                )
            conn.close()
        except Exception as e:
            logger.error(f"Failed to create database: {e}")
            raise SessionStoreError(f"Failed to create database: {e}")
        
        # Create connection pool
        try:
            self._pool = await aiomysql.create_pool(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                db=self._database,
                minsize=2,
                maxsize=10,
                autocommit=True
            )
        except Exception as e:
            logger.error(f"Failed to create connection pool: {e}")
            raise SessionStoreError(f"Failed to create connection pool: {e}")
        
        # Create tables
        await self._create_tables()
        logger.info(f"Session store initialized with MySQL at {self._host}:{self._port}")
        
    async def _create_tables(self) -> None:
        """Create required database tables."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # Sessions table
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id VARCHAR(36) PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        title VARCHAR(500),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_user_id (user_id),
                        INDEX idx_updated_at (updated_at)
                    )
                """)
                
                # Messages table
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        message_id VARCHAR(36) PRIMARY KEY,
                        session_id VARCHAR(36) NOT NULL,
                        role ENUM('user', 'assistant', 'system') NOT NULL,
                        content TEXT NOT NULL,
                        metadata JSON,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_session_id (session_id),
                        INDEX idx_created_at (created_at)
                    )
                """)
                
                # Session summaries table
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS session_summaries (
                        session_id VARCHAR(36) PRIMARY KEY,
                        summary TEXT NOT NULL,
                        summarized_up_to VARCHAR(36),
                        token_count INT DEFAULT 0,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                """)
                
    async def create_session(
        self,
        user_id: str,
        title: Optional[str] = None
    ) -> Session:
        """Create a new session.
        
        Args:
            user_id: User identifier
            title: Optional session title
            
        Returns:
            Session: The created session
        """
        session_id = str(uuid.uuid4())
        now = utc_now()
        
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO sessions (session_id, user_id, title, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (session_id, user_id, title, now, now)
                )
                
        logger.debug(f"Created session {session_id} for user {user_id}")
        
        return Session(
            session_id=session_id,
            user_id=user_id,
            title=title,
            created_at=now,
            updated_at=now
        )
        
    async def get_session(self, session_id: str) -> Optional[SessionWithMessages]:
        """Get a session with all its messages.
        
        Args:
            session_id: Session identifier
            
        Returns:
            SessionWithMessages or None if not found
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                # Get session
                await cursor.execute(
                    "SELECT * FROM sessions WHERE session_id = %s",
                    (session_id,)
                )
                session_row = await cursor.fetchone()
                
                if not session_row:
                    return None
                    
                # Get messages
                await cursor.execute(
                    """
                    SELECT * FROM messages 
                    WHERE session_id = %s 
                    ORDER BY created_at ASC
                    """,
                    (session_id,)
                )
                message_rows = await cursor.fetchall()
                
                # Get summary if exists
                await cursor.execute(
                    "SELECT summary FROM session_summaries WHERE session_id = %s",
                    (session_id,)
                )
                summary_row = await cursor.fetchone()
                
        messages = []
        for row in message_rows:
            metadata = row.get('metadata')
            if metadata and isinstance(metadata, str):
                metadata = json.loads(metadata)
            messages.append(Message(
                message_id=row['message_id'],
                session_id=row['session_id'],
                role=MessageRole(row['role']),
                content=row['content'],
                metadata=metadata,
                created_at=row['created_at']
            ))
            
        return SessionWithMessages(
            session_id=session_row['session_id'],
            user_id=session_row['user_id'],
            title=session_row['title'],
            created_at=session_row['created_at'],
            updated_at=session_row['updated_at'],
            messages=messages,
            summary=summary_row['summary'] if summary_row else None
        )
        
    async def list_sessions(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> tuple[list[Session], int]:
        """List sessions for a user.
        
        Args:
            user_id: User identifier
            limit: Maximum sessions to return
            offset: Offset for pagination
            
        Returns:
            Tuple of (sessions list, total count)
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                # Get total count
                await cursor.execute(
                    "SELECT COUNT(*) as count FROM sessions WHERE user_id = %s",
                    (user_id,)
                )
                count_row = await cursor.fetchone()
                total = count_row['count'] if count_row else 0
                
                # Get sessions
                await cursor.execute(
                    """
                    SELECT * FROM sessions 
                    WHERE user_id = %s 
                    ORDER BY updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, limit, offset)
                )
                rows = await cursor.fetchall()
                
        sessions = [
            Session(
                session_id=row['session_id'],
                user_id=row['user_id'],
                title=row['title'],
                created_at=row['created_at'],
                updated_at=row['updated_at']
            )
            for row in rows
        ]
        
        return sessions, total
        
    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages.
        
        Args:
            session_id: Session identifier
            
        Returns:
            bool: True if session was deleted
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # Delete messages first (foreign key)
                await cursor.execute(
                    "DELETE FROM messages WHERE session_id = %s",
                    (session_id,)
                )
                
                # Delete summary
                await cursor.execute(
                    "DELETE FROM session_summaries WHERE session_id = %s",
                    (session_id,)
                )
                
                # Delete session
                await cursor.execute(
                    "DELETE FROM sessions WHERE session_id = %s",
                    (session_id,)
                )
                deleted = cursor.rowcount > 0
                
        if deleted:
            logger.info(f"Deleted session {session_id}")
        return deleted
        
    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None
    ) -> Message:
        """Add a message to a session.
        
        Args:
            session_id: Session identifier
            role: Message role (user, assistant, system)
            content: Message content
            metadata: Optional metadata dict
            
        Returns:
            Message: The created message
        """
        message_id = str(uuid.uuid4())
        now = utc_now()
        
        metadata_json = json.dumps(metadata) if metadata else None
        
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO messages (message_id, session_id, role, content, metadata, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (message_id, session_id, role, content, metadata_json, now)
                )
                
                # Update session's updated_at
                await cursor.execute(
                    "UPDATE sessions SET updated_at = %s WHERE session_id = %s",
                    (now, session_id)
                )
                
        return Message(
            message_id=message_id,
            session_id=session_id,
            role=MessageRole(role),
            content=content,
            metadata=metadata,
            created_at=now
        )
        
    async def get_messages(
        self,
        session_id: str,
        limit: Optional[int] = None,
        after_message_id: Optional[str] = None
    ) -> list[Message]:
        """Get messages for a session.
        
        Args:
            session_id: Session identifier
            limit: Maximum messages to return
            after_message_id: Only return messages after this ID
            
        Returns:
            List of messages
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                if after_message_id:
                    # Get the timestamp of the reference message
                    await cursor.execute(
                        "SELECT created_at FROM messages WHERE message_id = %s",
                        (after_message_id,)
                    )
                    ref_row = await cursor.fetchone()
                    if ref_row:
                        query = """
                            SELECT * FROM messages 
                            WHERE session_id = %s AND created_at > %s
                            ORDER BY created_at ASC
                        """
                        params = [session_id, ref_row['created_at']]
                    else:
                        query = """
                            SELECT * FROM messages 
                            WHERE session_id = %s 
                            ORDER BY created_at ASC
                        """
                        params = [session_id]
                else:
                    query = """
                        SELECT * FROM messages 
                        WHERE session_id = %s 
                        ORDER BY created_at ASC
                    """
                    params = [session_id]
                    
                if limit:
                    query += " LIMIT %s"
                    params.append(limit)
                    
                await cursor.execute(query, params)
                rows = await cursor.fetchall()
                
        messages = []
        for row in rows:
            metadata = row.get('metadata')
            if metadata and isinstance(metadata, str):
                metadata = json.loads(metadata)
            messages.append(Message(
                message_id=row['message_id'],
                session_id=row['session_id'],
                role=MessageRole(row['role']),
                content=row['content'],
                metadata=metadata,
                created_at=row['created_at']
            ))
            
        return messages
        
    async def update_session_title(
        self,
        session_id: str,
        title: str
    ) -> bool:
        """Update a session's title.
        
        Args:
            session_id: Session identifier
            title: New title
            
        Returns:
            bool: True if updated
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE sessions SET title = %s WHERE session_id = %s",
                    (title, session_id)
                )
                return cursor.rowcount > 0
                
    async def save_summary(
        self,
        session_id: str,
        summary: str,
        summarized_up_to: str,
        token_count: int
    ) -> SessionSummary:
        """Save or update a session summary.
        
        Args:
            session_id: Session identifier
            summary: Summary text
            summarized_up_to: message_id of last summarized message
            token_count: Approximate token count of summary
            
        Returns:
            SessionSummary
        """
        now = utc_now()
        
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO session_summaries (session_id, summary, summarized_up_to, token_count, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        summary = VALUES(summary),
                        summarized_up_to = VALUES(summarized_up_to),
                        token_count = VALUES(token_count),
                        updated_at = VALUES(updated_at)
                    """,
                    (session_id, summary, summarized_up_to, token_count, now)
                )
                
        return SessionSummary(
            session_id=session_id,
            summary=summary,
            summarized_up_to=summarized_up_to,
            token_count=token_count,
            updated_at=now
        )
        
    async def get_summary(self, session_id: str) -> Optional[SessionSummary]:
        """Get the summary for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            SessionSummary or None
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT * FROM session_summaries WHERE session_id = %s",
                    (session_id,)
                )
                row = await cursor.fetchone()
                
        if not row:
            return None
            
        return SessionSummary(
            session_id=row['session_id'],
            summary=row['summary'],
            summarized_up_to=row['summarized_up_to'],
            token_count=row['token_count'],
            updated_at=row['updated_at']
        )
        
    async def health_check(self) -> bool:
        """Check if the store is healthy.
        
        Returns:
            bool: True if healthy
        """
        if not self._pool:
            return False
            
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 1")
            return True
        except Exception:
            return False
            
    async def shutdown(self) -> None:
        """Close the connection pool."""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
            logger.info("Session store shutdown")
            
    def get_metrics(self) -> dict[str, Any]:
        """Get store metrics.
        
        Returns:
            dict: Store metrics
        """
        return {
            "host": self._host,
            "port": self._port,
            "database": self._database,
            "connected": self._pool is not None
        }

