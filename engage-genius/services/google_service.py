from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import os
import logging
from typing import Dict, Any, List, Optional

# Configure logging
logger = logging.getLogger(__name__)

def _get_google_credentials():
    """Get Google API credentials from token file"""
    try:
        # Use paths from environment if available
        token_path = os.environ.get('GOOGLE_TOKEN_PATH', 'token.json')
        
        # Check if token exists
        if not os.path.exists(token_path):
            raise FileNotFoundError(f"Google token not found at {token_path}. Please run services/auth/google_auth.py first.")
        
        # Create credentials using the token
        creds = Credentials.from_authorized_user_file(token_path, [
            'https://www.googleapis.com/auth/documents.readonly',
            'https://www.googleapis.com/auth/spreadsheets.readonly'
        ])
        
        return creds
    except Exception as e:
        error_msg = f"Error getting Google credentials: {str(e)}"
        logger.error(error_msg)
        raise RuntimeError(error_msg)

def fetch_google_sheets_data(spreadsheet_id: str, sheet_name: str, range_name: str) -> str:
    """
    Fetch data from a specific Google Sheets spreadsheet, sheet and range
    
    Args:
        spreadsheet_id: The ID of the Google Sheets document
        sheet_name: The name of the sheet to fetch data from
        range_name: The range of cells to fetch (e.g. 'A1:D10')
        
    Returns:
        Formatted string representation of the sheet data
    """
    try:
        # Get credentials
        creds = _get_google_credentials()
        
        # Build the Sheets service
        service = build('sheets', 'v4', credentials=creds)
        
        # Format the range with sheet name if provided separately
        full_range = f"'{sheet_name}'!{range_name}" if sheet_name else range_name
        
        # Call the Sheets API
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=full_range
        ).execute()
        
        # Get spreadsheet metadata for title
        spreadsheet = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id
        ).execute()
        
        spreadsheet_title = spreadsheet['properties']['title']
        
        # Extract values
        values = result.get('values', [])
        
        if not values:
            return f"No data found in {spreadsheet_title}, sheet {sheet_name}, range {range_name}"
        
        # Format data as a string table
        formatted_data = f"Data from {spreadsheet_title}, sheet {sheet_name}, range {range_name}:\n\n"
        
        # Format as a table
        for row in values:
            formatted_data += " | ".join([str(cell) for cell in row]) + "\n"
        
        return formatted_data
        
    except Exception as e:
        error_msg = f"Error fetching Google Sheets data: {str(e)}"
        logger.error(error_msg)
        return f"Error: {error_msg}"

def fetch_google_doc_data(document_id: str) -> str:
    """
    Fetch content from a Google Docs document
    
    Args:
        document_id: The ID of the Google Docs document
        
    Returns:
        String with the document content
    """
    try:
        # Get credentials
        creds = _get_google_credentials()
        
        # Build the Docs service
        service = build('docs', 'v1', credentials=creds)
        
        # Call the Docs API
        document = service.documents().get(documentId=document_id).execute()
        
        # Extract document title
        doc_title = document.get('title', 'Untitled Document')
        
        # Extract document content
        content = []
        if 'body' in document and 'content' in document['body']:
            for element in document['body']['content']:
                if 'paragraph' in element:
                    for part in element['paragraph']['elements']:
                        if 'textRun' in part and 'content' in part['textRun']:
                            content.append(part['textRun']['content'])
        
        doc_content = ''.join(content)
        
        if not doc_content:
            return f"No content found in document {doc_title} ({document_id})"
        
        # Format the document content
        formatted_data = f"Content from Google Doc: {doc_title}\n\n{doc_content}"
        
        return formatted_data
        
    except Exception as e:
        error_msg = f"Error fetching Google Docs data: {str(e)}"
        logger.error(error_msg)
        return f"Error: {error_msg}" 