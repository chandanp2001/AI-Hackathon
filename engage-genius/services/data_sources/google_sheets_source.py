from typing import Dict, Any, List, Union, Tuple, Optional
from .base import DataSource
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import logging
import re
import json
import os
import datetime
from collections import defaultdict

class GoogleSheetsDataSource(DataSource):
    def __init__(self):
        """Initialize the Google Sheets data source."""
        super().__init__()
        self.service = None
        self._initialize_service()

    def _initialize_service(self):
        """Initialize the Google Sheets service with credentials."""
        try:
            # Use paths from environment if available
            credentials_path = os.environ.get('GOOGLE_CREDENTIALS_PATH', 'credentials.json')
            token_path = os.environ.get('GOOGLE_TOKEN_PATH', 'token.json')
            
            # Check if token exists
            if not os.path.exists(token_path):
                raise FileNotFoundError(f"Google token not found at {token_path}. Please run services/auth/google_auth.py first.")
            
            # Create client using the token path
            creds = Credentials.from_authorized_user_file(token_path, [
                'https://www.googleapis.com/auth/spreadsheets.readonly'
            ])
            self.service = build('sheets', 'v4', credentials=creds)
        except Exception as e:
            error_msg = f"Error initializing Google Sheets service: {str(e)}"
            logging.error(error_msg)
            raise RuntimeError(error_msg)

    async def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate the provided spreadsheet IDs."""
        try:
            # Log inputs for debugging
            logging.info(f"Validating inputs: {inputs}")
            
            # Check for spreadsheet_urls key
            if 'spreadsheet_urls' not in inputs:
                logging.error("No 'spreadsheet_urls' key found in inputs")
                return False
                
            # Extract spreadsheet IDs
            spreadsheet_ids = self._extract_spreadsheet_ids(inputs.get('spreadsheet_urls', ''))
            
            # Check if we got any spreadsheet IDs
            if not spreadsheet_ids:
                logging.error("No valid spreadsheet IDs extracted")
                return False
                
            # Validate each spreadsheet ID
            for spreadsheet_id in spreadsheet_ids:
                logging.info(f"Validating spreadsheet ID: {spreadsheet_id}")
                try:
                    # Just verify we can access the spreadsheet
                    self.service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
                    logging.info(f"Spreadsheet ID {spreadsheet_id} validation successful")
                except Exception as e:
                    logging.error(f"Spreadsheet ID {spreadsheet_id} validation failed: {e}")
                    return False
                    
            # All validations passed
            return True
            
        except Exception as e:
            logging.error(f"Error validating Google Sheets inputs: {str(e)}")
            return False

    async def fetch_data(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch data from all sheets in specified Google Sheets."""
        try:
            spreadsheet_ids = self._extract_spreadsheet_ids(inputs.get('spreadsheet_urls', ''))
            results = {}
            
            for spreadsheet_id in spreadsheet_ids:
                try:
                    # Get spreadsheet metadata
                    spreadsheet = self.service.spreadsheets().get(
                        spreadsheetId=spreadsheet_id
                    ).execute()
                    
                    sheet_data = {
                        'title': spreadsheet['properties']['title'],
                        'sheets': {}
                    }
                    
                    # Get all sheets in the spreadsheet
                    sheets = spreadsheet.get('sheets', [])
                    
                    # Fetch data from each sheet
                    for sheet in sheets:
                        sheet_title = sheet['properties']['title']
                        try:
                            # Get the sheet's grid properties
                            grid_props = sheet['properties'].get('gridProperties', {})
                            row_count = grid_props.get('rowCount', 1000)
                            col_count = grid_props.get('columnCount', 26)
                            
                            # Convert column count to letter (e.g., 26 -> 'Z')
                            end_col = chr(ord('A') + min(col_count - 1, 25))
                            range_name = f"'{sheet_title}'!A1:{end_col}{row_count}"
                            
                            result = self.service.spreadsheets().values().get(
                                spreadsheetId=spreadsheet_id,
                                range=range_name
                            ).execute()
                            
                            values = result.get('values', [])
                            
                            # Handle empty sheets or sheets with only headers
                            if not values:
                                sheet_data['sheets'][sheet_title] = {
                                    'headers': [],
                                    'data': [],
                                    'warning': 'Sheet is empty'
                                }
                                continue
                            
                            # LAYER 1: Data Preprocessing - Auto-detect header rows and handle jagged/unstructured rows
                            headers, data, structure_info = self._preprocess_sheet_data(values)
                            
                            # LAYER 2: Semantic Structuring - Guess column roles
                            column_roles = self._determine_column_roles(headers, data)
                            
                            sheet_data['sheets'][sheet_title] = {
                                'headers': headers,
                                'data': data,
                                'structure_info': structure_info,
                                'column_roles': column_roles
                            }
                            
                        except Exception as e:
                            logging.error(f"Error fetching sheet {sheet_title}: {str(e)}")
                            sheet_data['sheets'][sheet_title] = {
                                'error': f"Could not fetch sheet: {str(e)}"
                            }
                    
                    results[spreadsheet_id] = sheet_data
                    
                except Exception as e:
                    logging.error(f"Error fetching spreadsheet {spreadsheet_id}: {str(e)}")
                    results[spreadsheet_id] = {
                        'error': f"Could not fetch spreadsheet: {str(e)}"
                    }
            
            if not results:
                return {'error': 'No data found in any spreadsheet'}
                
            return results
            
        except Exception as e:
            logging.error(f"Error in fetch_data: {str(e)}")
            return {'error': str(e)}

    def _preprocess_sheet_data(self, values: List[List[str]]) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
        """
        Preprocess sheet data to handle various sheet structures.
        
        Args:
            values: Raw values from the sheet
            
        Returns:
            Tuple of (headers, data_rows, structure_info)
        """
        if not values:
            return [], [], {'structure_type': 'empty'}
            
        # Structure info to help LLM understand the sheet
        structure_info = {
            'structure_type': 'standard',  # Default: standard table with headers
            'confidence': 0.8,
            'notes': []
        }
        
        # Check if first row might not be headers (heuristic: if first row has mostly numeric values)
        first_row = values[0]
        numeric_count = sum(1 for cell in first_row if cell and re.match(r'^-?\d+(\.\d+)?$', str(cell).strip()))
        
        # If >50% of cells in first row are numeric, it might not be headers
        if numeric_count > len(first_row) * 0.5 and len(values) > 1:
            # Generate generic headers
            headers = [f"Column {i+1}" for i in range(len(first_row))]
            data = values  # Use all rows as data
            structure_info['structure_type'] = 'no_headers'
            structure_info['confidence'] = 0.6
            structure_info['notes'].append("First row appears to contain data rather than headers")
        else:
            # Normal case - first row is headers
            headers = first_row
            data = values[1:] if len(values) > 1 else []
        
        # Check for completely empty headers and replace with generic names
        for i, header in enumerate(headers):
            if not header or header.strip() == "":
                headers[i] = f"Column {i+1}"
                structure_info['notes'].append(f"Empty header in column {i+1} replaced with generic name")
                structure_info['confidence'] -= 0.1  # Reduce confidence
        
        # Handle jagged rows (rows with different numbers of cells)
        max_width = max(len(row) for row in data) if data else len(headers)
        if any(len(row) != max_width for row in data):
            structure_info['structure_type'] = 'jagged'
            structure_info['notes'].append("Sheet contains jagged rows (inconsistent number of cells)")
            structure_info['confidence'] -= 0.2
            
            # Normalize row lengths
            normalized_data = []
            for row in data:
                # Pad or truncate row to match the longest row
                padded_row = (row + [''] * max_width)[:max_width]
                normalized_data.append(padded_row)
            data = normalized_data
            
            # Ensure headers are also the right length
            if len(headers) < max_width:
                for i in range(len(headers), max_width):
                    headers.append(f"Column {i+1}")
            elif len(headers) > max_width:
                headers = headers[:max_width]
        
        # Check if the sheet appears to be a pivot table or other non-standard structure
        # Heuristic: Multiple empty cells in headers and/or merged-looking cells
        empty_headers = sum(1 for h in headers if not h or h.strip() == "")
        if empty_headers > len(headers) * 0.3:
            structure_info['structure_type'] = 'complex'
            structure_info['notes'].append("Sheet appears to have a complex structure (possibly a pivot table)")
            structure_info['confidence'] -= 0.3
        
        return headers, data, structure_info

    def _determine_column_roles(self, headers: List[str], rows: List[List[str]]) -> Dict[int, Dict[str, Any]]:
        """
        Determine the semantic role of each column (metric, date, category, etc.)
        
        Args:
            headers: Column headers
            rows: Data rows
            
        Returns:
            Dictionary mapping column indices to role information
        """
        column_roles = {}
        # Get basic types first
        column_types = self._determine_column_types(headers, rows)
        
        for col_idx, header in enumerate(headers):
            header_lower = header.lower() if header else ""
            basic_type = column_types.get(col_idx, 'text')
            
            role_info = {
                'type': basic_type,
                'role': 'unknown',
                'confidence': 0.5
            }
            
            # Check header name for clues
            date_indicators = ['date', 'day', 'month', 'year', 'time', 'created', 'updated', 'timestamp']
            metric_indicators = ['count', 'total', 'sum', 'average', 'avg', 'mean', 'median', 'min', 'max', 
                               'percentage', 'percent', 'rate', 'ratio', 'score', 'value', 'amount']
            id_indicators = ['id', 'key', 'code', 'identifier', 'uuid', 'guid']
            category_indicators = ['type', 'category', 'class', 'group', 'status', 'state', 'level']
            name_indicators = ['name', 'title', 'label', 'description', 'desc']
            
            # Check header name against indicators
            if any(indicator in header_lower for indicator in date_indicators) or basic_type == 'date':
                role_info['role'] = 'date'
                role_info['confidence'] = 0.8
            elif any(indicator in header_lower for indicator in metric_indicators) or basic_type == 'number':
                role_info['role'] = 'metric'
                role_info['confidence'] = 0.8
            elif any(indicator in header_lower for indicator in id_indicators):
                role_info['role'] = 'identifier'
                role_info['confidence'] = 0.9
            elif any(indicator in header_lower for indicator in category_indicators):
                role_info['role'] = 'category'
                role_info['confidence'] = 0.7
            elif any(indicator in header_lower for indicator in name_indicators):
                role_info['role'] = 'name'
                role_info['confidence'] = 0.7
            else:
                # Make educated guesses based on data patterns
                if basic_type == 'number':
                    # Check if values are small integers (likely counts or IDs)
                    try:
                        values = [float(row[col_idx]) for row in rows if col_idx < len(row) and row[col_idx].strip()]
                        if values and all(v.is_integer() and 0 <= v < 1000 for v in values):
                            role_info['role'] = 'count'
                            role_info['confidence'] = 0.6
                        else:
                            role_info['role'] = 'metric'
                            role_info['confidence'] = 0.6
                    except (ValueError, TypeError):
                        role_info['role'] = 'metric'
                        role_info['confidence'] = 0.5
                elif basic_type == 'text':
                    # Check if column has low cardinality (few unique values) - likely a category
                    unique_values = set()
                    for row in rows:
                        if col_idx < len(row) and row[col_idx].strip():
                            unique_values.add(row[col_idx].strip())
                    
                    if 1 < len(unique_values) <= 20 and len(rows) > 5:
                        # Few unique values relative to row count suggests a category
                        role_info['role'] = 'category'
                        role_info['confidence'] = 0.6
                    elif len(unique_values) == len(rows) and len(rows) > 5:
                        # All values unique suggests an identifier or name
                        role_info['role'] = 'identifier'
                        role_info['confidence'] = 0.6
                    else:
                        role_info['role'] = 'text'
                        role_info['confidence'] = 0.5
                
            column_roles[col_idx] = role_info
            
        return column_roles

    def format_for_llm(self, data: Dict[str, Any]) -> str:
        """Format the fetched Google Sheets data for LLM consumption using metadata control protocol."""
        # Handle non-dictionary data argument
        if not isinstance(data, dict):
            return f"Error: Google Sheets data is not in expected format. Received: {type(data)}"
            
        if not data or (isinstance(data, dict) and 'error' in data):
            return f"Error retrieving Google Sheets data: {data.get('error', 'Unknown error')}"
            
        formatted = []
        formatted.append("# GOOGLE SHEETS DATA")
        formatted.append("The following sections contain tabular data from Google Sheets.")
        
        # LAYER 3: Add fallback instructions for unstructured sheets
        formatted.append("\n## HOW TO INTERPRET THIS DATA")
        formatted.append("- Standard sheets: Treat as normal tabular data with headers and rows")
        formatted.append("- Complex/Pivot tables: Pay attention to hierarchical structure and row/column relationships")
        formatted.append("- Unstructured data: Look for patterns in content rather than strict tabular interpretation")
        formatted.append("- If headers are unclear: Infer meaning from context and patterns in the data")
        formatted.append("- For numeric columns: Consider their role (metrics, IDs, dates) when interpreting")
        
        any_complex_sheets = False
        
        for spreadsheet_id, sheet_data in data.items():
            # Check if sheet_data is a dictionary before trying to access keys
            if not isinstance(sheet_data, dict):
                formatted.append(f"\n## ERROR: Spreadsheet '{spreadsheet_id}'")
                formatted.append(f"```error\nInvalid spreadsheet data type: {type(sheet_data)}\n```")
                continue
                
            if 'error' in sheet_data:
                formatted.append(f"\n## ERROR: Spreadsheet '{spreadsheet_id}'")
                formatted.append(f"```error\n{sheet_data['error']}\n```")
                continue
            
            # Add spreadsheet title as a main section
            formatted.append(f"\n## SPREADSHEET: {sheet_data['title']}")
            
            # Process each sheet
            for sheet_name, sheet_content in sheet_data.get('sheets', {}).items():
                if 'error' in sheet_content:
                    formatted.append(f"\n### ERROR: Sheet '{sheet_name}'")
                    formatted.append(f"```error\n{sheet_content['error']}\n```")
                    continue
                
                # Get headers and rows, handling empty sheets
                headers = sheet_content.get('headers', [])
                rows = sheet_content.get('data', [])
                structure_info = sheet_content.get('structure_info', {'structure_type': 'standard', 'confidence': 0.8})
                column_roles = sheet_content.get('column_roles', {})
                
                if not headers:
                    formatted.append(f"\n### Sheet '{sheet_name}': Empty or no headers")
                    continue
                
                # Track if this is a complex sheet for later
                if structure_info.get('structure_type') in ['complex', 'jagged', 'no_headers']:
                    any_complex_sheets = True
                
                # Add sheet metadata section
                formatted.append(f"\n### SHEET: {sheet_name}")
                formatted.append("```metadata")
                formatted.append(f"sheet_name: {sheet_name}")
                formatted.append(f"total_rows: {len(rows)}")
                formatted.append(f"total_columns: {len(headers)}")
                formatted.append(f"structure_type: {structure_info.get('structure_type', 'standard')}")
                formatted.append(f"structure_confidence: {structure_info.get('confidence', 0.8)}")
                formatted.append(f"headers: {', '.join(headers)}")
                
                # Add structure notes if any
                if structure_info.get('notes'):
                    formatted.append(f"structure_notes: {'; '.join(structure_info.get('notes', []))}")
                
                # Extract and add numeric metrics for data analysis
                metrics = self._extract_metrics(headers, rows)
                if metrics:
                    for metric_name, value in metrics.items():
                        if metric_name not in ["Total Rows", "Total Columns"]:  # Already included
                            formatted.append(f"{metric_name.lower()}: {value}")
                
                formatted.append("```")
                
                # Add column roles metadata
                formatted.append("```column-roles")
                for col_idx, header in enumerate(headers):
                    if col_idx in column_roles:
                        role_info = column_roles[col_idx]
                        formatted.append(f"{header}: type={role_info['type']}, role={role_info['role']}, confidence={role_info['confidence']:.1f}")
                    else:
                        formatted.append(f"{header}: type=unknown, role=unknown, confidence=0.0")
                formatted.append("```")
                
                # Format the table data with metadata markers
                formatted.append("```tabular-data")
                
                # First, add the headers with column types and roles
                header_parts = []
                for i, header in enumerate(headers):
                    col_type = column_roles.get(i, {}).get('type', 'text')
                    col_role = column_roles.get(i, {}).get('role', 'unknown')
                    header_parts.append(f"{header} ({col_type}/{col_role})")
                
                header_line = "| " + " | ".join(header_parts) + " |"
                formatted.append(header_line)
                
                # Add separator line
                separator = "|" + "|".join(["-" * (len(h) + 10) for h in headers]) + "|"
                formatted.append(separator)
                
                # Add data rows, limited to 30 rows max to avoid token overflow
                display_rows = rows[:30]
                for row in display_rows:
                    # Format each cell value appropriately
                    formatted_row = []
                    for i, cell in enumerate(row):
                        cell_type = column_roles.get(i, {}).get('type', 'text')
                        if cell_type == 'number':
                            # Format numbers with commas for thousands
                            try:
                                value = float(cell)
                                if value.is_integer():
                                    formatted_row.append(f"{int(value):,}")
                                else:
                                    formatted_row.append(f"{value:,.2f}")
                            except:
                                formatted_row.append(cell)
                        else:
                            formatted_row.append(cell)
                    
                    row_line = "| " + " | ".join(formatted_row) + " |"
                    formatted.append(row_line)
                
                # Add indication if data was truncated
                if len(rows) > 30:
                    formatted.append(f"... {len(rows) - 30} more rows (truncated)")
                    
                formatted.append("```")
                
                # Add any detected patterns or insights
                patterns = self._detect_patterns(headers, rows)
                if patterns:
                    formatted.append("```insights")
                    for pattern in patterns:
                        formatted.append(f"- {pattern}")
                    formatted.append("```")
                
                formatted.append("")  # Add extra newline between sheets
            
            formatted.append("---")  # Add separator between spreadsheets
        
        # LAYER 3: Add fallback instructions if complex sheets were detected
        if any_complex_sheets:
            formatted.append("\n## HANDLING COMPLEX SHEETS")
            formatted.append("One or more sheets have been detected as having complex or non-standard structure.")
            formatted.append("For these sheets, consider the following approaches:")
            formatted.append("1. Focus on content and relationships rather than strict table interpretation")
            formatted.append("2. Pay attention to adjacent cells that may form logical groups")
            formatted.append("3. Look for patterns in the data arrangement that indicate hierarchy or grouping")
            formatted.append("4. Be flexible in interpretation - some cells may serve dual purposes")
            formatted.append("5. Treat empty cells as potential structural indicators rather than missing data")
        
        if not formatted:
            return "No data available from Google Sheets"
            
        return "\n".join(formatted)

    def _determine_column_types(self, headers: List[str], rows: List[List[str]]) -> Dict[int, str]:
        """Determine the data type for each column."""
        types = {}
        
        for col_idx, header in enumerate(headers):
            # Skip checking if no rows
            if not rows:
                types[col_idx] = 'text'
                continue
                
            # Check values to determine type
            numeric_count = 0
            date_count = 0
            boolean_count = 0
            text_count = 0
            
            for row in rows:
                if col_idx >= len(row):
                    continue
                    
                value = row[col_idx].strip()
                if not value:
                    continue
                
                # Check if value is a number
                try:
                    float(value.replace(',', ''))
                    numeric_count += 1
                    continue
                except ValueError:
                    pass
                
                # Check if value is a date (improved heuristic)
                if '/' in value or '-' in value:
                    # Basic date pattern check
                    if re.match(r'\d{1,4}[/-]\d{1,2}[/-]\d{1,4}', value):
                        date_count += 1
                        continue
                    
                # Check for ISO date formats (YYYY-MM-DD)
                if re.match(r'^\d{4}-\d{2}-\d{2}', value):
                    date_count += 1
                    continue
                    
                # Check for date with time
                if re.match(r'\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\s+\d{1,2}:\d{2}', value):
                    date_count += 1
                    continue
                
                # Check if boolean-like
                if value.lower() in ['true', 'false', 'yes', 'no', 'y', 'n', 't', 'f', '1', '0']:
                    boolean_count += 1
                    continue
                    
                # Otherwise it's text
                text_count += 1
            
            # Determine predominant type
            total = numeric_count + date_count + boolean_count + text_count
            if total == 0:
                types[col_idx] = 'text'
            elif numeric_count > total * 0.7:
                types[col_idx] = 'number'
            elif date_count > total * 0.7:
                types[col_idx] = 'date'
            elif boolean_count > total * 0.7:
                types[col_idx] = 'boolean'
            else:
                types[col_idx] = 'text'
        
        return types
        
    def _detect_patterns(self, headers: List[str], rows: List[List[str]]) -> List[str]:
        """Detect patterns or insights in the data."""
        if not rows or not headers:
            return []
            
        insights = []
        
        # Detect empty columns
        for i, header in enumerate(headers):
            empty_count = sum(1 for row in rows if i >= len(row) or not row[i].strip())
            if empty_count == len(rows):
                insights.append(f"Column '{header}' is completely empty")
            elif empty_count > len(rows) * 0.8:
                insights.append(f"Column '{header}' is mostly empty ({empty_count}/{len(rows)} empty cells)")
        
        # Detect numeric columns and their trends
        column_types = self._determine_column_types(headers, rows)
        for i, header in enumerate(headers):
            if column_types.get(i) == 'number':
                try:
                    values = [float(row[i].replace(',', '')) for row in rows if i < len(row) and row[i].strip()]
                    if values:
                        avg = sum(values) / len(values)
                        if max(values) > avg * 3:
                            insights.append(f"Column '{header}' has outliers, with maximum {max(values):,.2f} exceeding average {avg:,.2f}")
                        
                        # Check for increasing/decreasing trend
                        increases = sum(1 for j in range(1, len(values)) if values[j] > values[j-1])
                        if increases > len(values) * 0.8:
                            insights.append(f"Column '{header}' shows a predominantly increasing trend")
                        elif increases < len(values) * 0.2:
                            insights.append(f"Column '{header}' shows a predominantly decreasing trend")
                except:
                    pass
        
        # Detect potential relationships between columns
        if len(headers) > 1:
            for i, header_i in enumerate(headers):
                for j, header_j in enumerate(headers):
                    if i >= j:  # Skip duplicate comparisons
                        continue
                        
                    if column_types.get(i) == 'number' and column_types.get(j) == 'number':
                        try:
                            # Check for potential correlations between numeric columns
                            x_values = []
                            y_values = []
                            for row in rows:
                                if i < len(row) and j < len(row) and row[i].strip() and row[j].strip():
                                    try:
                                        x = float(row[i].replace(',', ''))
                                        y = float(row[j].replace(',', ''))
                                        x_values.append(x)
                                        y_values.append(y)
                                    except:
                                        pass
                                        
                            if len(x_values) >= 5:  # Need enough data points
                                # Simple correlation check - if both increase/decrease together
                                x_increases = sum(1 for k in range(1, len(x_values)) if x_values[k] > x_values[k-1])
                                y_increases = sum(1 for k in range(1, len(y_values)) if y_values[k] > y_values[k-1])
                                
                                # If both mostly increase or both mostly decrease together
                                if (x_increases > len(x_values) * 0.7 and y_increases > len(y_values) * 0.7) or \
                                   (x_increases < len(x_values) * 0.3 and y_increases < len(y_values) * 0.3):
                                    insights.append(f"Columns '{header_i}' and '{header_j}' appear to move together")
                        except:
                            pass
        
        return insights

    def _extract_metrics(self, headers: List[str], rows: List[List[str]]) -> Dict[str, Union[int, float, str]]:
        """Extract basic metrics from sheet data."""
        metrics = {
            "Total Rows": len(rows),
            "Total Columns": len(headers)
        }
        
        # Try to identify numeric columns and calculate their stats
        for col_idx, header in enumerate(headers):
            numeric_values = []
            for row in rows:
                if col_idx < len(row):
                    try:
                        value = float(row[col_idx].replace(',', ''))
                        numeric_values.append(value)
                    except (ValueError, AttributeError):
                        continue
            
            if numeric_values:
                avg = sum(numeric_values) / len(numeric_values)
                if avg.is_integer():
                    avg = int(avg)
                metrics[f"{header} (Avg)"] = avg
                metrics[f"{header} (Max)"] = max(numeric_values)
                metrics[f"{header} (Min)"] = min(numeric_values)
                
                # Add standard deviation if enough values
                if len(numeric_values) >= 5:
                    try:
                        mean = sum(numeric_values) / len(numeric_values)
                        variance = sum((x - mean) ** 2 for x in numeric_values) / len(numeric_values)
                        std_dev = variance ** 0.5
                        metrics[f"{header} (StdDev)"] = round(std_dev, 2)
                    except:
                        pass
        
        return metrics

    # LAYER 4: Relevance Mapping
    def calculate_query_relevance(self, query: str, data: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        """
        Calculate relevance scores between user query and sheets/columns.
        
        Args:
            query: User's query string
            data: The sheet data dictionary
            
        Returns:
            Nested dictionary of relevance scores by spreadsheet_id, sheet_name, and column
        """
        relevance_scores = {}
        query_terms = set(re.findall(r'\b\w+\b', query.lower()))
        
        if not query_terms:
            return {}
            
        for spreadsheet_id, spreadsheet_data in data.items():
            if not isinstance(spreadsheet_data, dict) or 'error' in spreadsheet_data:
                continue
                
            sheet_scores = {}
            spreadsheet_title = spreadsheet_data.get('title', '').lower()
            
            # Calculate spreadsheet title relevance
            spreadsheet_title_terms = set(re.findall(r'\b\w+\b', spreadsheet_title))
            title_overlap = len(query_terms.intersection(spreadsheet_title_terms))
            spreadsheet_score = title_overlap / len(query_terms) if query_terms else 0
            
            for sheet_name, sheet_content in spreadsheet_data.get('sheets', {}).items():
                if not isinstance(sheet_content, dict) or 'error' in sheet_content:
                    continue
                    
                # Sheet name relevance
                sheet_name_terms = set(re.findall(r'\b\w+\b', sheet_name.lower()))
                sheet_name_overlap = len(query_terms.intersection(sheet_name_terms))
                sheet_score = 0.2 + (sheet_name_overlap / len(query_terms) * 0.8) if query_terms else 0.2
                
                # Add base spreadsheet score for context
                sheet_score = max(sheet_score, spreadsheet_score * 0.5)
                
                # Column header relevance
                headers = sheet_content.get('headers', [])
                column_scores = {}
                
                for idx, header in enumerate(headers):
                    header_terms = set(re.findall(r'\b\w+\b', header.lower()))
                    header_overlap = len(query_terms.intersection(header_terms))
                    column_score = header_overlap / len(query_terms) if query_terms else 0
                    
                    # Boost score for exact matches
                    if header.lower() in query.lower():
                        column_score += 0.3
                        
                    # Boost score for partial matches
                    for term in query_terms:
                        if term in header.lower() and len(term) > 2:  # Avoid short terms
                            column_score += 0.1
                            
                    column_scores[idx] = min(1.0, column_score)  # Cap at 1.0
                
                sheet_scores[sheet_name] = {
                    'sheet_score': min(1.0, sheet_score),
                    'columns': column_scores
                }
            
            relevance_scores[spreadsheet_id] = sheet_scores
            
        return relevance_scores

    # LAYER 5: Optional Retry/Clarify
    def generate_clarification_questions(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Generate clarification questions for ambiguous or low-confidence sheets.
        
        Args:
            data: Sheet data dictionary
            
        Returns:
            List of potential clarification questions with context
        """
        questions = []
        
        for spreadsheet_id, spreadsheet_data in data.items():
            if not isinstance(spreadsheet_data, dict) or 'error' in spreadsheet_data:
                continue
                
            spreadsheet_title = spreadsheet_data.get('title', '')
            
            for sheet_name, sheet_content in spreadsheet_data.get('sheets', {}).items():
                if not isinstance(sheet_content, dict) or 'error' in sheet_content:
                    continue
                    
                structure_info = sheet_content.get('structure_info', {})
                structure_type = structure_info.get('structure_type', 'standard')
                confidence = structure_info.get('confidence', 0.8)
                
                # Generate questions for low-confidence or complex structures
                if confidence < 0.6 or structure_type in ['complex', 'jagged', 'no_headers']:
                    questions.append({
                        'type': 'structure',
                        'spreadsheet_id': spreadsheet_id,
                        'spreadsheet_title': spreadsheet_title,
                        'sheet_name': sheet_name,
                        'question': f"What is the intended structure of the '{sheet_name}' sheet? It appears to have {structure_type} data arrangement.",
                        'confidence': confidence
                    })
                
                # Check for unclear column purposes
                headers = sheet_content.get('headers', [])
                column_roles = sheet_content.get('column_roles', {})
                
                for idx, header in enumerate(headers):
                    role_info = column_roles.get(idx, {})
                    role_confidence = role_info.get('confidence', 0.5)
                    
                    if role_confidence < 0.4 and any(term in header.lower() for term in ['total', 'sum', 'value', 'id', 'code']):
                        questions.append({
                            'type': 'column_purpose',
                            'spreadsheet_id': spreadsheet_id,
                            'spreadsheet_title': spreadsheet_title,
                            'sheet_name': sheet_name,
                            'column_name': header,
                            'question': f"What does the '{header}' column in '{sheet_name}' represent?",
                            'confidence': role_confidence
                        })
        
        return questions

    def get_form_fields(self) -> List[Dict[str, Any]]:
        """Return the configuration fields required for Google Sheets."""
        return [
            {
                'type': 'text',
                'label': 'Spreadsheet URLs or IDs',
                'name': 'spreadsheet_urls',
                'placeholder': 'Enter Google Sheet URLs or IDs (comma-separated)',
                'required': True,
                'help_text': 'Paste spreadsheet links or IDs. You\'ll be able to select specific sheets after submitting.'
            }
        ]

    def _extract_spreadsheet_ids(self, urls_or_ids: str) -> List[str]:
        """Extract spreadsheet IDs from URLs or return as is if they're IDs."""
        ids = []
        
        # If input is None or empty, return empty list
        if not urls_or_ids:
            logging.warning("No spreadsheet URLs or IDs provided")
            return ids
            
        # Log the input for debugging
        logging.info(f"Extracting spreadsheet IDs from: {urls_or_ids}")
        
        # Handle both string and list inputs
        url_list = []
        if isinstance(urls_or_ids, str):
            # If it's a string, split by comma
            url_list = [item.strip() for item in urls_or_ids.split(',') if item.strip()]
        elif isinstance(urls_or_ids, list):
            # If it's already a list, use it directly
            url_list = urls_or_ids
        else:
            logging.error(f"Unexpected type for urls_or_ids: {type(urls_or_ids)}")
            return ids
            
        # Process each URL or ID
        for item in url_list:
            if not item:
                continue
                
            # Convert to string if not already
            if not isinstance(item, str):
                item = str(item)
                
            # Extract ID from URL if it's a URL
            # Standard sheets URL pattern
            match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', item)
            if match:
                sheet_id = match.group(1)
                # Remove any URL parameters or additional path segments
                sheet_id = sheet_id.split('/')[0]
                sheet_id = sheet_id.split('?')[0]
                ids.append(sheet_id)
                logging.info(f"Extracted spreadsheet ID: {sheet_id} from URL: {item}")
                continue
                
            # Alternative pattern for direct links
            alt_match = re.search(r'sheets.google.com/spreadsheets/d/([a-zA-Z0-9-_]+)', item)
            if alt_match:
                sheet_id = alt_match.group(1)
                # Remove any URL parameters or additional path segments
                sheet_id = sheet_id.split('/')[0]
                sheet_id = sheet_id.split('?')[0]
                ids.append(sheet_id)
                logging.info(f"Extracted spreadsheet ID: {sheet_id} from URL: {item}")
                continue
                
            # Check if it looks like a direct spreadsheet ID (alphanumeric with dashes/underscores, 25-44 chars)
            if re.match(r'^[a-zA-Z0-9-_]{25,44}$', item):
                ids.append(item)
                logging.info(f"Using direct spreadsheet ID: {item}")
                continue
                
            # Last resort - if it contains a spreadsheet ID pattern anywhere in the string
            last_resort_match = re.search(r'([a-zA-Z0-9-_]{25,44})', item)
            if last_resort_match:
                sheet_id = last_resort_match.group(1)
                ids.append(sheet_id)
                logging.info(f"Extracted possible spreadsheet ID: {sheet_id} from text: {item}")
                continue
                
            logging.warning(f"Could not extract spreadsheet ID from: {item}")
            
        # Log the final result
        if ids:
            logging.info(f"Extracted {len(ids)} spreadsheet IDs: {ids}")
        else:
            logging.error("Failed to extract any valid spreadsheet IDs")
            
        return ids 