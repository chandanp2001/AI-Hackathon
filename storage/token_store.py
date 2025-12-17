"""Secure token storage using SQLite with optional encryption.

Provides secure storage for OAuth tokens with encryption support
using the Fernet symmetric encryption scheme.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional, Any

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from config import settings

logger = logging.getLogger(__name__)


class TokenStoreError(Exception):
    """Custom exception for token storage errors."""
    pass


class TokenStore:
    """Secure token storage with SQLite backend.
    
    Stores OAuth tokens encrypted using Fernet symmetric encryption.
    Falls back to unencrypted storage if no encryption key is provided.
    
    Args:
        db_path: Path to SQLite database file
        encryption_key: Optional Fernet encryption key
        
    Examples:
        >>> store = TokenStore()
        >>> await store.initialize()
        >>> await store.store_tokens("user_123", {"access_token": "..."})
        >>> tokens = await store.get_tokens("user_123")
    """
    
    def __init__(
        self,
        db_path: Optional[str] = None,
        encryption_key: Optional[str] = None
    ):
        # Parse database path from URL format
        if db_path is None:
            db_url = settings.database_url
            if db_url.startswith("sqlite"):
                # Extract path from sqlite:///path or sqlite+aiosqlite:///path
                db_path = db_url.split("///")[-1]
            else:
                db_path = "./data/tokens.db"
                
        self._db_path = db_path
        self._encryption_key = encryption_key or settings.encryption_key
        self._fernet: Optional[Fernet] = None
        self._connection: Optional[aiosqlite.Connection] = None
        
        if self._encryption_key:
            try:
                self._fernet = Fernet(self._encryption_key.encode())
            except Exception as e:
                logger.warning(f"Invalid encryption key, tokens will not be encrypted: {e}")
                
    async def initialize(self) -> None:
        """Initialize the database and create tables.
        
        Creates the tokens table if it doesn't exist.
        """
        # Ensure directory exists
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        
        self._connection = await aiosqlite.connect(self._db_path)
        
        # Create tokens table
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                user_id TEXT PRIMARY KEY,
                token_data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await self._connection.commit()
        
        logger.info(f"Token store initialized at {self._db_path}")
        
    async def store_tokens(
        self,
        user_id: str,
        token_data: dict[str, Any]
    ) -> None:
        """Store tokens for a user.
        
        Args:
            user_id: User identifier
            token_data: Token data dictionary to store
            
        Raises:
            TokenStoreError: If storage fails
        """
        if self._connection is None:
            raise TokenStoreError("Token store not initialized")
            
        try:
            # Serialize token data
            data_json = json.dumps(token_data)
            
            # Encrypt if key available
            if self._fernet:
                data_json = self._fernet.encrypt(data_json.encode()).decode()
                
            now = datetime.utcnow().isoformat()
            
            # Upsert token data
            await self._connection.execute("""
                INSERT INTO tokens (user_id, token_data, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    token_data = excluded.token_data,
                    updated_at = excluded.updated_at
            """, (user_id, data_json, now, now))
            await self._connection.commit()
            
            logger.debug(f"Stored tokens for user {user_id}")
            
        except Exception as e:
            logger.error(f"Failed to store tokens for user {user_id}: {e}")
            raise TokenStoreError(f"Failed to store tokens: {str(e)}")
            
    async def get_tokens(self, user_id: str) -> Optional[dict[str, Any]]:
        """Retrieve tokens for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            dict: Token data or None if not found
            
        Raises:
            TokenStoreError: If retrieval fails
        """
        if self._connection is None:
            raise TokenStoreError("Token store not initialized")
            
        try:
            async with self._connection.execute(
                "SELECT token_data FROM tokens WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                
            if not row:
                return None
                
            data_json = row[0]
            
            # Decrypt if encrypted
            if self._fernet:
                try:
                    data_json = self._fernet.decrypt(data_json.encode()).decode()
                except InvalidToken:
                    # Data might not be encrypted (legacy or encryption disabled)
                    pass
                    
            return json.loads(data_json)
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid token data for user {user_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to get tokens for user {user_id}: {e}")
            raise TokenStoreError(f"Failed to retrieve tokens: {str(e)}")
            
    async def delete_tokens(self, user_id: str) -> bool:
        """Delete tokens for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            bool: True if tokens were deleted
            
        Raises:
            TokenStoreError: If deletion fails
        """
        if self._connection is None:
            raise TokenStoreError("Token store not initialized")
            
        try:
            cursor = await self._connection.execute(
                "DELETE FROM tokens WHERE user_id = ?",
                (user_id,)
            )
            await self._connection.commit()
            
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Deleted tokens for user {user_id}")
            return deleted
            
        except Exception as e:
            logger.error(f"Failed to delete tokens for user {user_id}: {e}")
            raise TokenStoreError(f"Failed to delete tokens: {str(e)}")
            
    async def list_users(self) -> list[str]:
        """List all users with stored tokens.
        
        Returns:
            list: User IDs with stored tokens
        """
        if self._connection is None:
            raise TokenStoreError("Token store not initialized")
            
        try:
            async with self._connection.execute(
                "SELECT user_id FROM tokens"
            ) as cursor:
                rows = await cursor.fetchall()
                
            return [row[0] for row in rows]
            
        except Exception as e:
            logger.error(f"Failed to list users: {e}")
            raise TokenStoreError(f"Failed to list users: {str(e)}")
            
    async def health_check(self) -> bool:
        """Check if token store is healthy.
        
        Returns:
            bool: True if store is operational
        """
        if self._connection is None:
            return False
            
        try:
            await self._connection.execute("SELECT 1")
            return True
        except Exception:
            return False
            
    async def shutdown(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Token store shutdown")
            
    def get_metrics(self) -> dict[str, Any]:
        """Get token store metrics.
        
        Returns:
            dict: Store metrics
        """
        return {
            "db_path": self._db_path,
            "encrypted": self._fernet is not None,
            "connected": self._connection is not None
        }

