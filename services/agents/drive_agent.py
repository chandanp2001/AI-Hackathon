"""Google Drive data connector agent.

This agent handles queries related to files, folders,
documents, and content stored in Google Drive.
"""

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
    DriveFile,
)
from services.agents.base_agent import BaseDataAgent
from services.llm.openai_service import OpenAIService

logger = logging.getLogger(__name__)


# Common MIME types for Google Workspace files
GOOGLE_DOCS_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveAgent(BaseDataAgent):
    """Google Drive data connector agent.
    
    Handles queries about:
    - Files and documents
    - Folders and organization
    - Google Docs, Sheets, Slides
    - File metadata (owner, modified, shared)
    - File content search
    - Recent and shared files
    
    Args:
        llm_service: Shared LLM service for OpenAI operations
        
    Examples:
        >>> agent = DriveAgent()
        >>> score = await agent.evaluate_relevance("Find the project proposal document")
        >>> if score.score >= 0.5:
        ...     result = await agent.fetch_data(query, credentials)
    """
    
    def __init__(self, llm_service: Optional[OpenAIService] = None):
        super().__init__(llm_service)
        self._max_results = 25
        self._content_preview_length = 1000
        
    @property
    def agent_name(self) -> str:
        """Return the agent identifier."""
        return "drive"
    
    @property
    def agent_type(self) -> AgentType:
        """Return the agent type."""
        return AgentType.DRIVE
    
    @property
    def data_source_description(self) -> str:
        """Return description of the Drive data source."""
        return """Google Drive - Contains:
- Files and documents stored in the cloud
- Google Docs (documents, reports, notes)
- Google Sheets (spreadsheets, data)
- Google Slides (presentations)
- PDFs and other uploaded files
- Folders and file organization
- File metadata (owner, created date, modified date)
- Shared files and folders
- File content (searchable text within documents)
- Recent files and activity
- File permissions and sharing status"""

    async def fetch_data(
        self,
        query: str,
        credentials: Credentials,
        search_terms: Optional[list[str]] = None
    ) -> AgentResult:
        """Fetch files from Google Drive based on the query.
        
        Args:
            query: User's natural language query
            credentials: Google OAuth credentials
            search_terms: Optional search terms from relevance evaluation
            
        Returns:
            AgentResult: Contains list of DriveFile objects
            
        Raises:
            HttpError: If Drive API call fails
        """
        start_time = time.time()
        
        try:
            service = build("drive", "v3", credentials=credentials)
            
            # Build Drive search query
            drive_query = self._build_drive_query(query, search_terms)
            
            # Define fields to retrieve
            fields = (
                "files(id, name, mimeType, createdTime, modifiedTime, "
                "size, webViewLink, owners, shared, parents)"
            )
            
            # Search for files
            results = service.files().list(
                q=drive_query,
                pageSize=self._max_results,
                fields=fields,
                orderBy="modifiedTime desc"
            ).execute()
            
            files = results.get("files", [])
            
            # Convert to structured format and optionally fetch content previews
            drive_files = []
            for file in files:
                drive_file = self._parse_file(file)
                
                # Try to get content preview for text-based files
                if self._is_previewable(file.get("mimeType", "")):
                    content = await self._get_content_preview(
                        service, file["id"], file.get("mimeType", "")
                    )
                    drive_file.content_preview = content
                    
                drive_files.append(drive_file)
                
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(
                f"Drive agent fetched {len(drive_files)} files "
                f"(took {execution_time:.1f}ms)"
            )
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=[file.model_dump() for file in drive_files],
                metadata={
                    "drive_query": drive_query,
                    "total_files": len(drive_files),
                    "max_results": self._max_results
                },
                execution_time_ms=execution_time,
                query_used=drive_query
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Drive API error: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Drive API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error in Drive agent: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    def _build_drive_query(
        self,
        query: str,
        search_terms: Optional[list[str]] = None
    ) -> str:
        """Build a Google Drive search query from natural language.
        
        Args:
            query: Natural language query
            search_terms: Suggested search terms from LLM
            
        Returns:
            str: Drive API search query string
        """
        query_parts = []
        query_lower = query.lower()
        
        # Full text search with terms
        if search_terms:
            # Combine search terms for fullText search
            terms = " ".join(search_terms)
            query_parts.append(f"fullText contains '{terms}'")
        else:
            # Extract keywords from query
            stop_words = {
                "find", "search", "show", "get", "my", "me", "the", "a", "an",
                "files", "file", "documents", "document", "folder", "folders",
                "in", "drive", "google", "about", "with", "called", "named"
            }
            words = query.split()
            key_words = [w for w in words if w.lower() not in stop_words and len(w) > 2]
            if key_words:
                terms = " ".join(key_words[:5])  # Limit to 5 keywords
                query_parts.append(f"fullText contains '{terms}'")
                
        # File type filters
        if "document" in query_lower or "doc" in query_lower:
            query_parts.append(f"mimeType = '{GOOGLE_DOCS_MIME}'")
        elif "spreadsheet" in query_lower or "sheet" in query_lower:
            query_parts.append(f"mimeType = '{GOOGLE_SHEETS_MIME}'")
        elif "presentation" in query_lower or "slide" in query_lower:
            query_parts.append(f"mimeType = '{GOOGLE_SLIDES_MIME}'")
        elif "folder" in query_lower:
            query_parts.append(f"mimeType = '{GOOGLE_FOLDER_MIME}'")
        elif "pdf" in query_lower:
            query_parts.append("mimeType = 'application/pdf'")
            
        # Shared files
        if "shared" in query_lower:
            query_parts.append("sharedWithMe = true")
            
        # Recent files
        if "recent" in query_lower:
            # Drive API doesn't have "recent" but we can sort by modifiedTime
            pass  # Handled by orderBy in the API call
            
        # Exclude trashed files
        query_parts.append("trashed = false")
        
        return " and ".join(query_parts) if query_parts else "trashed = false"
        
    def _parse_file(self, file: dict[str, Any]) -> DriveFile:
        """Parse a Google Drive file into structured format.
        
        Args:
            file: Raw file data from Drive API
            
        Returns:
            DriveFile: Structured file object
        """
        # Parse timestamps
        created_time = None
        modified_time = None
        
        if file.get("createdTime"):
            try:
                created_time = datetime.fromisoformat(
                    file["createdTime"].replace("Z", "+00:00")
                )
            except ValueError:
                pass
                
        if file.get("modifiedTime"):
            try:
                modified_time = datetime.fromisoformat(
                    file["modifiedTime"].replace("Z", "+00:00")
                )
            except ValueError:
                pass
                
        # Parse owners
        owners = [
            owner.get("emailAddress", "")
            for owner in file.get("owners", [])
            if owner.get("emailAddress")
        ]
        
        # Parse size (Google Workspace files don't have size)
        size_bytes = None
        if file.get("size"):
            try:
                size_bytes = int(file["size"])
            except (ValueError, TypeError):
                pass
                
        return DriveFile(
            file_id=file.get("id", ""),
            name=file.get("name", "Unknown"),
            mime_type=file.get("mimeType", ""),
            created_time=created_time,
            modified_time=modified_time,
            size_bytes=size_bytes,
            web_view_link=file.get("webViewLink"),
            owners=owners,
            shared=file.get("shared", False),
            parent_folders=[],  # Would need additional API calls to resolve
            content_preview=None
        )
        
    def _is_previewable(self, mime_type: str) -> bool:
        """Check if file type supports content preview.
        
        Args:
            mime_type: MIME type of the file
            
        Returns:
            bool: True if content can be previewed
        """
        previewable_types = {
            GOOGLE_DOCS_MIME,
            GOOGLE_SHEETS_MIME,
            GOOGLE_SLIDES_MIME,
            "text/plain",
            "text/html",
        }
        return mime_type in previewable_types
        
    async def _get_content_preview(
        self,
        service: Any,
        file_id: str,
        mime_type: str
    ) -> Optional[str]:
        """Get a preview of file content.
        
        Args:
            service: Drive API service
            file_id: File ID
            mime_type: File MIME type
            
        Returns:
            str: Content preview or None
        """
        try:
            if mime_type == GOOGLE_DOCS_MIME:
                # Export as plain text
                content = service.files().export(
                    fileId=file_id,
                    mimeType="text/plain"
                ).execute()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                return content[:self._content_preview_length]
                
            elif mime_type == GOOGLE_SHEETS_MIME:
                # Export as CSV (first sheet only)
                content = service.files().export(
                    fileId=file_id,
                    mimeType="text/csv"
                ).execute()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                return content[:self._content_preview_length]
                
            elif mime_type in ("text/plain", "text/html"):
                # Download directly
                content = service.files().get_media(fileId=file_id).execute()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                return content[:self._content_preview_length]
                
        except Exception as e:
            logger.warning(f"Failed to get content preview for {file_id}: {e}")
            
        return None

