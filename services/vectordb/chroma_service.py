"""ChromaDB vector database service for semantic document search.

This module provides integration with ChromaDB for storing and searching
document embeddings using Azure OpenAI's text-embedding-3-large model.

Features:
- Document chunk embedding and storage
- Semantic similarity search
- Metadata filtering
- Hybrid retrieval with multiple signals
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional, Any

import chromadb
from chromadb.config import Settings
import httpx

from config import settings
from services.document_processor import DocumentChunk

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Result from a semantic search query.
    
    Args:
        content: The text content of the chunk
        file_id: Source file identifier
        file_name: Source file name
        chunk_index: Index of this chunk within the document
        score: Similarity score (higher is better, 0-1 range)
        metadata: Additional metadata from the chunk
        
    Examples:
        >>> result = SearchResult(
        ...     content="Project proposal outline...",
        ...     file_id="abc123",
        ...     file_name="proposal.pdf",
        ...     chunk_index=2,
        ...     score=0.92,
        ...     metadata={"page": 3}
        ... )
    """
    content: str
    file_id: str
    file_name: str
    chunk_index: int
    score: float
    metadata: dict[str, Any]
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation.
        
        Returns:
            dict: Dictionary representation of the search result
        """
        return {
            "content": self.content,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "chunk_index": self.chunk_index,
            "score": self.score,
            "metadata": self.metadata
        }


class AzureOpenAIEmbeddings:
    """Azure OpenAI embeddings client using text-embedding-3-large.
    
    Uses the Azure OpenAI API to generate embeddings for text chunks.
    
    Args:
        endpoint: Azure OpenAI endpoint URL
        api_key: Azure OpenAI API key
        deployment: Deployment name for the embedding model
        api_version: API version to use
        
    Examples:
        >>> embeddings = AzureOpenAIEmbeddings(
        ...     endpoint="https://your-endpoint.openai.azure.com/",
        ...     api_key="your-key",
        ...     deployment="text-embedding-3-large"
        ... )
        >>> vectors = await embeddings.embed_documents(["Hello world"])
    """
    
    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        deployment: Optional[str] = None,
        api_version: str = "2023-05-15"
    ):
        self._endpoint = endpoint or settings.azure_openai_embedding_endpoint
        self._api_key = api_key or settings.azure_openai_api_key
        self._deployment = deployment or settings.azure_openai_embedding_deployment
        self._api_version = api_version
        self._dimension = 3072  # text-embedding-3-large dimension
        
        # Build the full URL
        self._url = (
            f"{self._endpoint.rstrip('/')}/openai/deployments/"
            f"{self._deployment}/embeddings?api-version={self._api_version}"
        )
        
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple documents.
        
        Args:
            texts: List of text strings to embed
            
        Returns:
            list[list[float]]: List of embedding vectors
            
        Raises:
            httpx.HTTPError: If API call fails
        """
        if not texts:
            return []
            
        embeddings = []
        
        # Process in batches of 16 to avoid rate limits
        batch_size = 16
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_embeddings = await self._embed_batch(batch)
            embeddings.extend(batch_embeddings)
            
        return embeddings
    
    async def embed_query(self, text: str) -> list[float]:
        """Generate embedding for a single query.
        
        Args:
            text: Query text to embed
            
        Returns:
            list[float]: Embedding vector
            
        Raises:
            httpx.HTTPError: If API call fails
        """
        embeddings = await self.embed_documents([text])
        return embeddings[0] if embeddings else []
    
    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.
        
        Args:
            texts: Batch of texts to embed
            
        Returns:
            list[list[float]]: Batch of embedding vectors
        """
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self._url,
                headers={
                    "api-key": self._api_key,
                    "Content-Type": "application/json"
                },
                json={
                    "input": texts,
                    "model": "text-embedding-3-large"
                }
            )
            response.raise_for_status()
            
            data = response.json()
            
            # Sort by index to maintain order
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in sorted_data]
    
    @property
    def dimension(self) -> int:
        """Get the embedding dimension.
        
        Returns:
            int: Embedding vector dimension
        """
        return self._dimension


class ChromaService:
    """ChromaDB service for document embedding and semantic search.
    
    Provides vector storage and retrieval using ChromaDB with Azure
    OpenAI embeddings for semantic document search.
    
    Args:
        collection_name: Name of the ChromaDB collection
        persist_directory: Directory for persistent storage
        
    Examples:
        >>> service = ChromaService(collection_name="drive_documents")
        >>> await service.initialize()
        >>> await service.add_chunks(chunks)
        >>> results = await service.search("project proposal", top_k=5)
    """
    
    def __init__(
        self,
        collection_name: str = "drive_documents",
        persist_directory: str = "./data/chromadb"
    ):
        self._collection_name = collection_name
        self._persist_directory = persist_directory
        self._client: Optional[chromadb.Client] = None
        self._collection = None
        self._embeddings = AzureOpenAIEmbeddings()
        self._initialized = False
        
    async def initialize(self) -> None:
        """Initialize the ChromaDB client and collection.
        
        Creates the collection if it doesn't exist.
        
        Raises:
            Exception: If initialization fails
        """
        try:
            # Ensure persist directory exists
            os.makedirs(self._persist_directory, exist_ok=True)
            
            # Initialize ChromaDB client with persistence
            self._client = chromadb.PersistentClient(
                path=self._persist_directory,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )
            
            # Get or create collection
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"}  # Use cosine similarity
            )
            
            self._initialized = True
            logger.info(
                f"ChromaDB initialized with collection '{self._collection_name}' "
                f"({self._collection.count()} documents)"
            )
            
        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB: {e}")
            raise
    
    async def add_chunks(
        self,
        chunks: list[DocumentChunk],
        user_id: Optional[str] = None
    ) -> int:
        """Add document chunks to the vector store.
        
        Args:
            chunks: List of document chunks to add
            user_id: Optional user ID for filtering
            
        Returns:
            int: Number of chunks added
            
        Raises:
            ValueError: If service not initialized
        """
        if not self._initialized:
            raise ValueError("ChromaService not initialized. Call initialize() first.")
            
        if not chunks:
            return 0
        
        try:
            # Generate embeddings for all chunks
            texts = [chunk.content for chunk in chunks]
            embeddings = await self._embeddings.embed_documents(texts)
            
            # Prepare data for ChromaDB
            ids = []
            documents = []
            metadatas = []
            
            for i, chunk in enumerate(chunks):
                # Create unique ID
                chunk_id = f"{chunk.file_id}_{chunk.chunk_index}"
                ids.append(chunk_id)
                documents.append(chunk.content)
                
                # Build metadata
                metadata = {
                    "file_id": chunk.file_id,
                    "file_name": chunk.file_name,
                    "chunk_index": chunk.chunk_index,
                    "total_chunks": chunk.total_chunks,
                    "mime_type": chunk.mime_type,
                    "token_count": chunk.token_count
                }
                
                if user_id:
                    metadata["user_id"] = user_id
                    
                # Add any custom metadata
                if chunk.metadata:
                    for key, value in chunk.metadata.items():
                        # ChromaDB only supports string, int, float, bool
                        if isinstance(value, (str, int, float, bool)):
                            metadata[key] = value
                            
                metadatas.append(metadata)
            
            # Upsert to collection (handles duplicates)
            self._collection.upsert(
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas
            )
            
            logger.info(f"Added/updated {len(chunks)} chunks to vector store")
            return len(chunks)
            
        except Exception as e:
            logger.error(f"Failed to add chunks to vector store: {e}")
            raise
    
    async def search(
        self,
        query: str,
        top_k: int = 10,
        user_id: Optional[str] = None,
        file_ids: Optional[list[str]] = None,
        mime_types: Optional[list[str]] = None
    ) -> list[SearchResult]:
        """Search for similar documents using semantic search.
        
        Args:
            query: Search query text
            top_k: Number of results to return
            user_id: Optional user ID filter
            file_ids: Optional list of file IDs to search within
            mime_types: Optional list of MIME types to filter
            
        Returns:
            list[SearchResult]: List of search results sorted by relevance
            
        Raises:
            ValueError: If service not initialized
        """
        if not self._initialized:
            raise ValueError("ChromaService not initialized. Call initialize() first.")
        
        try:
            # Generate query embedding
            query_embedding = await self._embeddings.embed_query(query)
            
            # Build where filter
            where_filter = None
            where_conditions = []
            
            if user_id:
                where_conditions.append({"user_id": {"$eq": user_id}})
                
            if file_ids:
                where_conditions.append({"file_id": {"$in": file_ids}})
                
            if mime_types:
                where_conditions.append({"mime_type": {"$in": mime_types}})
            
            if len(where_conditions) == 1:
                where_filter = where_conditions[0]
            elif len(where_conditions) > 1:
                where_filter = {"$and": where_conditions}
            
            # Query the collection
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where=where_filter,
                include=["documents", "metadatas", "distances"]
            )
            
            # Convert to SearchResult objects
            search_results = []
            
            if results and results["ids"] and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    # Convert distance to similarity score (cosine: 1 - distance)
                    distance = results["distances"][0][i]
                    score = 1 - distance
                    
                    metadata = results["metadatas"][0][i]
                    
                    search_results.append(SearchResult(
                        content=results["documents"][0][i],
                        file_id=metadata.get("file_id", ""),
                        file_name=metadata.get("file_name", ""),
                        chunk_index=metadata.get("chunk_index", 0),
                        score=score,
                        metadata=metadata
                    ))
            
            logger.info(f"Search returned {len(search_results)} results for query: {query[:50]}...")
            return search_results
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
            raise
    
    async def delete_file_chunks(self, file_id: str) -> int:
        """Delete all chunks for a specific file.
        
        Args:
            file_id: File ID to delete chunks for
            
        Returns:
            int: Number of chunks deleted
        """
        if not self._initialized:
            raise ValueError("ChromaService not initialized. Call initialize() first.")
        
        try:
            # Get all chunk IDs for this file
            results = self._collection.get(
                where={"file_id": {"$eq": file_id}},
                include=[]
            )
            
            if results and results["ids"]:
                self._collection.delete(ids=results["ids"])
                count = len(results["ids"])
                logger.info(f"Deleted {count} chunks for file {file_id}")
                return count
            
            return 0
            
        except Exception as e:
            logger.error(f"Failed to delete chunks for file {file_id}: {e}")
            raise
    
    async def get_file_status(self, file_id: str) -> dict[str, Any]:
        """Get indexing status for a file.
        
        Args:
            file_id: File ID to check
            
        Returns:
            dict: Status including chunk count and metadata
        """
        if not self._initialized:
            return {"indexed": False, "chunk_count": 0}
        
        try:
            results = self._collection.get(
                where={"file_id": {"$eq": file_id}},
                include=["metadatas"]
            )
            
            if results and results["ids"]:
                return {
                    "indexed": True,
                    "chunk_count": len(results["ids"]),
                    "file_name": results["metadatas"][0].get("file_name") if results["metadatas"] else None
                }
            
            return {"indexed": False, "chunk_count": 0}
            
        except Exception as e:
            logger.error(f"Failed to get file status: {e}")
            return {"indexed": False, "chunk_count": 0, "error": str(e)}
    
    async def health_check(self) -> bool:
        """Check if the service is healthy.
        
        Returns:
            bool: True if service is operational
        """
        return self._initialized and self._collection is not None
    
    async def get_stats(self) -> dict[str, Any]:
        """Get collection statistics.
        
        Returns:
            dict: Statistics including document count
        """
        if not self._initialized:
            return {"initialized": False}
        
        return {
            "initialized": True,
            "collection_name": self._collection_name,
            "document_count": self._collection.count(),
            "persist_directory": self._persist_directory
        }
    
    async def shutdown(self) -> None:
        """Shutdown the service and cleanup resources."""
        self._initialized = False
        self._collection = None
        self._client = None
        logger.info("ChromaDB service shut down")

