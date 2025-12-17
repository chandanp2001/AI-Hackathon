"""Google Drive data connector agent with action and semantic search capabilities.

This agent handles:
- Reading: file search, document lookup, folder contents
- Actions: creating documents, spreadsheets, sharing files
- Semantic Search: vector-based document search using ChromaDB
"""

import logging
import time
from datetime import datetime
from typing import Optional, Any
from io import BytesIO

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from models.agent_response import (
    AgentResult,
    AgentType,
    DriveFile,
)
from models.action import (
    ActionStepResult,
    ActionType,
    CreateDocumentParams,
    ShareFileParams,
)
from services.agents.base_agent import BaseDataAgent
from services.llm.openai_service import OpenAIService
from services.document_processor import DocumentProcessor
from services.vectordb.chroma_service import ChromaService, SearchResult

logger = logging.getLogger(__name__)


# Common MIME types for Google Workspace files
GOOGLE_DOCS_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveAgent(BaseDataAgent):
    """Google Drive data connector agent with semantic search.
    
    Handles queries about:
    - Files and documents
    - Folders and organization
    - Google Docs, Sheets, Slides
    - File metadata (owner, modified, shared)
    - File content search
    - Recent and shared files
    - Semantic search across indexed documents
    
    Args:
        llm_service: Shared LLM service for OpenAI operations
        enable_semantic_search: Whether to enable ChromaDB semantic search
        
    Examples:
        >>> agent = DriveAgent(enable_semantic_search=True)
        >>> await agent.initialize()
        >>> score = await agent.evaluate_relevance("Find the project proposal document")
        >>> if score.score >= 0.5:
        ...     result = await agent.fetch_data(query, credentials)
    """
    
    def __init__(
        self,
        llm_service: Optional[OpenAIService] = None,
        enable_semantic_search: bool = True
    ):
        super().__init__(llm_service)
        self._max_results = 30  # More results for better coverage
        self._content_preview_length = 2000  # Reasonable preview length
        
        # Semantic search components
        self._enable_semantic_search = enable_semantic_search
        self._chroma_service: Optional[ChromaService] = None
        self._doc_processor: Optional[DocumentProcessor] = None
        
        if enable_semantic_search:
            self._chroma_service = ChromaService(collection_name="drive_documents")
            self._doc_processor = DocumentProcessor(chunk_size=500, chunk_overlap=50)
        
    async def initialize(self) -> None:
        """Initialize the agent and semantic search components.
        
        Sets up ChromaDB connection for vector search if enabled.
        """
        await super().initialize()
        
        if self._enable_semantic_search and self._chroma_service:
            try:
                await self._chroma_service.initialize()
                logger.info("Drive agent semantic search initialized")
            except Exception as e:
                logger.error(f"Failed to initialize semantic search: {e}")
                self._enable_semantic_search = False
    
    async def shutdown(self) -> None:
        """Shutdown the agent and cleanup resources."""
        if self._chroma_service:
            await self._chroma_service.shutdown()
        await super().shutdown()
    
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
            # LIMIT: Only fetch content previews for top 5 files to avoid timeout
            MAX_PREVIEW_FILES = 5
            drive_files = []
            preview_count = 0
            
            for file in files:
                drive_file = self._parse_file(file)
                
                # Try to get content preview for text-based files (limited)
                if preview_count < MAX_PREVIEW_FILES and self._is_previewable(file.get("mimeType", "")):
                    try:
                        content = await self._get_content_preview(
                            service, file["id"], file.get("mimeType", "")
                        )
                        drive_file.content_preview = content
                        preview_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to get preview for {file.get('name')}: {e}")
                    
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
    
    # =========================================================================
    # SEMANTIC SEARCH METHODS
    # =========================================================================
    
    async def index_files(
        self,
        credentials: Credentials,
        file_ids: Optional[list[str]] = None,
        user_id: Optional[str] = None
    ) -> dict[str, Any]:
        """Index Drive files for semantic search.
        
        Fetches file content, processes into chunks, and stores in ChromaDB.
        
        Args:
            credentials: Google OAuth credentials
            file_ids: Optional list of specific file IDs to index.
                     If None, indexes recent indexable files.
            user_id: Optional user ID for filtering in search
            
        Returns:
            dict: Indexing results with counts and any errors
            
        Raises:
            ValueError: If semantic search is not enabled
        """
        if not self._enable_semantic_search or not self._chroma_service:
            raise ValueError("Semantic search is not enabled")
        
        start_time = time.time()
        results = {
            "files_processed": 0,
            "chunks_added": 0,
            "errors": [],
            "skipped": []
        }
        
        try:
            service = build("drive", "v3", credentials=credentials)
            
            if file_ids:
                # Index specific files
                files_to_index = []
                for file_id in file_ids:
                    try:
                        file_info = service.files().get(
                            fileId=file_id,
                            fields="id, name, mimeType"
                        ).execute()
                        files_to_index.append(file_info)
                    except HttpError as e:
                        results["errors"].append(f"Failed to get file {file_id}: {str(e)}")
            else:
                # Get recent indexable files
                indexable_types = [
                    GOOGLE_DOCS_MIME,
                    GOOGLE_SHEETS_MIME,
                    GOOGLE_SLIDES_MIME,
                    "application/pdf",
                    "text/plain"
                ]
                
                mime_filter = " or ".join([f"mimeType = '{t}'" for t in indexable_types])
                query = f"({mime_filter}) and trashed = false"
                
                response = service.files().list(
                    q=query,
                    pageSize=50,
                    fields="files(id, name, mimeType)",
                    orderBy="modifiedTime desc"
                ).execute()
                
                files_to_index = response.get("files", [])
            
            logger.info(f"Found {len(files_to_index)} files to index")
            
            for file in files_to_index:
                file_id = file["id"]
                file_name = file["name"]
                mime_type = file["mimeType"]
                
                # Check if file type is supported
                if not self._doc_processor.is_supported_type(mime_type):
                    results["skipped"].append(f"{file_name}: unsupported type")
                    continue
                
                try:
                    # Process file into chunks
                    chunks = await self._doc_processor.process_drive_file(
                        service=service,
                        file_id=file_id,
                        file_name=file_name,
                        mime_type=mime_type
                    )
                    
                    if chunks:
                        # Add chunks to vector store
                        chunk_count = await self._chroma_service.add_chunks(
                            chunks=chunks,
                            user_id=user_id
                        )
                        results["chunks_added"] += chunk_count
                        results["files_processed"] += 1
                        logger.info(f"Indexed {file_name}: {chunk_count} chunks")
                    else:
                        results["skipped"].append(f"{file_name}: no content extracted")
                        
                except Exception as e:
                    results["errors"].append(f"{file_name}: {str(e)}")
                    logger.error(f"Failed to index {file_name}: {e}")
            
            execution_time = (time.time() - start_time) * 1000
            results["execution_time_ms"] = execution_time
            
            logger.info(
                f"Indexing complete: {results['files_processed']} files, "
                f"{results['chunks_added']} chunks, {len(results['errors'])} errors "
                f"(took {execution_time:.1f}ms)"
            )
            
            return results
            
        except Exception as e:
            logger.error(f"Indexing failed: {e}")
            results["errors"].append(f"Indexing failed: {str(e)}")
            return results
    
    async def semantic_search(
        self,
        query: str,
        credentials: Credentials,
        top_k: int = 10,
        user_id: Optional[str] = None,
        file_ids: Optional[list[str]] = None
    ) -> AgentResult:
        """Perform semantic search across indexed documents.
        
        Uses vector similarity to find relevant document chunks.
        
        Args:
            query: Natural language search query
            credentials: Google OAuth credentials
            top_k: Number of results to return
            user_id: Optional user ID filter
            file_ids: Optional list of file IDs to search within
            
        Returns:
            AgentResult: Search results with relevant chunks
        """
        start_time = time.time()
        
        if not self._enable_semantic_search or not self._chroma_service:
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message="Semantic search is not enabled",
                execution_time_ms=(time.time() - start_time) * 1000
            )
        
        try:
            # Perform semantic search
            search_results = await self._chroma_service.search(
                query=query,
                top_k=top_k,
                user_id=user_id,
                file_ids=file_ids
            )
            
            # Convert to response format
            data = []
            for result in search_results:
                data.append({
                    "content": result.content,
                    "file_id": result.file_id,
                    "file_name": result.file_name,
                    "chunk_index": result.chunk_index,
                    "relevance_score": result.score,
                    "metadata": result.metadata
                })
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(
                f"Semantic search returned {len(data)} results "
                f"(took {execution_time:.1f}ms)"
            )
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=data,
                metadata={
                    "search_type": "semantic",
                    "query": query,
                    "top_k": top_k,
                    "result_count": len(data)
                },
                execution_time_ms=execution_time,
                query_used=query
            )
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Semantic search failed: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Semantic search failed: {str(e)}",
                execution_time_ms=execution_time
            )
    
    async def fetch_data_with_semantic_search(
        self,
        query: str,
        credentials: Credentials,
        search_terms: Optional[list[str]] = None,
        use_semantic: bool = True
    ) -> AgentResult:
        """Fetch data using both Drive API and semantic search.
        
        Combines traditional Drive search with semantic vector search
        for better results.
        
        Args:
            query: User's natural language query
            credentials: Google OAuth credentials
            search_terms: Optional search terms from relevance evaluation
            use_semantic: Whether to include semantic search results
            
        Returns:
            AgentResult: Combined results from both search methods
        """
        start_time = time.time()
        
        try:
            # Get traditional Drive search results
            drive_result = await self.fetch_data(query, credentials, search_terms)
            
            # If semantic search is not enabled or requested, return Drive results only
            if not use_semantic or not self._enable_semantic_search:
                return drive_result
            
            # Perform semantic search
            semantic_result = await self.semantic_search(
                query=query,
                credentials=credentials,
                top_k=5
            )
            
            # Combine results
            combined_data = []
            seen_file_ids = set()
            
            # Add Drive results first (they have full file metadata)
            if drive_result.success and drive_result.data:
                for item in drive_result.data:
                    file_id = item.get("file_id", "")
                    if file_id and file_id not in seen_file_ids:
                        combined_data.append(item)
                        seen_file_ids.add(file_id)
            
            # Add semantic search results with content previews
            semantic_chunks = []
            if semantic_result.success and semantic_result.data:
                for chunk in semantic_result.data:
                    file_id = chunk.get("file_id", "")
                    # Include semantic matches with their relevance scores
                    semantic_chunks.append({
                        "file_id": file_id,
                        "file_name": chunk.get("file_name", ""),
                        "content_preview": chunk.get("content", ""),
                        "relevance_score": chunk.get("relevance_score", 0),
                        "chunk_index": chunk.get("chunk_index", 0),
                        "source": "semantic_search"
                    })
            
            execution_time = (time.time() - start_time) * 1000
            
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=True,
                data=combined_data,
                metadata={
                    "drive_results": len(combined_data),
                    "semantic_chunks": semantic_chunks,
                    "semantic_match_count": len(semantic_chunks),
                    "combined_search": True
                },
                execution_time_ms=execution_time,
                query_used=query
            )
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Combined search failed: {e}")
            return AgentResult(
                agent_name=self.agent_name,
                agent_type=self.agent_type,
                success=False,
                error_message=f"Combined search failed: {str(e)}",
                execution_time_ms=execution_time
            )
    
    async def get_index_status(self) -> dict[str, Any]:
        """Get the status of the semantic search index.
        
        Returns:
            dict: Index statistics and status
        """
        if not self._enable_semantic_search or not self._chroma_service:
            return {
                "enabled": False,
                "message": "Semantic search is not enabled"
            }
        
        stats = await self._chroma_service.get_stats()
        stats["enabled"] = True
        return stats
        
    # =========================================================================
    # ACTION METHODS
    # =========================================================================
    
    async def create_document(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Create a new Google Doc.
        
        Args:
            credentials: Google OAuth credentials
            params: Document parameters including:
                - title: Document title (required)
                - content: Initial content (optional)
                - folder_id: Parent folder ID (optional)
                
        Returns:
            ActionStepResult: Result with document info or error
        """
        start_time = time.time()
        
        try:
            drive_service = build("drive", "v3", credentials=credentials)
            
            title = params.get("title", "Untitled Document")
            
            # Create file metadata
            file_metadata = {
                "name": title,
                "mimeType": GOOGLE_DOCS_MIME,
            }
            
            if params.get("folder_id"):
                file_metadata["parents"] = [params["folder_id"]]
                
            # Create the document
            doc = drive_service.files().create(
                body=file_metadata,
                fields="id, name, webViewLink"
            ).execute()
            
            doc_id = doc.get("id")
            
            # If content is provided, add it to the document
            if params.get("content"):
                docs_service = build("docs", "v1", credentials=credentials)
                
                # Insert content at the beginning
                requests = [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": params["content"]
                        }
                    }
                ]
                
                docs_service.documents().batchUpdate(
                    documentId=doc_id,
                    body={"requests": requests}
                ).execute()
                
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Created Google Doc: {doc_id}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "file_id": doc_id,
                    "name": doc.get("name"),
                    "web_view_link": doc.get("webViewLink"),
                    "mime_type": GOOGLE_DOCS_MIME,
                },
                can_rollback=True,
                rollback_info={"file_id": doc_id},
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Drive API error creating document: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Drive API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error creating document: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def create_spreadsheet(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Create a new Google Sheet.
        
        Args:
            credentials: Google OAuth credentials
            params: Spreadsheet parameters including:
                - title: Spreadsheet title (required)
                - folder_id: Parent folder ID (optional)
                - initial_data: 2D array of initial data (optional)
                
        Returns:
            ActionStepResult: Result with spreadsheet info or error
        """
        start_time = time.time()
        
        try:
            drive_service = build("drive", "v3", credentials=credentials)
            
            title = params.get("title", "Untitled Spreadsheet")
            
            # Create file metadata
            file_metadata = {
                "name": title,
                "mimeType": GOOGLE_SHEETS_MIME,
            }
            
            if params.get("folder_id"):
                file_metadata["parents"] = [params["folder_id"]]
                
            # Create the spreadsheet
            sheet = drive_service.files().create(
                body=file_metadata,
                fields="id, name, webViewLink"
            ).execute()
            
            sheet_id = sheet.get("id")
            
            # If initial data is provided, add it
            if params.get("initial_data"):
                sheets_service = build("sheets", "v4", credentials=credentials)
                
                body = {
                    "values": params["initial_data"]
                }
                
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range="A1",
                    valueInputOption="RAW",
                    body=body
                ).execute()
                
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Created Google Sheet: {sheet_id}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "file_id": sheet_id,
                    "name": sheet.get("name"),
                    "web_view_link": sheet.get("webViewLink"),
                    "mime_type": GOOGLE_SHEETS_MIME,
                },
                can_rollback=True,
                rollback_info={"file_id": sheet_id},
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Drive API error creating spreadsheet: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Drive API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error creating spreadsheet: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def share_file(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Share a file with another user.
        
        Args:
            credentials: Google OAuth credentials
            params: Share parameters including:
                - file_id: File to share (required)
                - email: Email to share with (required)
                - role: Permission role (reader, commenter, writer)
                - send_notification: Whether to send email notification
                - message: Custom notification message
                
        Returns:
            ActionStepResult: Result with permission info or error
        """
        start_time = time.time()
        
        try:
            service = build("drive", "v3", credentials=credentials)
            
            file_id = params.get("file_id")
            email = params.get("email")
            
            if not file_id or not email:
                return ActionStepResult(
                    step_number=1,
                    success=False,
                    error_message="Missing file_id or email",
                    execution_time_ms=(time.time() - start_time) * 1000
                )
                
            # Get file name for response
            try:
                file_info = service.files().get(
                    fileId=file_id,
                    fields="name, webViewLink"
                ).execute()
            except HttpError:
                file_info = {"name": "Unknown", "webViewLink": None}
                
            # Create permission
            permission = {
                "type": "user",
                "role": params.get("role", "reader"),
                "emailAddress": email
            }
            
            send_notification = params.get("send_notification", True)
            email_message = params.get("message")
            
            created_permission = service.permissions().create(
                fileId=file_id,
                body=permission,
                sendNotificationEmail=send_notification,
                emailMessage=email_message,
                fields="id, role, emailAddress"
            ).execute()
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Shared file {file_id} with {email}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "permission_id": created_permission.get("id"),
                    "file_id": file_id,
                    "file_name": file_info.get("name"),
                    "shared_with": email,
                    "role": created_permission.get("role"),
                    "web_view_link": file_info.get("webViewLink"),
                },
                can_rollback=True,
                rollback_info={
                    "file_id": file_id,
                    "permission_id": created_permission.get("id")
                },
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Drive API error sharing file: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Drive API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error sharing file: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Unexpected error: {str(e)}",
                execution_time_ms=execution_time
            )
            
    async def create_folder(
        self,
        credentials: Credentials,
        params: dict[str, Any]
    ) -> ActionStepResult:
        """Create a new folder in Google Drive.
        
        Args:
            credentials: Google OAuth credentials
            params: Folder parameters including:
                - name: Folder name (required)
                - parent_id: Parent folder ID (optional)
                
        Returns:
            ActionStepResult: Result with folder info or error
        """
        start_time = time.time()
        
        try:
            service = build("drive", "v3", credentials=credentials)
            
            name = params.get("name", "New Folder")
            
            file_metadata = {
                "name": name,
                "mimeType": GOOGLE_FOLDER_MIME
            }
            
            if params.get("parent_id"):
                file_metadata["parents"] = [params["parent_id"]]
                
            folder = service.files().create(
                body=file_metadata,
                fields="id, name, webViewLink"
            ).execute()
            
            execution_time = (time.time() - start_time) * 1000
            
            logger.info(f"Created folder: {folder.get('id')}")
            
            return ActionStepResult(
                step_number=1,
                success=True,
                result_data={
                    "folder_id": folder.get("id"),
                    "name": folder.get("name"),
                    "web_view_link": folder.get("webViewLink"),
                },
                can_rollback=True,
                rollback_info={"file_id": folder.get("id")},
                execution_time_ms=execution_time
            )
            
        except HttpError as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Drive API error creating folder: {e}")
            return ActionStepResult(
                step_number=1,
                success=False,
                error_message=f"Drive API error: {str(e)}",
                execution_time_ms=execution_time
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error creating folder: {e}")
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
        """Execute a Drive action by type.
        
        Args:
            action_type: Type of action to execute
            credentials: Google OAuth credentials
            params: Action parameters
            
        Returns:
            ActionStepResult: Result of the action
        """
        action_handlers = {
            ActionType.CREATE_DOCUMENT: self.create_document,
            ActionType.CREATE_SPREADSHEET: self.create_spreadsheet,
            ActionType.SHARE_FILE: self.share_file,
            ActionType.CREATE_FOLDER: self.create_folder,
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

