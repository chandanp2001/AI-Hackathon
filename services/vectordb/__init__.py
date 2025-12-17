"""Vector database services for semantic search.

This module provides ChromaDB integration for document embedding
and semantic search capabilities.
"""

from services.vectordb.chroma_service import ChromaService, SearchResult

__all__ = ["ChromaService", "SearchResult"]

