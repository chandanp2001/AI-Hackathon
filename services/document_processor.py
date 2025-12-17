"""Document processor for parsing and chunking documents.

This module handles parsing of various document formats (PDFs, Google Docs, 
Google Sheets) and chunking them into segments suitable for vector embedding.

Supported formats:
- PDF files
- Google Docs (exported as plain text)
- Google Sheets (exported as CSV)
- Plain text files
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional, Any
from io import BytesIO

import tiktoken

logger = logging.getLogger(__name__)


@dataclass
class DocumentChunk:
    """A chunk of document content ready for embedding.
    
    Args:
        content: The text content of the chunk
        file_id: Source file identifier
        file_name: Source file name
        chunk_index: Index of this chunk within the document
        total_chunks: Total number of chunks for the document
        mime_type: MIME type of the source document
        metadata: Additional metadata (page number, section, etc.)
        token_count: Number of tokens in the chunk
        
    Examples:
        >>> chunk = DocumentChunk(
        ...     content="This is sample content...",
        ...     file_id="abc123",
        ...     file_name="report.pdf",
        ...     chunk_index=0,
        ...     total_chunks=5,
        ...     mime_type="application/pdf",
        ...     token_count=150
        ... )
    """
    content: str
    file_id: str
    file_name: str
    chunk_index: int
    total_chunks: int
    mime_type: str
    metadata: dict[str, Any] = None
    token_count: int = 0
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
            
    def to_dict(self) -> dict[str, Any]:
        """Convert chunk to dictionary for storage.
        
        Returns:
            dict: Dictionary representation of the chunk
        """
        return {
            "content": self.content,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "chunk_index": self.chunk_index,
            "total_chunks": self.total_chunks,
            "mime_type": self.mime_type,
            "metadata": self.metadata,
            "token_count": self.token_count
        }


class DocumentProcessor:
    """Processor for parsing and chunking documents.
    
    Handles multiple document formats and creates chunks suitable for
    vector embedding and semantic search.
    
    Args:
        chunk_size: Target size of chunks in tokens (default 500)
        chunk_overlap: Number of overlapping tokens between chunks (default 50)
        
    Examples:
        >>> processor = DocumentProcessor(chunk_size=500, chunk_overlap=50)
        >>> chunks = processor.chunk_text(content, file_id, file_name, mime_type)
        >>> print(f"Created {len(chunks)} chunks")
    """
    
    # MIME types for Google Workspace files
    GOOGLE_DOCS_MIME = "application/vnd.google-apps.document"
    GOOGLE_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
    GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"
    PDF_MIME = "application/pdf"
    
    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50
    ):
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._encoding = tiktoken.get_encoding("cl100k_base")
        
    def count_tokens(self, text: str) -> int:
        """Count the number of tokens in text.
        
        Args:
            text: Text to count tokens for
            
        Returns:
            int: Number of tokens
        """
        return len(self._encoding.encode(text))
    
    def parse_pdf(self, pdf_content: bytes) -> str:
        """Parse PDF content to plain text.
        
        Args:
            pdf_content: Raw PDF bytes
            
        Returns:
            str: Extracted text from PDF
            
        Raises:
            ValueError: If PDF cannot be parsed
        """
        try:
            from pypdf import PdfReader
            
            reader = PdfReader(BytesIO(pdf_content))
            text_parts = []
            
            for page_num, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text:
                    # Add page marker for reference
                    text_parts.append(f"[Page {page_num + 1}]\n{page_text}")
                    
            full_text = "\n\n".join(text_parts)
            logger.info(f"Parsed PDF with {len(reader.pages)} pages")
            return full_text
            
        except Exception as e:
            logger.error(f"Failed to parse PDF: {e}")
            raise ValueError(f"PDF parsing failed: {str(e)}")
    
    def parse_google_doc(self, doc_content: str) -> str:
        """Parse Google Doc content.
        
        Google Docs are exported as plain text via Drive API, so this
        method primarily cleans up the text.
        
        Args:
            doc_content: Plain text content from exported Google Doc
            
        Returns:
            str: Cleaned text content
        """
        # Clean up the text
        text = doc_content.strip()
        
        # Normalize whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        
        return text
    
    def parse_google_sheet(self, csv_content: str) -> str:
        """Parse Google Sheet CSV content.
        
        Converts CSV data into a structured text format suitable for
        embedding and semantic search.
        
        Args:
            csv_content: CSV content from exported Google Sheet
            
        Returns:
            str: Structured text representation of the spreadsheet
        """
        import csv
        from io import StringIO
        
        try:
            reader = csv.reader(StringIO(csv_content))
            rows = list(reader)
            
            if not rows:
                return ""
            
            # Use first row as headers if it looks like headers
            headers = rows[0] if rows else []
            data_rows = rows[1:] if len(rows) > 1 else []
            
            text_parts = []
            
            # Add header info
            if headers:
                text_parts.append(f"Columns: {', '.join(headers)}")
                text_parts.append("")
            
            # Convert each row to a readable format
            for row_idx, row in enumerate(data_rows):
                if not any(cell.strip() for cell in row):
                    continue  # Skip empty rows
                    
                if headers:
                    # Create key-value pairs for each cell
                    row_parts = []
                    for i, cell in enumerate(row):
                        if cell.strip():
                            header = headers[i] if i < len(headers) else f"Column{i+1}"
                            row_parts.append(f"{header}: {cell}")
                    if row_parts:
                        text_parts.append(f"Row {row_idx + 1}: {', '.join(row_parts)}")
                else:
                    # Just join cells
                    text_parts.append(f"Row {row_idx + 1}: {', '.join(row)}")
            
            return "\n".join(text_parts)
            
        except Exception as e:
            logger.warning(f"Failed to parse sheet as CSV, using raw content: {e}")
            return csv_content
    
    def chunk_text(
        self,
        content: str,
        file_id: str,
        file_name: str,
        mime_type: str,
        metadata: Optional[dict[str, Any]] = None
    ) -> list[DocumentChunk]:
        """Split text content into chunks for embedding.
        
        Uses a sliding window approach with overlap to ensure context
        is preserved across chunk boundaries.
        
        Args:
            content: Text content to chunk
            file_id: Source file identifier
            file_name: Source file name
            mime_type: MIME type of the source
            metadata: Additional metadata to include in chunks
            
        Returns:
            list[DocumentChunk]: List of document chunks
            
        Raises:
            ValueError: If content is empty
        """
        if not content or not content.strip():
            logger.warning(f"Empty content for file {file_id}")
            return []
        
        # Split content into sentences/paragraphs
        segments = self._split_into_segments(content)
        
        if not segments:
            return []
        
        chunks = []
        current_chunk = []
        current_tokens = 0
        
        for segment in segments:
            segment_tokens = self.count_tokens(segment)
            
            # If single segment exceeds chunk size, split it further
            if segment_tokens > self._chunk_size:
                # Save current chunk if not empty
                if current_chunk:
                    chunk_text = " ".join(current_chunk)
                    chunks.append(DocumentChunk(
                        content=chunk_text,
                        file_id=file_id,
                        file_name=file_name,
                        chunk_index=len(chunks),
                        total_chunks=0,  # Will update later
                        mime_type=mime_type,
                        metadata=metadata or {},
                        token_count=self.count_tokens(chunk_text)
                    ))
                    current_chunk = []
                    current_tokens = 0
                
                # Split large segment by words
                words = segment.split()
                word_chunk = []
                word_tokens = 0
                
                for word in words:
                    word_token_count = self.count_tokens(word + " ")
                    if word_tokens + word_token_count > self._chunk_size:
                        if word_chunk:
                            chunk_text = " ".join(word_chunk)
                            chunks.append(DocumentChunk(
                                content=chunk_text,
                                file_id=file_id,
                                file_name=file_name,
                                chunk_index=len(chunks),
                                total_chunks=0,
                                mime_type=mime_type,
                                metadata=metadata or {},
                                token_count=self.count_tokens(chunk_text)
                            ))
                        # Start new chunk with overlap
                        overlap_start = max(0, len(word_chunk) - 10)
                        word_chunk = word_chunk[overlap_start:] + [word]
                        word_tokens = self.count_tokens(" ".join(word_chunk))
                    else:
                        word_chunk.append(word)
                        word_tokens += word_token_count
                
                # Add remaining words to current chunk
                if word_chunk:
                    current_chunk = word_chunk
                    current_tokens = word_tokens
                    
            elif current_tokens + segment_tokens > self._chunk_size:
                # Current chunk is full, save it
                chunk_text = " ".join(current_chunk)
                chunks.append(DocumentChunk(
                    content=chunk_text,
                    file_id=file_id,
                    file_name=file_name,
                    chunk_index=len(chunks),
                    total_chunks=0,
                    mime_type=mime_type,
                    metadata=metadata or {},
                    token_count=self.count_tokens(chunk_text)
                ))
                
                # Start new chunk with overlap from previous chunk
                overlap_segments = []
                overlap_tokens = 0
                for seg in reversed(current_chunk):
                    seg_tokens = self.count_tokens(seg)
                    if overlap_tokens + seg_tokens <= self._chunk_overlap:
                        overlap_segments.insert(0, seg)
                        overlap_tokens += seg_tokens
                    else:
                        break
                
                current_chunk = overlap_segments + [segment]
                current_tokens = overlap_tokens + segment_tokens
            else:
                current_chunk.append(segment)
                current_tokens += segment_tokens
        
        # Add final chunk
        if current_chunk:
            chunk_text = " ".join(current_chunk)
            chunks.append(DocumentChunk(
                content=chunk_text,
                file_id=file_id,
                file_name=file_name,
                chunk_index=len(chunks),
                total_chunks=0,
                mime_type=mime_type,
                metadata=metadata or {},
                token_count=self.count_tokens(chunk_text)
            ))
        
        # Update total_chunks for all chunks
        total = len(chunks)
        for chunk in chunks:
            chunk.total_chunks = total
        
        logger.info(
            f"Created {total} chunks for {file_name} "
            f"(avg {sum(c.token_count for c in chunks) // max(1, total)} tokens/chunk)"
        )
        
        return chunks
    
    def _split_into_segments(self, content: str) -> list[str]:
        """Split content into semantic segments (sentences/paragraphs).
        
        Args:
            content: Text content to split
            
        Returns:
            list[str]: List of text segments
        """
        # Split by paragraphs first
        paragraphs = re.split(r'\n\s*\n', content)
        
        segments = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
                
            # If paragraph is short enough, keep it as one segment
            if self.count_tokens(para) <= self._chunk_size // 2:
                segments.append(para)
            else:
                # Split by sentences
                sentences = re.split(r'(?<=[.!?])\s+', para)
                for sent in sentences:
                    sent = sent.strip()
                    if sent:
                        segments.append(sent)
        
        return segments
    
    async def process_drive_file(
        self,
        service: Any,
        file_id: str,
        file_name: str,
        mime_type: str
    ) -> list[DocumentChunk]:
        """Process a Google Drive file and return chunks.
        
        Fetches the file content from Drive API and processes it
        according to its type.
        
        Args:
            service: Google Drive API service
            file_id: File ID in Drive
            file_name: Name of the file
            mime_type: MIME type of the file
            
        Returns:
            list[DocumentChunk]: List of document chunks
            
        Raises:
            ValueError: If file type is not supported
        """
        try:
            content = None
            
            if mime_type == self.GOOGLE_DOCS_MIME:
                # Export Google Doc as plain text
                content = service.files().export(
                    fileId=file_id,
                    mimeType="text/plain"
                ).execute()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                content = self.parse_google_doc(content)
                
            elif mime_type == self.GOOGLE_SHEETS_MIME:
                # Export Google Sheet as CSV
                content = service.files().export(
                    fileId=file_id,
                    mimeType="text/csv"
                ).execute()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                content = self.parse_google_sheet(content)
                
            elif mime_type == self.GOOGLE_SLIDES_MIME:
                # Export Google Slides as plain text
                content = service.files().export(
                    fileId=file_id,
                    mimeType="text/plain"
                ).execute()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                content = self.parse_google_doc(content)
                
            elif mime_type == self.PDF_MIME:
                # Download PDF content
                pdf_content = service.files().get_media(fileId=file_id).execute()
                content = self.parse_pdf(pdf_content)
                
            elif mime_type.startswith("text/"):
                # Download text files directly
                content = service.files().get_media(fileId=file_id).execute()
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
            else:
                logger.warning(f"Unsupported file type: {mime_type}")
                return []
            
            if not content:
                return []
            
            return self.chunk_text(
                content=content,
                file_id=file_id,
                file_name=file_name,
                mime_type=mime_type
            )
            
        except Exception as e:
            logger.error(f"Failed to process file {file_id}: {e}")
            return []
    
    def is_supported_type(self, mime_type: str) -> bool:
        """Check if a MIME type is supported for processing.
        
        Args:
            mime_type: MIME type to check
            
        Returns:
            bool: True if type is supported
        """
        supported = {
            self.GOOGLE_DOCS_MIME,
            self.GOOGLE_SHEETS_MIME,
            self.GOOGLE_SLIDES_MIME,
            self.PDF_MIME,
            "text/plain",
            "text/html",
            "text/csv",
        }
        return mime_type in supported or mime_type.startswith("text/")

