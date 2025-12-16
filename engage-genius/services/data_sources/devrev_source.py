from typing import Dict, Any, List, Optional, Union, Tuple
from .base import DataSource
import logging
import requests
import json
from config import DEVREV_API_KEY, DEVREV_API_URL
from datetime import datetime
from functools import lru_cache
import re

class DevRevDataSource(DataSource):
    def __init__(self):
        # Ensure API URL is correctly formatted with no trailing slash
        # Default to the standard API URL if not provided in config or if it's misconfigured
        if not DEVREV_API_URL or 'api.devrev.ai' not in DEVREV_API_URL:
            self.api_url = "https://api.devrev.ai"
            logging.warning(f"Using default DevRev API URL: {self.api_url} (config value was incorrect or missing)")
        else:
            self.api_url = DEVREV_API_URL.rstrip("/")
            
        # Ensure API key is properly formatted for authentication
        api_key = DEVREV_API_KEY.strip() if DEVREV_API_KEY else ""
        if api_key.startswith("Bearer "):
            api_key = api_key[7:].strip()  # Remove 'Bearer ' prefix if present
            
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        # Add version header for all requests
        self.headers["X-DevRev-Version"] = "2022-10-20"
        
        # Debug the token for troubleshooting
        self._debug_token(api_key)
        
        # Check connection to DevRev API
        try:
            self.connection_error = None
            self._validate_connection()
            logging.info("DevRev connection successful")
        except Exception as e:
            self.connection_error = str(e)
            logging.error(f"DevRev connection error: {str(e)}")
            
        # Print API URL for debugging
        logging.info(f"Using DevRev API URL: {self.api_url}")
    
    def _debug_token(self, token: str) -> None:
        """Debug the token to check for common issues."""
        if not token:
            logging.error("DevRev API token is missing")
            return
            
        # Check basic JWT structure (3 parts separated by dots)
        parts = token.split('.')
        if len(parts) != 3:
            logging.error(f"DevRev API token has invalid JWT format. Expected 3 parts, got {len(parts)}")
        else:
            logging.info("DevRev API token has valid JWT format")
    
    def _validate_connection(self) -> bool:
        """Test the connection to DevRev API by making a simple request"""
        logging.info(f"Validating connection to DevRev API at {self.api_url}")
        
        # Check if API key is provided
        if not DEVREV_API_KEY:
            error_msg = "DevRev API token is missing - please set DEVREV_API_KEY in your environment variables or .env file"
            logging.error(error_msg)
            raise Exception(error_msg)
        
        # First try REST API endpoint - dev-users.self
        try:
            # Make direct request using requests library for validation
            url = f"{self.api_url}/dev-users.self"
            logging.info(f"Validating connection with REST API request to: {url}")
            
            response = requests.get(
                url,
                headers=self.headers,
                timeout=30
            )
            
            if response.status_code == 200:
                try:
                    result = response.json()
                    logging.info(f"DevRev REST API connection validated successfully: {json.dumps(result)[:100]}...")
                    self.api_type = "rest"
                    return True
                except ValueError as e:
                    logging.warning(f"Failed to parse REST API response as JSON: {str(e)}")
                    # Fall through to try GraphQL
            else:
                logging.warning(f"REST API validation failed with status code {response.status_code}")
                # Fall through to try GraphQL
        except Exception as e:
            logging.warning(f"REST API connection attempt failed: {str(e)}")
            # Fall through to try GraphQL
        
        # If REST failed, try GraphQL - this is a simpler query to test connectivity
        graphql_query = """
        query {
            __schema {
                queryType {
                    name
                }
            }
        }
        """
        
        try:
            logging.info("REST API validation failed. Attempting GraphQL connection...")
            # Try different possible GraphQL endpoints
            possible_endpoints = [
                "graphql", 
                "v1/graphql", 
                "api/graphql", 
                "graphql/v1",
                "api/v1/graphql",
                "v1/api/graphql",
                "public/graphql"
            ]
            
            for endpoint in possible_endpoints:
                try:
                    url = f"{self.api_url}/{endpoint}"
                    logging.info(f"Trying GraphQL endpoint: {url}")
                    
                    response = requests.post(
                        url=url,
                        headers=self.headers,
                        json={"query": graphql_query},
                        timeout=30
                    )
                    
                    if response.status_code == 200:
                        try:
                            result = response.json()
                            if "data" in result or "errors" in result:
                                logging.info(f"DevRev GraphQL API connection validated successfully via endpoint: {endpoint}")
                                self.graphql_endpoint = endpoint
                                self.api_type = "graphql"
                                return True
                        except ValueError:
                            pass  # Try next endpoint
                except Exception as e:
                    logging.warning(f"Error trying GraphQL endpoint {endpoint}: {str(e)}")
                    continue
                
            # If we got here, all GraphQL endpoints failed, but let's try one more approach
            # Some GraphQL APIs are embedded within existing REST endpoints
            try:
                # Try the base URL directly, this sometimes works
                url = f"{self.api_url}"
                logging.info(f"Trying base URL for GraphQL: {url}")
                
                response = requests.post(
                    url=url,
                    headers=self.headers,
                    json={"query": graphql_query},
                    timeout=30
                )
                
                if response.status_code == 200:
                    try:
                        result = response.json()
                        if "data" in result or "errors" in result:
                            logging.info("DevRev GraphQL API connection validated successfully via base URL")
                            self.graphql_endpoint = ""  # Empty string means base URL
                            self.api_type = "graphql"
                            return True
                    except ValueError:
                        pass
            except Exception as e:
                logging.warning(f"Error trying GraphQL at base URL: {str(e)}")
            
            # If we still haven't found a working endpoint, check if the base URL itself is a GraphQL endpoint
            error_msg = "Could not find a working DevRev API endpoint. Tried both REST and GraphQL endpoints."
            logging.error(error_msg)
            raise Exception(error_msg)
            
        except Exception as e:
            logging.error(f"Error validating DevRev API connection: {str(e)}")
            raise
    
    def _make_api_request(self, endpoint: str, method: str = "GET", params: Dict[str, Any] = None, data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Make a request to the DevRev REST API
        
        Args:
            endpoint: API endpoint path (without base URL)
            method: HTTP method (GET, POST, etc.)
            params: Query parameters
            data: Body data for POST/PUT requests
            
        Returns:
            Dictionary containing the API response
        """
        # DevRev API endpoints don't use v1/ prefix in the path - it's already part of the base URL
        # Remove v1/ prefix if present to avoid duplicate version path
        if endpoint.startswith('v1/'):
            endpoint = endpoint[3:]
        elif endpoint.startswith('/v1/'):
            endpoint = endpoint[4:]
            
        url = f"{self.api_url}/{endpoint}"
        logging.info(f"Making {method} request to {url}")
        
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self.headers,
                params=params,
                json=data,
                timeout=30
            )
            
            if response.status_code >= 400:
                logging.error(f"DevRev API error: {response.status_code} - {response.text}")
                return {"error": f"API error: {response.status_code} - {response.text}"}
            
            try:
                result = response.json()
                # Add debugging to see the response structure
                if isinstance(result, dict):
                    logging.info(f"API response keys: {list(result.keys())}")
                    if "works" in result:
                        works_data = result["works"]
                        logging.info(f"Works field type: {type(works_data)}, length: {len(works_data) if isinstance(works_data, list) else 'N/A'}")
                        if isinstance(works_data, list) and len(works_data) > 0:
                            logging.info(f"First work item type: {type(works_data[0])}")
                else:
                    logging.info(f"API response type: {type(result)}")
                return result
            except ValueError as e:
                logging.error(f"Failed to parse JSON response: {e}")
                logging.error(f"Response content: {response.text[:500]}...")
                return {"error": f"Failed to parse response as JSON: {str(e)}"}
                
        except Exception as e:
            logging.error(f"Error making API request: {str(e)}")
            return {"error": str(e)}
    
    def _make_paginated_api_request(self, endpoint: str, method: str = "GET", params: Dict[str, Any] = None, 
                                   data: Dict[str, Any] = None, max_results: int = 500) -> Dict[str, Any]:
        """Make a paginated request to the DevRev API
        
        Args:
            endpoint: API endpoint path (without base URL)
            method: HTTP method (GET, POST, etc.)
            params: Query parameters
            data: Body data for POST/PUT requests
            max_results: Maximum number of results to return
            
        Returns:
            Dictionary containing the combined API response with pagination
        """
        if params is None:
            params = {}
            
        # Set default limit if not specified
        if "limit" not in params:
            params["limit"] = 100
            
        combined_results = {}
        all_items = []
        total_fetched = 0
        page = 1
        has_more = True
        
        # Remove v1/ prefix if present to avoid duplicate version path
        if endpoint.startswith('v1/'):
            endpoint = endpoint[3:]
        elif endpoint.startswith('/v1/'):
            endpoint = endpoint[4:]
        
        while has_more and total_fetched < max_results:
            try:
                # Make API request for current page
                response = self._make_api_request(
                    endpoint=endpoint,
                    method=method,
                    params=params,
                    data=data
                )
                
                if "error" in response:
                    # If error in first page, return the error
                    if page == 1:
                        return response
                    # Otherwise, return what we have so far with the error noted
                    combined_results["pagination_error"] = response.get("error")
                    break
                
                # Log the response structure for debugging
                logging.info(f"API response keys: {list(response.keys())}")
                if "works" in response:
                    works_data = response["works"]
                    logging.info(f"Works field type: {type(works_data)}, length: {len(works_data) if isinstance(works_data, list) else 'N/A'}")
                    if isinstance(works_data, list) and len(works_data) > 0:
                        logging.info(f"First work item type: {type(works_data[0])}, keys: {list(works_data[0].keys()) if isinstance(works_data[0], dict) else 'N/A'}")
                
                # Extract items based on endpoint type
                items = []
                cursor = None
                
                # Handle different response structures based on endpoint type
                if endpoint == "works.list":
                    # For works.list, data should be in the 'works' array, not 'data'
                    items = response.get("works", [])
                    # Also check 'data' as fallback if 'works' is empty
                    if not items:
                        items = response.get("data", [])
                    # Pagination info might be in different structures
                    if "next_cursor" in response:
                        cursor = response.get("next_cursor")
                    elif "pagination_info" in response:
                        pagination_info = response.get("pagination_info", {})
                        cursor = pagination_info.get("next_cursor")
                        has_more = pagination_info.get("has_more", False)
                elif endpoint.startswith("works"):
                    items = response.get("works", [])
                    cursor = response.get("next_cursor")
                elif endpoint.startswith("parts"):
                    items = response.get("parts", [])
                    cursor = response.get("next_cursor")
                else:
                    # Default case - the response structure varies depending on endpoint
                    for key in response:
                        if isinstance(response[key], list) and len(response[key]) > 0:
                            items = response[key]
                            break
                    cursor = response.get("next_cursor")
                
                # Add items to combined results
                all_items.extend(items)
                total_fetched += len(items)
                
                # Check if we have more pages
                if cursor and len(items) > 0:
                    # Update params with cursor for next page
                    params["cursor"] = cursor
                    page += 1
                else:
                    has_more = False
                    
                # Log progress
                logging.info(f"Fetched page {page} with {len(items)} items. Total so far: {total_fetched}")
                
            except Exception as e:
                logging.error(f"Error in pagination request (page {page}): {str(e)}")
                combined_results["pagination_error"] = str(e)
                break
        
        # Construct combined result based on endpoint type
        if endpoint == "works.list":
            combined_results["works"] = all_items
        elif endpoint.startswith("works"):
            combined_results["works"] = all_items
        elif endpoint.startswith("parts"):
            combined_results["parts"] = all_items
        else:
            # For other endpoints, use the first list key from original response
            for key in response:
                if isinstance(response[key], list):
                    combined_results[key] = all_items
                    break
                    
        # Add pagination metadata
        combined_results["_pagination"] = {
            "total_fetched": total_fetched,
            "pages_fetched": page,
            "has_more": has_more
        }
        
        return combined_results
        
    def _map_jql_field(self, field: str, operator: str, value: str) -> Tuple[str, Any]:
        """Map a JQL field to the corresponding DevRev API parameter
        
        Args:
            field: JQL field name
            operator: JQL operator
            value: JQL value
            
        Returns:
            Tuple of (DevRev API parameter name, value)
        """
        # Define field mappings from JQL to DevRev API - using camelCase for DevRev GraphQL
        field_mappings = {
            "status": "stage",
            "stage": "stage",
            "assignee": "ownedBy",
            "owned_by": "ownedBy",
            "owner": "ownedBy",
            "reporter": "reportedBy",
            "reported_by": "reportedBy",
            "creator": "createdBy",
            "created_by": "createdBy",
            "priority": "priority",
            "type": "type",
            "issuetype": "type",
            "created": "createdDate",
            "updated": "modifiedDate",
            "modified": "modifiedDate",
            "resolved": "closedDate",
            "closed": "closedDate",
            "project": "partId",
            "part": "partId",
            "label": "tags",
            "tag": "tags",
            "tags": "tags",
        }
        
        # Map the field name
        api_field = field_mappings.get(field.lower(), field.lower())
        
        # Handle special cases based on operator
        if api_field in ["createdDate", "modifiedDate", "closedDate"]:
            if operator in [">", ">="]:
                return f"{api_field}Start", value
            elif operator in ["<", "<="]:
                return f"{api_field}End", value
        
        # Convert value based on field type
        if api_field == "priority" and isinstance(value, str):
            # Map priority values (e.g., High -> P0)
            priority_map = {
                "highest": "P0",
                "high": "P1",
                "medium": "P2",
                "low": "P3",
                "lowest": "P4"
            }
            value = priority_map.get(value.lower(), value)
            
        # Return the mapped field and value
        return api_field, value
        
    def _jql_to_query_params(self, jql: str) -> Dict[str, Any]:
        """Convert JQL query to DevRev API query parameters
        
        Args:
            jql: JQL query string
            
        Returns:
            Dictionary containing query parameters for DevRev API
        """
        if not jql:
            return {}
            
        logging.info(f"Converting JQL to DevRev parameters: {jql}")
        
        # Parse JQL components
        jql_components = self._parse_jql_query(jql)
        
        # Build query parameters
        query_params = {}
        devrev_filters = {}
        
        # Process project filter - this is the most important part for DevRev
        if jql_components.get("projects"):
            query_params["partId"] = ",".join(jql_components["projects"])
            devrev_filters["partIds"] = jql_components["projects"]
        
        # Process date conditions first (they often override other filters)
        for date_condition in jql_components.get("date_conditions", []):
            field = date_condition["field"].lower()
            operator = date_condition["operator"]
            value = date_condition["value"]
            
            try:
                if field in ["created", "updated", "resolved"]:
                    if value.endswith('d'):  # Relative days like -30d
                        days = int(value.rstrip('d'))
                        if days < 0:  # Past days
                            if field == "updated":
                                query_params["updatedSinceDays"] = abs(days)
                            elif field == "created":
                                query_params["createdSinceDays"] = abs(days)
                    elif value.startswith(('20', '19')):  # Absolute date like 2024-01-01
                        if operator in ['>=', '>']:
                            devrev_filters[f"{field}DateStart"] = value
                        elif operator in ['<=', '<']:
                            devrev_filters[f"{field}DateEnd"] = value
                        elif operator == '=':
                            devrev_filters[f"{field}Date"] = value
            except (ValueError, TypeError) as e:
                logging.warning(f"Error processing date condition {date_condition}: {str(e)}")
        
        # Process standard and custom field conditions
        for condition in jql_components.get("conditions", []):
            field = condition["field"].lower()
            operator = condition["operator"]
            value = condition["value"]
            field_type = condition.get("field_type", "general")
            
            logging.info(f"Processing condition: {field} {operator} {value} (type: {field_type})")
            
            # Handle custom fields by converting them to appropriate DevRev filters
            if field_type == "custom" or field.startswith('#') or field.startswith('customfield_'):
                logging.info(f"Processing custom field '{field}' for DevRev query conversion")
                
                # For custom fields, we have several options:
                # 1. Convert to tags if DevRev supports tag-based filtering
                # 2. Store for client-side filtering
                # 3. Use DevRev's custom field API if available
                
                if operator == "=":
                    # Convert custom field to a tag-based filter for DevRev
                    tag_name = self._convert_custom_field_to_tag(field, value)
                    if tag_name:
                        if "tags" not in devrev_filters:
                            devrev_filters["tags"] = []
                        devrev_filters["tags"].append(tag_name)
                        
                        # Also store for client-side filtering as backup
                        if "custom_fields_filter" not in query_params:
                            query_params["custom_fields_filter"] = {}
                        query_params["custom_fields_filter"][field] = value
                        
                elif operator in ["!=", "!~"]:
                    # Store for client-side filtering (DevRev API may not support negation)
                    if "custom_fields_exclude" not in query_params:
                        query_params["custom_fields_exclude"] = {}
                    query_params["custom_fields_exclude"][field] = value
                    
                elif operator.upper() == "IN":
                    # Handle multi-value custom fields
                    values = [v.strip().strip('"\'') for v in value.split(',')]
                    if "custom_fields_filter" not in query_params:
                        query_params["custom_fields_filter"] = {}
                    query_params["custom_fields_filter"][field] = values
                    
                continue
                
            # Map standard JQL fields to DevRev API parameters
            api_field, api_value = self._map_jql_field(field, operator, value)
            
            if api_field and api_value is not None:
                # Handle different operators
                if operator == "=":
                    devrev_filters[api_field] = api_value
                elif operator == "!=":
                    # Store for client-side filtering
                    if "exclude_filters" not in query_params:
                        query_params["exclude_filters"] = {}
                    query_params["exclude_filters"][api_field] = api_value
                elif operator.upper() == "IN":
                    # Handle multi-value fields
                    if isinstance(api_value, str) and ',' in api_value:
                        values = [v.strip().strip('"\'') for v in api_value.split(',')]
                        devrev_filters[api_field] = values
                    else:
                        devrev_filters[api_field] = api_value
                elif operator in ["~", "LIKE"]:
                    # Store for client-side text matching
                    if "text_filters" not in query_params:
                        query_params["text_filters"] = {}
                    query_params["text_filters"][api_field] = api_value
                else:
                    # Default to exact match
                    devrev_filters[api_field] = api_value
        
        # Handle logical operators for complex queries
        logical_ops = jql_components.get("logical_operators", [])
        if logical_ops:
            query_params["logical_operators"] = logical_ops
            # Note: DevRev API may not support complex logical operations
            # These will need to be handled client-side
        
        # Handle sorting
        sort_conditions = jql_components.get("sort_conditions", [])
        if sort_conditions:
            # Take the first sort condition (DevRev API may only support single sort)
            sort_condition = sort_conditions[0]
            sort_field = sort_condition["field"].lower()
            sort_direction = sort_condition["direction"].lower()
            
            # Map JQL sort fields to DevRev sort fields
            sort_mapping = {
                "created": "created_date",
                "updated": "modified_date",
                "resolved": "resolved_date",
                "priority": "priority",
                "status": "stage"
            }
            
            devrev_sort_field = sort_mapping.get(sort_field, sort_field)
            query_params["sort_by"] = devrev_sort_field
            query_params["sort_order"] = sort_direction
        
        # Merge devrev_filters into query_params
        query_params.update(devrev_filters)
        
        # Add debug log for resulting DevRev query parameters
        logging.info(f"Converted JQL to DevRev parameters: {json.dumps(query_params, indent=2)}")
            
        return query_params
    
    def _parse_jql_query(self, jql: str) -> Dict[str, Any]:
        """Parse a JQL query to extract components like fields, projects, and conditions.
        
        Args:
            jql: JQL query string
            
        Returns:
            Dictionary of query components including fields, projects, etc.
        """
        if not jql:
            return {}
            
        result = {
            "fields": [],
            "projects": [],
            "custom_fields": [],
            "conditions": [],
            "logical_operators": [],
            "date_conditions": [],
            "sort_conditions": []
        }
        
        # Normalize the JQL query
        jql = jql.strip()
        logging.info(f"Parsing JQL query: {jql}")
        
        # Extract project information with comprehensive patterns
        project_patterns = [
            r'\bproject\s*=\s*["\']?(\w+)["\']?',                    # project = "PROJ" or project = PROJ
            r'\bproject\s*!=\s*["\']?(\w+)["\']?',                   # project != "PROJ"
            r'\bproject\s+in\s+\(\s*([^)]+)\s*\)',                   # project in ("PROJ1", "PROJ2")
            r'\bproject\s+not\s+in\s+\(\s*([^)]+)\s*\)',             # project not in ("PROJ1", "PROJ2")
            r'\bproject\s*~\s*["\']?([^"\']+)["\']?',                # project ~ "PROJ*"
            r'\bproject\s*!~\s*["\']?([^"\']+)["\']?',               # project !~ "PROJ*"
        ]
        
        for pattern in project_patterns:
            projects_found = re.findall(pattern, jql, re.IGNORECASE)
            if projects_found:
                for project_match in projects_found:
                    if ',' in project_match:
                        # Handle comma-separated list from "in" clause
                        parts = [p.strip().strip('"\'') for p in project_match.split(',') if p.strip()]
                        result["projects"].extend(parts)
                    else:
                        result["projects"].append(project_match.strip().strip('"\''))
        
        # Remove duplicates while preserving order
        if result["projects"]:
            seen = set()
            result["projects"] = [x for x in result["projects"] if not (x in seen or seen.add(x))]
            logging.info(f"Extracted project IDs from JQL: {result['projects']}")
        
        # Extract custom fields with quotes and hash prefixes
        custom_field_patterns = [
            r'"([^"]+)"\s*([=!~<>]+|IN|NOT IN)\s*([^,\s]+(?:\s+[^,\s]+)*)',  # "Custom Field" = value
            r'#([^#\s]+)\s*([=!~<>]+|IN|NOT IN)\s*([^,\s]+(?:\s+[^,\s]+)*)', # #CustomField = value
            r'customfield_(\d+)\s*([=!~<>]+|IN|NOT IN)\s*([^,\s]+(?:\s+[^,\s]+)*)', # customfield_12345 = value
        ]
        
        for pattern in custom_field_patterns:
            matches = re.findall(pattern, jql, re.IGNORECASE)
            for field_name, operator, value in matches:
                # Clean up field name and value
                field_name = field_name.strip()
                value = value.strip().strip('"\'()')
                
                result["custom_fields"].append(field_name)
                result["fields"].append(field_name)
                result["conditions"].append({
                    "field": field_name,
                    "operator": operator.strip(),
                    "value": value,
                    "field_type": "custom"
                })
        
        # Extract standard field conditions with comprehensive operator support
        standard_fields = ["status", "stage", "assignee", "reporter", "priority", "type", "created", "updated", "resolved", "labels", "component", "version"]
        
        for field in standard_fields:
            # Pattern to match field with various operators
            field_pattern = rf'\b{field}\s*([=!~<>]+|IN|NOT IN|IS|IS NOT)\s*([^,\s\)]+(?:\s+[^,\s\)]+)*)'
            matches = re.findall(field_pattern, jql, re.IGNORECASE)
            
            for operator, value in matches:
                value = value.strip().strip('"\'()')
                result["fields"].append(field)
                result["conditions"].append({
                    "field": field,
                    "operator": operator.strip(),
                    "value": value,
                    "field_type": "standard"
                })
        
        # Extract date conditions with relative date support
        date_patterns = [
            r'(created|updated|resolved)\s*([><=!]+)\s*(["\']?[^"\']+["\']?)',  # created >= "2024-01-01"
            r'(created|updated|resolved)\s*([><=!]+)\s*(-?\d+[dwmy])',          # created >= -30d
            r'(created|updated|resolved)\s*(IN|NOT IN)\s*\(([^)]+)\)',          # created in (dateRange)
        ]
        
        for pattern in date_patterns:
            matches = re.findall(pattern, jql, re.IGNORECASE)
            for field, operator, value in matches:
                value = value.strip().strip('"\'')
                result["date_conditions"].append({
                    "field": field,
                    "operator": operator.strip(),
                    "value": value
                })
        
        # Extract logical operators (AND, OR, NOT)
        logical_pattern = r'\b(AND|OR|NOT)\b'
        logical_ops = re.findall(logical_pattern, jql, re.IGNORECASE)
        result["logical_operators"] = [op.upper() for op in logical_ops]
        
        # Extract ORDER BY clauses
        order_pattern = r'ORDER\s+BY\s+([^,\s]+)(?:\s+(ASC|DESC))?'
        order_matches = re.findall(order_pattern, jql, re.IGNORECASE)
        for field, direction in order_matches:
            result["sort_conditions"].append({
                "field": field.strip(),
                "direction": direction.upper() if direction else "ASC"
            })
        
        # Extract general conditions with comprehensive operator support
        general_condition_pattern = r'(\w+|"[^"]+"|#[^#\s]+)\s*([=!~<>]+|IN|NOT IN|IS|IS NOT)\s*("[^"]*"|\'[^\']*\'|\([^)]*\)|\w+)'
        conditions = re.findall(general_condition_pattern, jql, re.IGNORECASE)
        
        for field, operator, value in conditions:
            # Skip if already processed
            field_clean = field.strip('"#')
            if any(cond["field"] == field_clean for cond in result["conditions"]):
                continue
                
            # Clean up the value
            value = value.strip('"\'()')
            result["conditions"].append({
                "field": field_clean,
                "operator": operator.strip(),
                "value": value,
                "field_type": "general"
            })
        
        # Log the detailed structure for debugging
        logging.info(f"Parsed JQL structure: {json.dumps(result, indent=2)}")
                
        return result
    
    def extract_filters_from_prompt(self, prompt: str) -> Dict[str, Any]:
        """Extract filtering parameters from a natural language prompt
        
        Args:
            prompt: Natural language prompt/query
            
        Returns:
            Dictionary of extracted filter parameters
        """
        filters = {}
        
        # Extract priority filter (e.g., "high priority", "priority: high")
        priority_pattern = r'(?:high|medium|low|highest|lowest)\s+priority|priority\s*[:=]\s*(high|medium|low|highest|lowest)'
        priority_match = re.search(priority_pattern, prompt, re.IGNORECASE)
        if priority_match:
            priority_value = priority_match.group(1) if priority_match.group(1) else "high"
            filters["priority"] = priority_value
            
        # Extract stage/status filter
        status_pattern = r'status\s*[:=]\s*"?([^",]+)"?|stage\s*[:=]\s*"?([^",]+)"?'
        status_match = re.search(status_pattern, prompt, re.IGNORECASE)
        if status_match:
            status_value = status_match.group(1) or status_match.group(2)
            filters["stage"] = status_value
            
        # Extract owner/assignee filter 
        owner_pattern = r'(?:assign(?:ed)?\s+to|owner\s*[:=])\s*"?([^",]+)"?'
        owner_match = re.search(owner_pattern, prompt, re.IGNORECASE)
        if owner_match:
            filters["ownedBy"] = owner_match.group(1)
            
        # Extract type filter
        type_pattern = r'type\s*[:=]\s*"?([^",]+)"?'
        type_match = re.search(type_pattern, prompt, re.IGNORECASE)
        if type_match:
            filters["type"] = type_match.group(1)
            
        # Extract time-based filters
        time_pattern = r'(?:in|from|within) (?:the )?(?:last|past) (\d+) (days?|weeks?|months?)'
        time_match = re.search(time_pattern, prompt, re.IGNORECASE)
        if time_match:
            amount = int(time_match.group(1))
            unit = time_match.group(2).lower()
            
            # Convert to days
            if unit.startswith('week'):
                days = amount * 7
            elif unit.startswith('month'):
                days = amount * 30
            else:
                days = amount
                
            filters["updatedSinceDays"] = days
            
        return filters
    
    # GraphQL query construction methods
    def build_filter_block(self, filters: Dict[str, Any]) -> str:
        """Build a GraphQL filter block from filter parameters
        
        Args:
            filters: Dictionary of filter parameters
            
        Returns:
            GraphQL filter block string
        """
        if not filters:
            return "{}"
            
        filter_parts = []
        
        # Map filter keys to GraphQL filter fields - using camelCase for DevRev GraphQL
        for key, value in filters.items():
            if key == "part_id" and value:
                if isinstance(value, list):
                    part_ids = [f'"{part}"' for part in value]
                    filter_parts.append(f'partIds: [{", ".join(part_ids)}]')
                else:
                    parts = [f'"{part.strip()}"' for part in value.split(",")]
                    filter_parts.append(f'partIds: [{", ".join(parts)}]')
            elif key == "stage" and value:
                filter_parts.append(f'stageName: "{value}"')
            elif key == "owned_by" and value:
                filter_parts.append(f'ownedBy: "{value}"')
            elif key == "reported_by" and value:
                filter_parts.append(f'reportedBy: "{value}"')
            elif key == "priority" and value:
                filter_parts.append(f'priority: "{value}"')
            elif key == "type" and value:
                filter_parts.append(f'type: "{value}"')
            elif key == "tags" and value:
                if isinstance(value, list):
                    tag_values = [f'"{tag}"' for tag in value]
                    filter_parts.append(f'tags: [{", ".join(tag_values)}]')
                else:
                    tags = [f'"{tag.strip()}"' for tag in value.split(",")]
                    filter_parts.append(f'tags: [{", ".join(tags)}]')
            elif key == "created_date_start" and value:
                filter_parts.append(f'createdDateStart: "{value}"')
            elif key == "created_date_end" and value:
                filter_parts.append(f'createdDateEnd: "{value}"')
            elif key == "modified_date_start" and value:
                filter_parts.append(f'modifiedDateStart: "{value}"')
            elif key == "modified_date_end" and value:
                filter_parts.append(f'modifiedDateEnd: "{value}"')
            elif key == "updated_since_days" and value:
                # Calculate date from days
                filter_parts.append(f'updatedSinceDays: {value}')
        
        return "{ " + ", ".join(filter_parts) + " }"
    
    def build_sort_block(self, sort_by: str = "updated", order: str = "desc") -> str:
        """Build a GraphQL sort block
        
        Args:
            sort_by: Field to sort by
            order: Sort order (asc or desc)
            
        Returns:
            GraphQL sort block string
        """
        # Map sort field to GraphQL sort field - using camelCase for DevRev GraphQL
        sort_field_map = {
            "updated": "modifiedDate",
            "modified": "modifiedDate",
            "created": "createdDate",
            "priority": "priority",
            "status": "stage"
        }
        
        sort_field = sort_field_map.get(sort_by.lower(), "modifiedDate")
        
        return f'{{ field: {sort_field}, order: {order.upper()} }}'
    
    def build_devrev_query(self, filter_block: str, sort_block: str, limit: int = 100) -> str:
        """Build a complete DevRev GraphQL query
        
        Args:
            filter_block: GraphQL filter block
            sort_block: GraphQL sort block
            limit: Result limit
            
        Returns:
            Complete GraphQL query string
        """
        # Check if we're using REST API or GraphQL based on connection validation
        if hasattr(self, 'api_type') and self.api_type == "rest":
            # For REST API, just return a placeholder that we'll convert to params later
            logging.info("Using REST API mode, filter will be converted to query parameters")
            return f"rest:filter={filter_block}&sort={sort_block}&limit={limit}"
        
        # For different possible GraphQL schemas, try both standard patterns
        # Pattern 1: Standard Works query with edges/nodes
        pattern1 = f"""
        query GetWorks {{
            works(
                filter: {filter_block},
                orderBy: [{sort_block}],
                first: {limit}
            ) {{
                edges {{
                    node {{
                        id
                        displayId
                        title
                        body
                        type {{ name displayName }}
                        stage {{ name displayName }}
                        priority {{ name displayName }}
                        ownedBy {{ id displayName email }}
                        reportedBy {{ id displayName email }}
                        createdBy {{ id displayName email }}
                        createdDate
                        modifiedDate
                        tags {{ name }}
                        parts {{ id name displayId }}
                    }}
                }}
                pageInfo {{
                    hasNextPage
                    endCursor
                }}
                totalCount
            }}
        }}
        """
        
        # Pattern 2: Alternative scheme with a direct list of works (not edges/nodes)
        pattern2 = f"""
        query GetWorks {{
            works(
                filter: {filter_block},
                orderBy: [{sort_block}],
                limit: {limit}
            ) {{
                items {{
                    id
                    displayId
                    title
                    body
                    type {{ name displayName }}
                    stage {{ name displayName }}
                    priority {{ name displayName }}
                    ownedBy {{ id displayName email }}
                    reportedBy {{ id displayName email }}
                    createdBy {{ id displayName email }}
                    createdDate
                    modifiedDate
                    tags {{ name }}
                    parts {{ id name displayId }}
                }}
                pageInfo {{
                    hasNextPage
                    nextCursor
                }}
                totalCount
            }}
        }}
        """
        
        # By default, use the first pattern which is more common in GraphQL APIs
        return pattern1
    
    async def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate DevRev-specific inputs"""
        try:
            # Check if connection to API is available
            if hasattr(self, 'connection_error') and self.connection_error:
                logging.error(f"Cannot validate inputs due to connection error: {self.connection_error}")
                return False
                
            # Check if we have a direct GraphQL query
            if 'project_key' in inputs:
                keys = inputs['project_key']
                if isinstance(keys, str) and keys.strip().startswith("query {"):
                    logging.info(f"Detected direct GraphQL query in project_key field")
                    
                    # DevRev doesn't support GraphQL, but we'll extract project info to validate
                    query = keys.strip()
                    
                    # Extract project key from GraphQL query
                    project_match = re.search(r'project\s*:\s*{\s*key\s*:\s*"([^"]+)"', query)
                    if project_match:
                        project_key = project_match.group(1)
                        logging.info(f"Extracted project key from GraphQL query: {project_key}")
                        
                        # Validate the extracted project key using REST API
                        endpoint = "parts.list"
                        response = self._make_api_request(
                            endpoint=endpoint,
                            method="GET"
                        )
                        
                        if "error" in response:
                            logging.error(f"Error validating project key: {response['error']}")
                            return False
                        
                        parts = response.get("parts", [])
                        if not parts and "data" in response:
                            parts = response.get("data", [])
                        
                        # Look for matching part by name or display_id
                        for part in parts:
                            if (part.get("name") == project_key or 
                                part.get("display_id") == project_key):
                                logging.info(f"Found matching part for project key {project_key}")
                                return True
                        
                        # If we didn't find a direct match, log warning but return true anyway
                        # since the user's query might be valid in their context
                        logging.warning(f"No exact match found for project key {project_key}, but allowing query")
                        return True
                        
                    else:
                        # If we can't extract a project key, try a basic REST API request
                        # to validate general DevRev connectivity
                        endpoint = "parts.list"
                        response = self._make_api_request(
                            endpoint=endpoint,
                            method="GET",
                            params={"limit": 1}
                        )
                        
                        if "error" in response:
                            logging.error(f"Error validating with REST API: {response['error']}")
                            return False
                        
                        logging.info("Basic REST API connectivity validated")
                        return True
                        
                # First check if the input appears to be a JQL query directly in project_key field
                elif isinstance(keys, str) and any(keyword in keys.upper() for keyword in [' AND ', ' OR ', '=', '<', '>', 'ORDER BY', 'PROJECT =']):
                    logging.info(f"Detected JQL query in project_key field: {keys}")
                    # Move it to jql_query field and validate as JQL
                    if 'jql_query' not in inputs or not inputs['jql_query'].strip():
                        inputs['jql_query'] = keys
                        
            # Check if JQL query is provided - we'll extract the project/part ID from it
            if 'jql_query' in inputs and inputs['jql_query'].strip():
                # Check if this is actually a GraphQL query
                if inputs['jql_query'].strip().startswith("query {"):
                    logging.info(f"Detected direct GraphQL query in jql_query field")
                    
                    # Similar handling as above for GraphQL in project_key
                    query = inputs['jql_query'].strip()
                    
                    # Extract project key from GraphQL query
                    project_match = re.search(r'project\s*:\s*{\s*key\s*:\s*"([^"]+)"', query)
                    if project_match:
                        project_key = project_match.group(1)
                        logging.info(f"Extracted project key from GraphQL query: {project_key}")
                        
                        # Make a simple REST API request to validate connectivity
                        endpoint = "parts.list"
                        response = self._make_api_request(
                            endpoint=endpoint,
                            method="GET"
                        )
                        
                        if "error" in response:
                            logging.error(f"Error validating with REST API: {response['error']}")
                            return False
                        
                        return True
                    else:
                        # If we can't extract a project key, try a basic REST API request
                        endpoint = "parts.list"
                        response = self._make_api_request(
                            endpoint=endpoint,
                            method="GET",
                            params={"limit": 1}
                        )
                        
                        if "error" in response:
                            logging.error(f"Error validating with REST API: {response['error']}")
                            return False
                        
                        logging.info("Basic REST API connectivity validated")
                        return True
                        
                try:
                    jql = inputs['jql_query'].strip()
                    logging.info(f"Extracting part_id from JQL query: {jql}")
                    
                    # Parse JQL to extract projects
                    jql_structure = self._parse_jql_query(jql)
                    
                    # Check if we have a project - this is the minimum requirement
                    if not jql_structure.get('projects'):
                        logging.error("JQL query doesn't contain a valid project identifier")
                        return False
                    
                    # Get the first project ID - this is what we'll use for validation
                    part_id = jql_structure.get('projects')[0]
                    
                    logging.info(f"Using part_id '{part_id}' from JQL query for validation")
                    
                    # Validate with a simple REST API request
                    endpoint = "parts.list"
                    response = self._make_api_request(
                        endpoint=endpoint,
                        method="GET",
                        params={"limit": 1}
                    )
                    
                    if "error" in response:
                        logging.error(f"Error validating with REST API: {response['error']}")
                        return False
                    
                    # If we got here, basic REST connectivity is working
                    logging.info("Basic REST API connectivity validated")
                    return True
                        
                except Exception as e:
                    logging.error(f"Error validating with REST API: {str(e)}")
                    return False
            
            # If no JQL, fall back to project key validation
            # Collect project IDs from multiple possible field names
            projects = []
            
            # Check for project_key field first (newer format)
            if 'project_key' in inputs:
                keys = inputs['project_key']
                if isinstance(keys, list):
                    # Process each key in the list
                    for key in keys:
                        if isinstance(key, str) and ',' in key:
                            # Split comma-separated keys in a string
                            parts = [k.strip() for k in key.split(',') if k.strip()]
                            projects.extend(parts)
                        else:
                            projects.append(key)
                elif isinstance(keys, str):
                    # Split comma-separated string
                    projects.extend([k.strip() for k in keys.split(',') if k.strip()])
                else:
                    projects.append(keys)
            
            # Filter out empty values
            projects = [p for p in projects if p]
            
            if not projects:
                logging.error("No DevRev project specified and no JQL query provided")
                return False
            
            # Validate with a simple REST API request to check connectivity
            endpoint = "parts.list"
            response = self._make_api_request(
                endpoint=endpoint,
                method="GET",
                params={"limit": 1}
            )
            
            if "error" in response:
                logging.error(f"Error validating with REST API: {response['error']}")
                return False
            
            # If we got here, basic REST connectivity is working
            logging.info("Basic REST API connectivity validated")
            return True
                
        except Exception as e:
            logging.error(f"Error validating DevRev project(s): {str(e)}")
            return False
            
    async def fetch_data(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch data from DevRev using REST or GraphQL API.
        
        Args:
            params: Dictionary of parameters including:
                - project_key: DevRev project/part ID or name
                - jql_query: JQL query to extract project ID from
                - days: Optional days to look back (int)
                
        Returns:
            Dictionary of DevRev data organized by project
        """
        try:
            # Check for connection errors
            if hasattr(self, 'connection_error') and self.connection_error:
                return {"error": f"DevRev connection error: {self.connection_error}"}
            
            # Detect direct GraphQL queries in any parameter
            direct_query = None
            query_source = None
            
            # First check for a GraphQL query in project_key
            if 'project_key' in params:
                project_key_param = params['project_key']
                
                # Handle direct GraphQL queries in project_key field
                if isinstance(project_key_param, str) and self._is_graphql_query(project_key_param):
                    direct_query = project_key_param.strip()
                    query_source = "project_key"
                # Handle list where items might need to be combined into a query
                elif isinstance(project_key_param, list):
                    combined = " ".join([str(item) for item in project_key_param])
                    if self._is_graphql_query(combined):
                        direct_query = combined.strip()
                        query_source = "project_key_list"
            
            # Check for GraphQL query in jql_query
            if not direct_query and 'jql_query' in params and isinstance(params['jql_query'], str):
                if self._is_graphql_query(params['jql_query']):
                    direct_query = params['jql_query'].strip()
                    query_source = "jql_query"
                
            # Check for GraphQL query in query field
            if not direct_query and 'query' in params and isinstance(params['query'], str):
                if self._is_graphql_query(params['query']):
                    direct_query = params['query'].strip()
                    query_source = "query"
            
            # Process direct GraphQL query if found
            if direct_query:
                logging.info(f"Processing direct GraphQL query from {query_source}")
                # Process the GraphQL query by translating it to REST API calls
                result = self.process_direct_graphql_query(direct_query)
                if "error" not in result:
                    return {"GRAPHQL": result}  # Use a special key to indicate this is from a GraphQL query
                else:
                    return {"error": result["error"]}
                
            # Extract project IDs from params
            part_ids = self._extract_part_ids_from_params(params)
            
            if not part_ids:
                return {"error": "No DevRev project/part ID provided"}
            
            # Check if we have a JQL query to process
            jql_query = params.get('jql_query', '')
            jql_filters = {}
            client_side_filters = {}
            
            if jql_query and not self._is_graphql_query(jql_query):
                logging.info(f"Processing JQL query: {jql_query}")
                try:
                    # Convert JQL to DevRev API parameters
                    jql_filters = self._jql_to_query_params(jql_query)
                    
                    # Extract client-side filters that DevRev API cannot handle
                    client_side_filters = {
                        "custom_fields_filter": jql_filters.pop("custom_fields_filter", {}),
                        "custom_fields_exclude": jql_filters.pop("custom_fields_exclude", {}),
                        "exclude_filters": jql_filters.pop("exclude_filters", {}),
                        "text_filters": jql_filters.pop("text_filters", {}),
                        "logical_operators": jql_filters.pop("logical_operators", [])
                    }
                    
                    logging.info(f"JQL filters for API: {json.dumps(jql_filters, indent=2)}")
                    logging.info(f"Client-side filters: {json.dumps(client_side_filters, indent=2)}")
                    
                except Exception as e:
                    logging.error(f"Error processing JQL query '{jql_query}': {str(e)}")
                    return {"error": f"Invalid JQL query: {str(e)}"}
            
            # Extract additional filters from the prompt if available
            query = params.get('query', '')
            prompt_filters = {}
            if query and not jql_query:  # Only use prompt filters if no JQL query
                prompt_filters = self.extract_filters_from_prompt(query)
                logging.info(f"Extracted filters from prompt: {prompt_filters}")
            
            # Prepare filters for GraphQL/REST - merge JQL filters with other filters
            gql_filters = {}
            gql_filters.update(jql_filters)
            
            # Get days parameter and convert to filter (if not already set by JQL)
            days = None
            if 'days' in params and 'updatedSinceDays' not in gql_filters:
                try:
                    days = int(params['days'])
                    gql_filters["updatedSinceDays"] = days
                except (ValueError, TypeError):
                    pass
            
            # Add any filters extracted from the prompt (if no JQL)
            if not jql_query:
                for key, value in prompt_filters.items():
                    if key not in gql_filters:
                        gql_filters[key] = value
            
            # Apply metadata-based filtering if available (and not overridden by JQL)
            if 'metadata' in params and isinstance(params['metadata'], dict):
                metadata = params['metadata']
                if 'owner' in metadata and 'ownedBy' not in gql_filters:
                    gql_filters['ownedBy'] = metadata['owner']
                if 'priority' in metadata and 'priority' not in gql_filters:
                    gql_filters['priority'] = metadata['priority']
                if 'status' in metadata and 'stage' not in gql_filters:
                    gql_filters['stage'] = metadata['status']
                if 'type' in metadata and 'type' not in gql_filters:
                    gql_filters['type'] = metadata['type']
            
            # Container for results
            results = {}
            project_relevance = {}  # Track relevance scores per project
            
            # Determine if we're using REST or GraphQL API
            api_type = getattr(self, 'api_type', 'graphql')  # Default to GraphQL if not set
            
            # Process each part ID 
            for part_id in part_ids:
                try:
                    logging.info(f"Processing DevRev part: {part_id}")
                    
                    # Create filter with this part_id
                    current_filters = gql_filters.copy()
                    current_filters["partId"] = part_id
                    
                    # Build the query based on API type
                    if api_type == "rest":
                        # For REST API, use the works.list endpoint
                        works = await self._fetch_works_via_rest(part_id, current_filters)
                    else:
                        # For GraphQL, build and execute a GraphQL query
                        filter_block = self.build_filter_block(current_filters)
                        sort_block = self.build_sort_block()
                        graphql_query = self.build_devrev_query(filter_block, sort_block, limit=100)
                        works = await self._fetch_works_via_graphql(part_id, graphql_query)
                    
                    # If error occurred
                    if isinstance(works, dict) and "error" in works:
                        error_msg = works.get("error", "Unknown API error")
                        logging.error(f"Error fetching works for part {part_id}: {error_msg}")
                        results[part_id] = {
                            "error": f"Error fetching project data: {error_msg}",
                            "relevance": 0.1  # Very low relevance for error cases
                        }
                        continue
                    
                    # Get total count
                    total_count = works.get("total_count", len(works.get("items", [])))
                    work_items = works.get("items", [])
                    
                    # Apply client-side filtering for complex JQL queries
                    if client_side_filters and work_items:
                        logging.info(f"Applying client-side filtering to {len(work_items)} work items")
                        work_items = self._apply_client_side_filters(work_items, client_side_filters)
                        logging.info(f"After client-side filtering: {len(work_items)} work items remain")
                    
                    # Add debugging to see what we got
                    logging.info(f"Works object keys: {list(works.keys()) if isinstance(works, dict) else 'Not a dict'}")
                    logging.info(f"Work items type: {type(work_items)}, length: {len(work_items) if isinstance(work_items, list) else 'N/A'}")
                    if isinstance(work_items, list) and len(work_items) > 0:
                        logging.info(f"First work item type: {type(work_items[0])}")
                        if isinstance(work_items[0], dict):
                            logging.info(f"First work item keys: {list(work_items[0].keys())}")
                        else:
                            logging.info(f"First work item value: {str(work_items[0])[:100]}")
                    
                    # Log total number of results
                    logging.info(f"Query for part {part_id} returned {len(work_items)} works")
                    
                    # Calculate basic project relevance based on number of works and matches to query
                    query_matches = 0
                    if query:
                        for work in work_items:
                            try:
                                # Add type checking to handle string objects
                                if not isinstance(work, dict):
                                    logging.warning(f"Work item in query matching is not a dictionary, type: {type(work)}")
                                    continue
                                
                                title = work.get('title', '')
                                body = work.get('body', '')
                                work_text = f"{title} {body}".lower()
                                if query.lower() in work_text:
                                    query_matches += 1
                            except Exception as e:
                                logging.warning(f"Error processing work for query matching: {str(e)}")
                    
                    # Calculate relevance score (0.3-1.0 range)
                    if not work_items:
                        relevance = 0.3  # Base relevance for empty projects
                    else:
                        # Start with a base relevance of 0.5
                        relevance = 0.5
                        
                        # If there's a query, adjust relevance based on matches
                        if query:
                            match_percentage = query_matches / len(work_items) if len(work_items) > 0 else 0
                            relevance = 0.5 + (match_percentage * 0.5)  # Scale from 0.5 to 1.0
                    
                    # Store project relevance for later use
                    project_relevance[part_id] = relevance
                    
                    # Process and store the result with metadata
                    results[part_id] = {
                        "issues": work_items,  # Using DevRev works as issues for compatibility
                        "total": len(work_items),
                        "relevance": relevance,
                        "metadata": {
                            "query_matches": query_matches,
                            "issues_count": len(work_items),
                            "total_fetched": len(work_items),
                            "total_count": total_count,
                            "api_type": api_type,
                            "filters": current_filters,
                            "processing_time": str(datetime.now())
                        }
                    }
                except Exception as e:
                    logging.error(f"Error processing DevRev part {part_id}: {str(e)}")
                    results[part_id] = {
                        "error": f"Error fetching project data: {str(e)}",
                        "relevance": 0.2  # Low relevance for error cases
                    }
            
            # Normalize relevance scores across projects
            if len(part_ids) > 1:
                max_relevance = max(project_relevance.values()) if project_relevance else 0
                if max_relevance > 0:
                    for part_id in project_relevance:
                        normalized_relevance = (project_relevance[part_id] / max_relevance) * 0.9 + 0.1
                        if part_id in results and isinstance(results[part_id], dict):
                            results[part_id]['relevance'] = normalized_relevance
            
            # Add global metadata
            results['_metadata'] = {
                "query_type": api_type,
                "projects_processed": len(part_ids),
                "projects_succeeded": sum(1 for p in part_ids if p in results and "error" not in results[p]),
                "projects_failed": sum(1 for p in part_ids if p in results and "error" in results[p]),
                "total_issues": sum(results[p].get("total", 0) for p in part_ids if p in results and isinstance(results[p], dict) and "total" in results[p]),
                "query_matches": sum(results[p].get("metadata", {}).get("query_matches", 0) for p in part_ids if p in results and isinstance(results[p], dict) and "metadata" in results[p]),
                "project_relevance": project_relevance
            }
            
            return results
            
        except Exception as e:
            logging.error(f"Error in DevRev fetch_data: {str(e)}")
            return {"error": f"Failed to fetch DevRev data: {str(e)}"}
    
    async def _fetch_works_via_rest(self, part_id: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch works for a part using the REST API
        
        Args:
            part_id: Part ID
            filters: Dictionary of filters
            
        Returns:
            Dictionary with items, total_count, and other metadata
        """
        try:
            # Skip part lookup and directly fetch works
            # Works list endpoint
            endpoint = "works.list"
            
            # Use a simple POST request with minimal filter
            logging.info(f"Fetching works without part filtering")
            
            # Initialize data with just a limit
            data = {
                "limit": 100
            }
            
            # If the part_id looks like a DON, add it as a filter
            if part_id.startswith("don:"):
                logging.info(f"Using direct part ID filter: {part_id}")
                data["filter"] = {
                    "part_ids": [part_id]
                }
            # Otherwise, we'll just fetch without part filtering
            
            # Make the API request
            logging.info(f"Making REST API request to {endpoint}")
            response = self._make_api_request(
                endpoint=endpoint,
                method="POST",
                data=data
            )
            
            # Check for errors
            if "error" in response:
                error_msg = response.get("error", "Unknown API error")
                logging.error(f"REST API error: {error_msg}")
                return {"error": error_msg}
                
            # Extract works from response
            items = response.get("works", [])
            total_count = response.get("total_count", len(items))
            
            logging.info(f"Successfully fetched {len(items)} works")
                
            return {
                "items": items,
                "total_count": total_count,
                "has_more": response.get("has_more", False)
            }
        except Exception as e:
            logging.error(f"Error fetching works via REST: {str(e)}")
            return {"error": f"REST API error: {str(e)}"}
    
    async def _fetch_works_via_graphql(self, part_id: str, query: str) -> Dict[str, Any]:
        """Fetch works for a part using the GraphQL API
        
        Args:
            part_id: Part ID
            query: GraphQL query string
            
        Returns:
            Dictionary with items, total_count, and other metadata
        """
        try:
            # If this is a REST placeholder, it means we're using REST API but the query was built assuming GraphQL
            if query.startswith("rest:"):
                # Extract params from the placeholder
                params_str = query.replace("rest:", "")
                
                # Parse the params
                params = {}
                for param in params_str.split('&'):
                    key, value = param.split('=', 1)
                    params[key] = value
                    
                # Convert to filters and call REST API method
                filters = {}
                if "filter" in params:
                    # Parse the filter block - simplified for this example
                    filter_str = params["filter"].strip("{} ")
                    for filter_part in filter_str.split(','):
                        if ":" in filter_part:
                            k, v = filter_part.split(':', 1)
                            filters[k.strip()] = v.strip().strip('"\'')
                            
                # Add partId from the part_id argument
                filters["partId"] = part_id
                
                # Call the REST API method
                return await self._fetch_works_via_rest(part_id, filters)
                
            # Execute GraphQL query
            logging.info(f"Executing GraphQL query for part_id: {part_id}")
            response = self._execute_graphql_query(query)
            
            if "error" in response:
                error_msg = response.get("error", "Unknown API error")
                logging.error(f"GraphQL query error: {error_msg}")
                return {"error": error_msg}
            
            # Extract works from GraphQL response
            items = []
            total_count = 0
            has_next_page = False
            
            if "data" in response and "works" in response["data"]:
                works_data = response["data"]["works"]
                
                # Try edges/nodes pattern first (most common)
                if "edges" in works_data and isinstance(works_data["edges"], list):
                    # Process the edges to extract node data
                    for edge in works_data["edges"]:
                        if "node" in edge:
                            items.append(edge["node"])
                    
                    # Get page info
                    if "pageInfo" in works_data:
                        page_info = works_data["pageInfo"]
                        has_next_page = page_info.get("hasNextPage", False)
                
                # Try items pattern (alternative)
                elif "items" in works_data and isinstance(works_data["items"], list):
                    items = works_data["items"]
                    
                    # Get page info
                    if "pageInfo" in works_data:
                        page_info = works_data["pageInfo"]
                        has_next_page = page_info.get("hasNextPage", False)
                
                # Get total count
                total_count = works_data.get("totalCount", len(items))
            
            return {
                "items": items,
                "total_count": total_count,
                "has_more": has_next_page
            }
        except Exception as e:
            logging.error(f"Error fetching works via GraphQL: {str(e)}")
            return {"error": f"GraphQL API error: {str(e)}"}
    
    def format_for_llm(self, data: Dict[str, Any]) -> str:
        """Format DevRev data for LLM consumption.
        
        Args:
            data: Dictionary containing DevRev data
            
        Returns:
            Formatted string ready for LLM context
        """
        if not data or not isinstance(data, dict):
            return "No DevRev data available"
            
        if "error" in data:
            return f"Error retrieving DevRev data: {data['error']}"
            
        # Extract metadata if available
        metadata = data.get('_metadata', {})
        projects_processed = metadata.get('projects_processed', 0)
        total_issues = metadata.get('total_issues', 0)
        project_relevance = metadata.get('project_relevance', {})
        is_jql_query = metadata.get('query_type') == 'custom_jql'
        jql = metadata.get('jql', 'Custom JQL Query')
        
        formatted_sections = []
        
        # Add summary section
        if projects_processed > 1:
            summary = [
                f"DevRev Data Summary: {projects_processed} Projects with {total_issues} total works",
                "Projects by relevance score:"
            ]
            # Sort projects by relevance
            sorted_projects = sorted(
                project_relevance.items(), 
                key=lambda x: x[1], 
                reverse=True
            )
            for project, relevance in sorted_projects:
                summary.append(f"- {project}: Relevance {relevance:.2f}")
            
            formatted_sections.append("\n".join(summary))
            formatted_sections.append("-" * 40)
        elif is_jql_query:
            summary = [
                f"DevRev Data from JQL Query: {jql}",
                f"Total Works: {total_issues}"
            ]
            formatted_sections.append("\n".join(summary))
            formatted_sections.append("-" * 40)
            
            # Extract custom field names from JQL query to know what to display
            custom_field_names = self._extract_custom_fields_from_jql(jql)
            if custom_field_names:
                formatted_sections.append(f"Custom fields in query: {', '.join(custom_field_names)}")
        
        # Track aggregate counts for summary
        total_works_by_type = {}
        total_works_by_stage = {}
        total_works_by_priority = {}
        
        # Collect all works for custom field analysis
        all_works = []
        
        # Process each project or JQL query result
        for project_key, project_data in data.items():
            # Skip metadata entry
            if project_key == '_metadata':
                continue
                
            # Handle error case
            if isinstance(project_data, dict) and "error" in project_data:
                formatted_sections.append(f"## PROJECT: {project_key}")
                formatted_sections.append(f"Error: {project_data['error']}")
                formatted_sections.append("-" * 40)
                continue
                
            # Get relevance score if available
            relevance = 0.5
            if isinstance(project_data, dict) and "relevance" in project_data:
                relevance = project_data["relevance"]
                
            # Handle special case for JQL custom query
            is_jql_query_result = project_key == "jql_custom_query"
            
            # Format header with relevance
            if is_jql_query_result:
                jql_query = metadata.get('jql', project_data.get('metadata', {}).get('jql_query', 'Custom JQL Query'))
                formatted_sections.append(f"## JQL QUERY: {jql_query} (Relevance: {relevance:.2f})")
            else:
                formatted_sections.append(f"## PROJECT: {project_key} (Relevance: {relevance:.2f})")
            
            # Handle works/issues - fix the data structure handling
            works = []
            if isinstance(project_data, dict):
                if "issues" in project_data:
                    works = project_data["issues"]
                elif "items" in project_data:
                    # This handles the case where we get items directly from REST API
                    works = project_data["items"]
            elif isinstance(project_data, list):
                works = project_data
            
            # Additional validation to ensure works is a list
            if not isinstance(works, list):
                logging.error(f"Works data is not a list for project {project_key}, type: {type(works)}")
                formatted_sections.append("Error: Invalid works data structure.")
                formatted_sections.append("-" * 40)
                continue
            
            if not works:
                formatted_sections.append("No works found for this query.")
                formatted_sections.append("-" * 40)
                continue
                
            # Validate that all work items are dictionaries
            valid_works = []
            for i, work in enumerate(works):
                if isinstance(work, dict):
                    valid_works.append(work)
                else:
                    logging.error(f"Work item {i} in project {project_key} is not a dictionary, type: {type(work)}, value: {str(work)[:100]}")
            
            if not valid_works:
                formatted_sections.append("Error: No valid work items found.")
                formatted_sections.append("-" * 40)
                continue
            
            works = valid_works
            # Add works to the collection for custom field analysis
            all_works.extend(works)
                
            # Add works count
            formatted_sections.append(f"Total Works: {len(works)}")
            
            # Status distribution (stage in DevRev)
            stage_counts = {}
            type_counts = {}
            priority_counts = {}
            
            for work in works:
                try:
                    # Count by stage
                    stage = work.get('stage', {})
                    if isinstance(stage, dict):
                        stage_name = stage.get('name', 'Unknown')
                    else:
                        stage_name = str(stage) if stage else 'Unknown'
                    stage_counts[stage_name] = stage_counts.get(stage_name, 0) + 1
                    total_works_by_stage[stage_name] = total_works_by_stage.get(stage_name, 0) + 1
                    
                    # Count by type
                    work_type = work.get('type', {})
                    if isinstance(work_type, dict):
                        type_name = work_type.get('name', 'Unknown')
                    else:
                        type_name = str(work_type) if work_type else 'Unknown'
                    type_counts[type_name] = type_counts.get(type_name, 0) + 1
                    total_works_by_type[type_name] = total_works_by_type.get(type_name, 0) + 1
                    
                    # Count by priority
                    priority = work.get('priority', {})
                    if isinstance(priority, dict):
                        priority_name = priority.get('name', 'Unknown')
                    else:
                        priority_name = str(priority) if priority else 'Unknown'
                    priority_counts[priority_name] = priority_counts.get(priority_name, 0) + 1
                    total_works_by_priority[priority_name] = total_works_by_priority.get(priority_name, 0) + 1
                    
                except Exception as e:
                    logging.error(f"Error processing work item for statistics: {str(e)}")
                    stage_counts['Unknown'] = stage_counts.get('Unknown', 0) + 1
                    type_counts['Unknown'] = type_counts.get('Unknown', 0) + 1
                    priority_counts['Unknown'] = priority_counts.get('Unknown', 0) + 1
            
            # Output distribution by type, stage, and priority
            distribution_sections = []
            
            if type_counts:
                type_section = ["Type Distribution:"]
                for work_type, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
                    type_section.append(f"- {work_type}: {count}")
                distribution_sections.append("\n".join(type_section))
                
            if stage_counts:
                stage_section = ["Stage Distribution:"]
                for stage, count in sorted(stage_counts.items(), key=lambda x: x[1], reverse=True):
                    stage_section.append(f"- {stage}: {count}")
                distribution_sections.append("\n".join(stage_section))
                
            if priority_counts:
                priority_section = ["Priority Distribution:"]
                for priority, count in sorted(priority_counts.items(), key=lambda x: x[1], reverse=True):
                    priority_section.append(f"- {priority}: {count}")
                distribution_sections.append("\n".join(priority_section))
                
            formatted_sections.append("\n\n".join(distribution_sections))
            
            # Get the search query if available
            query = metadata.get('query', '').lower()
            
            # Format works (top 10 most recently updated)
            formatted_sections.append("\nRecent Works:")
            
            # Sort by modified date if possible
            try:
                sorted_works = sorted(
                    works,
                    key=lambda work: work.get('modifiedDate', ''),
                    reverse=True
                )[:10]  # Limit to top 10
            except Exception:
                # Fallback: take first 10
                sorted_works = works[:10]
                logging.warning("Failed to sort works by modified date, using first 10 works")
            
            # Extract custom field names from JQL query if available
            custom_field_names = []
            if is_jql_query_result:
                custom_field_names = self._extract_custom_fields_from_jql(jql)
            
            # Format each work
            for work in sorted_works:
                try:
                    work_id = work.get('displayId', 'Unknown-ID')
                    title = work.get('title', 'No title')
                    
                    # Get stage, type, priority with proper handling
                    stage = work.get('stage', {})
                    stage_name = stage.get('name', 'Unknown') if isinstance(stage, dict) else str(stage) if stage else 'Unknown'
                    
                    work_type = work.get('type', {})
                    type_name = work_type.get('name', 'Unknown') if isinstance(work_type, dict) else str(work_type) if work_type else 'Unknown'
                    
                    priority = work.get('priority', {})
                    priority_name = priority.get('name', 'Unknown') if isinstance(priority, dict) else str(priority) if priority else 'Unknown'
                    
                    # Get owner/assignee
                    owned_by = work.get('ownedBy', {})
                    owner = owned_by.get('displayName', 'Unassigned') if isinstance(owned_by, dict) else str(owned_by) if owned_by else 'Unassigned'
                    
                    # Highlight title if it matches the query
                    if query and title.lower().find(query) >= 0:
                        title_parts = title.split(query, 1)
                        title = f"{title_parts[0]}**{query}**{title_parts[1]}"
                    
                    formatted_work = [
                        f"- {work_id}: {title}",
                        f"  Type: {type_name} | Stage: {stage_name} | Priority: {priority_name} | Owner: {owner}"
                    ]
                    
                    # Extract and display custom fields (similar to Jira implementation)
                    custom_field_values = self._extract_all_custom_fields(work)
                    if custom_field_values:
                        # Sort by those mentioned in the JQL query first
                        prioritized_fields = []
                        other_fields = []
                        
                        for name, value in custom_field_values.items():
                            if custom_field_names and any(cf.lower() in name.lower() or name.lower() in cf.lower() for cf in custom_field_names):
                                prioritized_fields.append(f"{name}: {value}")
                            elif len(other_fields) < 5:  # Limit to 5 additional fields
                                other_fields.append(f"{name}: {value}")
                        
                        if prioritized_fields:
                            formatted_work.append(f"  Query Fields: {' | '.join(prioritized_fields)}")
                        if other_fields:
                            formatted_work.append(f"  Other Custom Fields: {' | '.join(other_fields)}")
                    
                    # Add timestamps for timeline
                    created_date = work.get('createdDate', '')
                    modified_date = work.get('modifiedDate', '')
                    if created_date or modified_date:
                        dates = []
                        if created_date:
                            created_str = created_date.split('T')[0] if 'T' in created_date else created_date
                            dates.append(f"Created: {created_str}")
                        if modified_date:
                            modified_str = modified_date.split('T')[0] if 'T' in modified_date else modified_date
                            dates.append(f"Updated: {modified_str}")
                        formatted_work.append(f"  {' | '.join(dates)}")
                    
                    # Add body snippet if available
                    body = work.get('body', '')
                    if body:
                        # Truncate long descriptions
                        body_preview = body[:100] + ('...' if len(body) > 100 else '')
                        # Clean up newlines for better formatting
                        body_preview = ' '.join(body_preview.split())
                        
                        # Highlight query terms in the description
                        if query and query in body_preview.lower():
                            idx = body_preview.lower().find(query)
                            query_len = len(query)
                            body_preview = f"{body_preview[:idx]}**{body_preview[idx:idx+query_len]}**{body_preview[idx+query_len:]}"
                            
                        formatted_work.append(f"  Description: {body_preview}")
                    
                    # Check for tags
                    try:
                        tags = work.get('tags', [])
                        if tags and isinstance(tags, list):
                            tag_names = [tag.get('name', '') for tag in tags if isinstance(tag, dict)]
                            if tag_names:
                                formatted_work.append(f"  Tags: {', '.join(tag_names)}")
                    except Exception:
                        pass
                    
                    # Check for parts
                    try:
                        parts = work.get('parts', [])
                        if parts and isinstance(parts, list):
                            part_names = [part.get('name', '') for part in parts if isinstance(part, dict)]
                            if part_names:
                                formatted_work.append(f"  Parts: {', '.join(part_names)}")
                    except Exception:
                        pass
                            
                    formatted_sections.append("\n".join(formatted_work))
                        
                except Exception as e:
                    logging.error(f"Error formatting work: {str(e)}")
                    # Add more detailed error information
                    work_info = "unknown"
                    try:
                        work_info = work.get('displayId', str(work)[:50])
                    except Exception:
                        work_info = "error getting work info"
                    formatted_sections.append(f"- Error formatting work {work_info}: {str(e)}")
            
            # Add separator between projects
            formatted_sections.append("-" * 40)
        
        # Add custom field analysis for JQL queries (similar to Jira)
        if is_jql_query and all_works:
            # Extract custom field names from JQL query
            custom_field_names = self._extract_custom_fields_from_jql(jql)
            
            # Try to gather distribution of values for custom fields in the query
            if custom_field_names:
                custom_field_values = self._get_custom_field_distributions(all_works, custom_field_names)
                if custom_field_values:
                    formatted_sections.append("\nCustom Field Distributions:")
                    for field_name, values in custom_field_values.items():
                        formatted_sections.append(f"- {field_name}:")
                        for value, count in values.items():
                            formatted_sections.append(f"  - {value}: {count}")
            
            # Collect all available fields from works to understand what data we have
            all_available_fields = self._collect_available_fields(all_works)
            if all_available_fields:
                standard_fields = all_available_fields.get("standard_fields", [])
                custom_fields = all_available_fields.get("custom_fields", [])
                
                # Include information about available fields
                if standard_fields:
                    formatted_sections.append("\nStandard fields available: " + ", ".join(standard_fields))
                if custom_fields:
                    formatted_sections.append("Custom fields available: " + ", ".join(custom_fields[:10]) + 
                                             (f" and {len(custom_fields) - 10} more..." if len(custom_fields) > 10 else ""))
        
        # Add overall summary at the end
        if len(data) > 1 and (total_works_by_type or total_works_by_stage or total_works_by_priority):
            summary_sections = ["# OVERALL SUMMARY"]
            
            if total_works_by_type:
                type_summary = ["Work Types:"]
                for work_type, count in sorted(total_works_by_type.items(), key=lambda x: x[1], reverse=True):
                    type_summary.append(f"- {work_type}: {count}")
                summary_sections.append("\n".join(type_summary))
                
            if total_works_by_stage:
                stage_summary = ["Work Stages:"]
                for stage, count in sorted(total_works_by_stage.items(), key=lambda x: x[1], reverse=True):
                    stage_summary.append(f"- {stage}: {count}")
                summary_sections.append("\n".join(stage_summary))
                
            if total_works_by_priority:
                priority_summary = ["Work Priorities:"]
                for priority, count in sorted(total_works_by_priority.items(), key=lambda x: x[1], reverse=True):
                    priority_summary.append(f"- {priority}: {count}")
                summary_sections.append("\n".join(priority_summary))
                
            formatted_sections.append("\n\n".join(summary_sections))
        
        return "\n".join(formatted_sections)
    
    def get_form_fields(self) -> Dict[str, Any]:
        """Return form fields for DevRev configuration"""
        return {
            'type': 'devrev',
            'fields': [
                {
                    'type': 'text',
                    'label': 'Project Key',
                    'name': 'project_key',
                    'placeholder': 'Enter DevRev project name or ID (e.g., PROD or PROD, DEV)',
                    'help_text': 'Enter one or more DevRev project names or IDs, separated by commas.',
                    'optional': True,
                    'add_more': True
                },
                {
                    'type': 'textarea',
                    'label': 'JQL Query',
                    'name': 'jql_query',
                    'placeholder': 'Enter a JQL query (e.g., project = PROD AND status = "In Progress")',
                    'help_text': 'Advanced: Enter a JQL query to fetch specific works. Will be converted to DevRev API parameters. Takes precedence over Project Key if both provided.',
                    'optional': True
                },
                {
                    'type': 'number',
                    'label': 'Days Lookback',
                    'name': 'days',
                    'placeholder': '30',
                    'help_text': 'Number of days to look back for updated works.',
                    'optional': True
                }
            ]
        }
    
    def _execute_graphql_query(self, query: str) -> Dict[str, Any]:
        """Execute a GraphQL query against the DevRev API
        
        Args:
            query: The GraphQL query string
            
        Returns:
            Dictionary containing the GraphQL response
        """
        # If we don't have a graphql_endpoint set yet, try to find one
        if not hasattr(self, 'graphql_endpoint') or not self.graphql_endpoint:
            # Try to find a working GraphQL endpoint
            possible_endpoints = [
                "graphql", 
                "v1/graphql", 
                "api/graphql", 
                "graphql/v1",
                "api/v1/graphql",
                "v1/api/graphql",
                "public/graphql",
                ""  # Empty string means try the base URL
            ]
            
            # Keep track of all errors to report if all endpoints fail
            all_errors = {}
            
            # Log the query for debugging
            logging.info(f"Executing GraphQL query: {query[:100]}...")
            
            # Try each possible endpoint until we find one that works
            for endpoint in possible_endpoints:
                try:
                    # Construct the full URL
                    full_url = self.api_url if endpoint == "" else f"{self.api_url}/{endpoint}"
                    logging.info(f"Trying GraphQL endpoint: {full_url}")
                    
                    # Always use POST for GraphQL
                    response = requests.post(
                        url=full_url,
                        headers=self.headers,
                        json={"query": query},
                        timeout=30
                    )
                    
                    # Check the response
                    if response.status_code >= 400:
                        error_msg = f"Error {response.status_code}: {response.text}"
                        all_errors[full_url] = error_msg
                        logging.warning(f"GraphQL request to {endpoint} failed: {error_msg}")
                        continue
                    
                    # Try to parse the response as JSON
                    try:
                        result = response.json()
                        
                        # Check for GraphQL-specific errors
                        if "errors" in result and not "data" in result:
                            error_msgs = [error.get("message", "Unknown GraphQL error") for error in result.get("errors", [])]
                            error_msg = "; ".join(error_msgs)
                            all_errors[full_url] = error_msg
                            logging.warning(f"GraphQL errors with endpoint {endpoint}: {error_msg}")
                            continue
                        
                        # If we have data or this appears to be a valid GraphQL response, store the endpoint
                        if "data" in result or "errors" in result:
                            logging.info(f"Successfully used GraphQL endpoint: {endpoint}")
                            self.graphql_endpoint = endpoint
                            return result
                    except ValueError:
                        all_errors[full_url] = "Invalid JSON response"
                        continue
                except Exception as e:
                    logging.warning(f"Error trying GraphQL endpoint {endpoint}: {str(e)}")
                    all_errors[endpoint] = str(e)
                    continue
                
            # If we get here, all endpoints failed
            error_details = "\n".join([f"{endpoint}: {error}" for endpoint, error in all_errors.items()])
            error_msg = f"Failed to find working GraphQL endpoint. Errors: {error_details}"
            logging.error(error_msg)
            return {"error": f"GraphQL endpoint not available. Tried {len(possible_endpoints)} different endpoints."}
        else:
            # We already have a working endpoint, use it
            try:
                full_url = self.api_url if self.graphql_endpoint == "" else f"{self.api_url}/{self.graphql_endpoint}"
                logging.info(f"Using known GraphQL endpoint: {full_url}")
                
                response = requests.post(
                    url=full_url,
                    headers=self.headers,
                    json={"query": query},
                    timeout=30
                )
                
                if response.status_code >= 400:
                    error_msg = f"Error {response.status_code}: {response.text}"
                    logging.error(f"GraphQL request failed: {error_msg}")
                    return {"error": error_msg}
                
                try:
                    result = response.json()
                    
                    # Check for GraphQL-specific errors
                    if "errors" in result and not "data" in result:
                        error_msgs = [error.get("message", "Unknown GraphQL error") for error in result.get("errors", [])]
                        error_msg = "; ".join(error_msgs)
                        logging.error(f"GraphQL errors: {error_msg}")
                        return {"error": error_msg}
                    
                    return result
                except ValueError as e:
                    error_msg = f"Failed to parse response as JSON: {str(e)}"
                    logging.error(error_msg)
                    return {"error": error_msg}
            except Exception as e:
                logging.error(f"Error executing GraphQL query: {str(e)}")
                return {"error": str(e)}
    
    def process_direct_graphql_query(self, query: str) -> Dict[str, Any]:
        """Process a direct GraphQL query provided by the user
        
        Args:
            query: The complete GraphQL query string
            
        Returns:
            Dictionary containing the results
        """
        logging.info(f"Processing direct GraphQL query from user")
        logging.debug(f"Query: {query}")
        
        # Clean up the query if needed
        query = query.strip()
        
        # Since DevRev doesn't support GraphQL, we need to convert this to REST API calls
        logging.info("DevRev doesn't support GraphQL. Converting to REST API calls.")
        
        # Parse the GraphQL query to extract relevant information
        parsed_query = self._parse_graphql_query(query)
        
        # Extract project key and other filters
        project_key = parsed_query.get('project_key')
        custom_fields = parsed_query.get('custom_fields', {})
        
        logging.info(f"Extracted project key: {project_key}")
        logging.info(f"Extracted custom fields: {custom_fields}")
        
        # Now use the extracted information to make REST API calls
        try:
            # Try a simpler approach - use query parameters directly without a complex filter object
            # Since the filter object format seems to be causing errors
            
            endpoint = "works.list"
            method = "GET"  # Use GET instead of POST
            params = {
                "limit": 100
            }
            
            # If we have a project key, try to find the corresponding works
            if project_key:
                # Try to find the part ID first, then use that to filter
                try:
                    parts_response = self._make_api_request(
                        endpoint="parts.list",
                        method="GET"
                    )
                    
                    # Check if we got a valid response with parts
                    if "parts" in parts_response and parts_response["parts"]:
                        # Look for a matching part by name or display_id
                        matching_part = None
                        for part in parts_response["parts"]:
                            if (part.get("name", "").lower() == project_key.lower() or 
                                part.get("display_id", "").lower() == project_key.lower()):
                                matching_part = part
                                break
                        
                        # If we found a matching part, use its ID
                        if matching_part:
                            params["part_id"] = matching_part.get("id")
                            logging.info(f"Found matching part ID: {matching_part.get('id')}")
                except Exception as e:
                    logging.error(f"Error searching for part ID: {str(e)}")
            
            # Add tags based on custom fields as a simpler way to filter
            if custom_fields:
                tags = []
                
                # Convert custom fields to tags
                for field_name, field_value in custom_fields.items():
                    if isinstance(field_value, list):
                        # For array values, add each as a separate tag
                        for value in field_value:
                            tags.append(f"{field_name}:{value}")
                    else:
                        # For scalar values
                        tags.append(f"{field_name}:{field_value}")
                
                # If we have tags, add them to the params
                if tags:
                    # Join tags with commas for the query parameter
                    params["tags"] = ",".join(tags)
                    logging.info(f"Added tags for filtering: {params['tags']}")
            
            # Make the API call with the simplified approach
            logging.info(f"Making simplified REST API request to {endpoint} with params: {params}")
            response = self._make_api_request(
                endpoint=endpoint,
                method=method,
                params=params
            )
            
            # Check for errors in the response
            if "error" in response:
                error_msg = response.get("error", "Unknown API error")
                logging.error(f"REST API error: {error_msg}")
                
                # Try a more minimal request as fallback
                logging.info("Trying a more minimal request as fallback")
                minimal_response = self._make_api_request(
                    endpoint=endpoint,
                    method="GET",
                    params={"limit": 100}
                )
                
                if "error" in minimal_response:
                    # If even the minimal request fails, return the original error
                    return {
                        "error": f"Error executing REST API request: {error_msg}",
                        "relevance": 0.2
                    }
                else:
                    # If the minimal request works, filter results client-side
                    logging.info("Using client-side filtering on minimal results")
                    
                    # Ensure we have a valid response structure before proceeding
                    if not isinstance(minimal_response, dict):
                        logging.error(f"Unexpected minimal response type: {type(minimal_response)}")
                        return {"error": "Invalid response format from API", "relevance": 0.2}
                    
                    # Extract works from minimal response with careful handling of the data structure
                    all_works = []
                    
                    # Check if we have works directly
                    if "works" in minimal_response and isinstance(minimal_response["works"], list):
                        all_works = minimal_response["works"]
                        logging.info(f"Found {len(all_works)} works in 'works' field")
                    # Check if works are in the data field
                    elif "data" in minimal_response and isinstance(minimal_response["data"], list):
                        all_works = minimal_response["data"]
                        logging.info(f"Found {len(all_works)} works in 'data' field")
                    # Check if we have a nested structure
                    elif "data" in minimal_response and isinstance(minimal_response["data"], dict):
                        # Try to find works in nested data structure
                        data_dict = minimal_response["data"]
                        for key, value in data_dict.items():
                            if isinstance(value, list) and len(value) > 0:
                                all_works = value
                                logging.info(f"Found {len(all_works)} works in data.{key} field")
                                break
                    
                    if not all_works:
                        logging.warning("No works found in minimal response")
                        all_works = []  # Ensure it's at least an empty list
                        
                    try:
                        # Filter works client-side based on extracted fields
                        filtered_works = self._filter_works_client_side(all_works, project_key, custom_fields)
                        
                        # Handle case where filtered_works could be None
                        if filtered_works is None:
                            logging.warning("Client-side filtering returned None, using empty list")
                            filtered_works = []
                            
                        total_count = len(filtered_works)
                        
                        logging.info(f"Client-side filtering found {total_count} works out of {len(all_works)} total")
                        
                        # Format response
                        return {
                            "issues": filtered_works,
                            "total": total_count,
                            "relevance": 0.7 if filtered_works else 0.5,
                            "metadata": {
                                "original_query": query,
                                "extracted_project": project_key,
                                "extracted_custom_fields": custom_fields,
                                "processing_time": str(datetime.now()),
                                "api_type": "rest_with_client_filtering",
                                "total_count": total_count,
                                "query_type": "graphql_converted_to_rest",
                                "filtering_method": "client_side"
                            }
                        }
                    except Exception as e:
                        logging.error(f"Error during client-side filtering: {str(e)}", exc_info=True)
                        return {
                            "error": f"Error during client-side filtering: {str(e)}",
                            "issues": [],
                            "total": 0,
                            "relevance": 0.3,
                            "metadata": {
                                "original_query": query,
                                "error": str(e),
                                "api_type": "rest",
                                "query_type": "graphql_converted_to_rest"
                            }
                        }
            
            # Extract works from the response
            works = []
            
            # Check if we have works directly
            if "works" in response and isinstance(response["works"], list):
                works = response["works"]
            # Check if works are in the data field
            elif "data" in response and isinstance(response["data"], list):
                works = response["data"]
            # Check if we have a nested structure
            elif "data" in response and isinstance(response["data"], dict):
                # Try to find works in nested data structure
                data_dict = response["data"]
                for key, value in data_dict.items():
                    if isinstance(value, list) and len(value) > 0:
                        works = value
                        break
            
            total_count = len(works)
            
            logging.info(f"Successfully fetched {total_count} works directly from API")
            
            # Format response
            result = {
                "issues": works,
                "total": total_count,
                "relevance": 0.8 if works else 0.5,
                "metadata": {
                    "original_query": query,
                    "extracted_project": project_key,
                    "extracted_custom_fields": custom_fields,
                    "processing_time": str(datetime.now()),
                    "api_type": "rest",
                    "total_count": total_count,
                    "query_type": "graphql_converted_to_rest"
                }
            }
            
            return result
                
        except Exception as e:
            logging.error(f"Error processing GraphQL query via REST API: {str(e)}")
            return {"error": f"REST API error: {str(e)}"}

    def _filter_works_client_side(self, works: List[Dict[str, Any]], project_key: str, custom_fields: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Filter works based on project key and custom fields client-side
        
        Args:
            works: List of works to filter
            project_key: Project key to filter by
            custom_fields: Custom fields to filter by
            
        Returns:
            Filtered list of works
        """
        # Ensure works is a list and not None
        if not works or not isinstance(works, list):
            logging.warning("Invalid works data for filtering: not a list or empty")
            return []
        
        logging.info(f"Client-side filtering {len(works)} works")
        
        # Start with all works
        filtered_works = works.copy()
        
        # Track if we've filtered anything
        project_filter_applied = False
        custom_fields_filter_applied = False
        
        # Filter by project/part if needed
        if project_key:
            project_filtered = []
            for work in filtered_works:
                # Skip invalid work objects
                if not work or not isinstance(work, dict):
                    continue
                
                # Check if work is associated with the specified project
                # Based on our testing, works have 'applies_to_part' field, not 'parts'
                part = work.get("applies_to_part", {})
                if part and isinstance(part, dict):
                    # Check if part matches by name or display ID
                    part_name = part.get("name", "")
                    part_display_id = part.get("display_id", "")
                    
                    if (project_key.lower() == (part_name or "").lower() or
                        project_key.lower() == (part_display_id or "").lower()):
                        project_filtered.append(work)
                        continue
                
                # Also check other part-related fields if present
                owned_by_part = work.get("owned_by_part", {})
                if owned_by_part and isinstance(owned_by_part, dict):
                    part_name = owned_by_part.get("name", "")
                    part_display_id = owned_by_part.get("display_id", "")
                    
                    if (project_key.lower() == (part_name or "").lower() or
                        project_key.lower() == (part_display_id or "").lower()):
                        project_filtered.append(work)
                        continue
        
            # If we found any matches, update the filtered list
            if project_filtered:
                filtered_works = project_filtered
                project_filter_applied = True
                logging.info(f"Filtered to {len(filtered_works)} works matching project {project_key}")
            else:
                logging.warning(f"No works matched project {project_key}")
        
        # Filter by custom fields
        if custom_fields:
            custom_filtered = []
            for work in filtered_works:
                # Skip invalid work objects
                if not work or not isinstance(work, dict):
                    continue
                
                # Assume work matches until we find a non-matching field
                matches = True
                
                # Get custom fields from the work
                work_custom_fields = work.get("custom_fields", {})
                if work_custom_fields is None:
                    work_custom_fields = {}
                
                # Check each custom field
                for field_name, expected_value in custom_fields.items():
                    # Convert field name to snake_case for better matching with possible API responses
                    snake_field_name = self._convert_field_name_for_rest(field_name)
                    
                    # Try multiple field name formats
                    field_names_to_try = [
                        field_name,                # Original (e.g., keyProjectStatus)
                        snake_field_name,          # Snake case (e.g., key_project_status)
                        field_name.lower(),        # Lowercase (e.g., keyprojectstatus)
                        snake_field_name.lower()   # Lowercase snake case (e.g., key_project_status)
                    ]
                    
                    # Find the first field name that exists
                    actual_value = None
                    for name in field_names_to_try:
                        if name in work_custom_fields:
                            actual_value = work_custom_fields[name]
                            break
                    
                    # If field doesn't exist in custom_fields, not a match
                    if actual_value is None:
                        matches = False
                        break
                        
                    # Check if the values match
                    if isinstance(expected_value, list):
                        # For list values, check if actual_value is in the list
                        if actual_value not in expected_value:
                            matches = False
                            break
                    else:
                        # For scalar values
                        if actual_value != expected_value:
                            matches = False
                            break
                
                # If all fields matched, add to filtered list
                if matches:
                    custom_filtered.append(work)
            
            # Always update filtered_works when custom_fields filter is applied,
            # even if the result is an empty list
            filtered_works = custom_filtered
            custom_fields_filter_applied = True
            
            if custom_filtered:
                logging.info(f"Filtered to {len(filtered_works)} works matching custom fields")
            else:
                logging.warning(f"No works matched custom fields criteria")
        
        if not project_filter_applied and not custom_fields_filter_applied:
            logging.info(f"No filtering applied - returning all {len(filtered_works)} works")
        else:
            logging.info(f"Final result: {len(filtered_works)} works after all filtering")
        
        return filtered_works
    
    def _parse_graphql_query(self, query: str) -> Dict[str, Any]:
        """Parse a GraphQL query to extract structured information
        
        Args:
            query: GraphQL query string
            
        Returns:
            Dictionary with extracted components from the query
        """
        # Initialize result structure
        result = {
            'project_key': None,
            'custom_fields': {},
            'filters': {},
        }
        
        # Extract project key using various patterns
        # Pattern 1: project: { key: "XXX" }
        project_match1 = re.search(r'project\s*:\s*{\s*key\s*:\s*"([^"]+)"', query)
        # Pattern 2: project = "XXX" or project: "XXX"
        project_match2 = re.search(r'project\s*[=:]\s*"([^"]+)"', query)
        # Pattern 3: project { key: "XXX" }
        project_match3 = re.search(r'project\s*{\s*key\s*:\s*"([^"]+)"', query)
        # Pattern 4: "key": "XXX" (anywhere in the query when there's no explicit project)
        project_match4 = re.search(r'"key"\s*:\s*"([^"]+)"', query)
        
        if project_match1:
            result['project_key'] = project_match1.group(1)
            logging.info(f"Extracted project key (pattern 1): {result['project_key']}")
        elif project_match2:
            result['project_key'] = project_match2.group(1)
            logging.info(f"Extracted project key (pattern 2): {result['project_key']}")
        elif project_match3:
            result['project_key'] = project_match3.group(1)
            logging.info(f"Extracted project key (pattern 3): {result['project_key']}")
        elif project_match4:
            result['project_key'] = project_match4.group(1)
            logging.info(f"Extracted project key (pattern 4): {result['project_key']}")
        
        # Extract custom fields with more complex patterns
        # First, look for customFields section
        custom_fields_block = None
        custom_fields_match = re.search(r'customFields\s*:\s*{([^}]+)}', query)
        
        if custom_fields_match:
            custom_fields_block = custom_fields_match.group(1).strip()
            logging.info(f"Found custom fields block: {custom_fields_block}")
            
            # Extract key-value pairs from the custom fields block
            # Handle different value types: strings, arrays, objects
            field_pattern = r'(\w+)\s*:\s*(?:"([^"]+)"|(\[[^\]]+\])|{([^}]+)})'
            
            for match in re.finditer(field_pattern, custom_fields_block):
                field_name = match.group(1)
                # Determine which value group matched
                if match.group(2):  # String value
                    field_value = match.group(2)
                elif match.group(3):  # Array value
                    # Process array values
                    array_values = []
                    for array_match in re.finditer(r'"([^"]+)"', match.group(3)):
                        array_values.append(array_match.group(1))
                    field_value = array_values
                elif match.group(4):  # Object value
                    # Simplified object handling (for now just keep as string)
                    field_value = match.group(4)
                else:
                    continue  # Skip if no value could be extracted
                
                result['custom_fields'][field_name] = field_value
                logging.info(f"Extracted custom field: {field_name} = {field_value}")
        
        # Extract additional filters from filter section
        filter_match = re.search(r'filter\s*:\s*{([^}]+)}', query)
        if filter_match:
            filter_content = filter_match.group(1).strip()
            logging.info(f"Found filter block: {filter_content}")
            
            # Extract fields directly in the filter (outside of project and customFields)
            # Example: status: "Open", priority: "High"
            direct_filter_pattern = r'(\w+)\s*:\s*(?:"([^"]+)"|(\[[^\]]+\]))'
            
            for match in re.finditer(direct_filter_pattern, filter_content):
                field_name = match.group(1)
                # Skip already processed fields
                if field_name in ['project', 'customFields']:
                    continue
                
                # Determine which value group matched
                if match.group(2):  # String value
                    field_value = match.group(2)
                elif match.group(3):  # Array value
                    # Process array values
                    array_values = []
                    for array_match in re.finditer(r'"([^"]+)"', match.group(3)):
                        array_values.append(array_match.group(1))
                    field_value = array_values
                else:
                    continue  # Skip if no value could be extracted
                
                result['filters'][field_name] = field_value
                logging.info(f"Extracted filter: {field_name} = {field_value}")
            
        return result

    def _build_rest_params_from_graphql(self, parsed_query: Dict[str, Any]) -> Dict[str, Any]:
        """Convert parsed GraphQL parameters into DevRev REST API parameters
        
        Args:
            parsed_query: Dictionary with extracted GraphQL parameters
            
        Returns:
            Dictionary with proper REST API parameters
        """
        # Check DevRev API docs to understand expected format
        # The error suggests that our filter structure is incorrect
        
        # Start with basic parameters
        rest_params = {
            "limit": 100  # Default limit
        }
        
        # Create a valid filter object according to DevRev API requirements
        # Based on the API error, we need to properly format the filter
        filter_object = {}
        
        # Add project/part ID if available
        if parsed_query.get('project_key'):
            # First try to find the part ID by name through a separate API call
            try:
                # Try to look up the part ID by name
                parts_response = self._make_api_request(
                    endpoint="parts.list",
                    method="GET",
                    params={"name": parsed_query['project_key']}
                )
                
                # If we found parts, use the first matching one's ID
                if parts_response.get("parts") and len(parts_response["parts"]) > 0:
                    part = parts_response["parts"][0]
                    filter_object['part_ids'] = [part.get('id')]
                    logging.info(f"Mapped project key {parsed_query['project_key']} to part ID {part.get('id')}")
                else:
                    # Otherwise, try searching by display ID
                    logging.warning(f"Could not find part by name '{parsed_query['project_key']}', trying display_id")
                    parts_response = self._make_api_request(
                        endpoint="parts.list",
                        method="GET",
                        params={"display_id": parsed_query['project_key']}
                    )
                    
                    if parts_response.get("parts") and len(parts_response["parts"]) > 0:
                        part = parts_response["parts"][0]
                        filter_object['part_ids'] = [part.get('id')]
                        logging.info(f"Mapped display_id {parsed_query['project_key']} to part ID {part.get('id')}")
                    else:
                        # If still not found, just store the name as a tag to match
                        logging.warning(f"Could not find part ID for {parsed_query['project_key']}, using as tag")
                        filter_object['tags'] = [parsed_query['project_key']]
            except Exception as e:
                logging.error(f"Error looking up part ID: {str(e)}")
                # Fall back to using as a tag
                filter_object['tags'] = [parsed_query['project_key']]
        
        # Simplify the custom fields handling based on API requirements
        # Check documentation/logs to make sure the format is correct
        if parsed_query.get('custom_fields'):
            # For DevRev API, it appears we might need a different format
            for field_name, field_value in parsed_query['custom_fields'].items():
                # Convert field name to snake_case for REST API
                api_field_name = self._convert_field_name_for_rest(field_name)
                
                # For filter matching, we'll add as tags since custom_fields may not be directly filterable
                # This is a simplification to get working results - improve based on API testing
                if isinstance(field_value, list):
                    # For lists, add each value as a tag
                    for value in field_value:
                        # Create a more specific tag format that combines field and value
                        tag = f"{api_field_name}:{value}"
                        if 'tags' not in filter_object:
                            filter_object['tags'] = []
                        filter_object['tags'].append(tag)
                else:
                    # For single values
                    tag = f"{api_field_name}:{field_value}"
                    if 'tags' not in filter_object:
                        filter_object['tags'] = []
                    filter_object['tags'].append(tag)
        
        # Add direct filters
        if parsed_query.get('filters'):
            for filter_name, filter_value in parsed_query['filters'].items():
                api_filter_name = self._convert_field_name_for_rest(filter_name)
                
                # Map common fields to their API counterparts
                if api_filter_name in ['stage', 'status']:
                    filter_object['stage'] = filter_value
                elif api_filter_name in ['type']:
                    filter_object['type'] = filter_value
                elif api_filter_name in ['priority']:
                    filter_object['priority'] = filter_value
                
                # For other fields, add as tags for now
                else:
                    tag = f"{api_filter_name}:{filter_value}"
                    if 'tags' not in filter_object:
                        filter_object['tags'] = []
                    filter_object['tags'].append(tag)
        
        # Add the filter object if not empty
        if filter_object:
            rest_params["filter"] = filter_object
            
        # Log the final parameters
        logging.info(f"Built REST parameters from GraphQL: {json.dumps(rest_params, indent=2)}")
                
        return rest_params

    def _convert_field_name_for_rest(self, graphql_field: str) -> str:
        """Convert GraphQL field names to REST API field names
        
        Args:
            graphql_field: Field name in GraphQL format (usually camelCase)
            
        Returns:
            Field name in REST API format (usually snake_case)
        """
        # Handle specific field mappings
        field_mappings = {
            'keyProjectStatus': 'key_project_status',
            'quarterNew': 'quarter_new',
            'startDate': 'start_date',
            'endDate': 'end_date',
            'dueDate': 'due_date',
            'assignedTo': 'assigned_to',
            'ownedBy': 'owned_by',
        }
        
        # Check for exact match in mappings
        if graphql_field in field_mappings:
            return field_mappings[graphql_field]
        
        # Otherwise, convert camelCase to snake_case
        snake_case = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', graphql_field).lower()
        
        return snake_case
    
    def _is_graphql_query(self, text: str) -> bool:
        """Determine if a string is a GraphQL query
        
        Args:
            text: String to check
            
        Returns:
            True if the string appears to be a GraphQL query
        """
        if not isinstance(text, str):
            return False
        
        text = text.strip()
        
        # Check for obvious GraphQL query indicators - must be more strict
        if text.startswith("query ") or text.startswith("mutation ") or text.startswith("subscription "):
            return True
            
        # Check if it starts with { and has GraphQL structure
        if text.startswith("{") and len(text) > 10:
            # Look for GraphQL-specific patterns within braces
            graphql_structure_patterns = [
                re.search(r'\{\s*\w+\s*\(.*\)\s*\{', text),        # { fieldName(args) {
                re.search(r'\{\s*\w+\s*\{\s*\w+\s*\}', text),       # { field { subfield }
                re.search(r'edges\s*\{\s*node\s*\{', text),         # edges { node {
                re.search(r'pageInfo\s*\{', text),                  # pageInfo {
                re.search(r'__typename', text),                     # __typename (GraphQL introspection)
            ]
            
            if any(graphql_structure_patterns):
                return True
        
        # Look for specific GraphQL keywords in proper context
        # These patterns are more restrictive to avoid false positives
        restrictive_graphql_patterns = [
            re.search(r'query\s+\w+\s*\(.*\$\w+', text),          # query SomeName($var
            re.search(r'query\s*\{\s*\w+\s*\(.*\)', text),        # query { someField(...)
            # Remove overly broad patterns like "filter: {" as they can appear in normal text
        ]
        
        return any(restrictive_graphql_patterns)
    
    def _extract_part_ids_from_params(self, params: Dict[str, Any]) -> List[str]:
        """Extract part IDs from parameters
        
        Args:
            params: Parameter dictionary
            
        Returns:
            List of part IDs
        """
        part_ids = []
        
        logging.info(f"Extracting part IDs from params: {params}")
        
        # First check if we have a JQL query to extract projects from
        if 'jql_query' in params and params['jql_query']:
            jql = params['jql_query'].strip()
            logging.info(f"Found JQL query: {jql}")
            
            if self._is_graphql_query(jql):
                logging.warning(f"JQL query detected as GraphQL, skipping: {jql}")
            else:
                logging.info(f"Extracting part IDs from JQL query: {jql}")
                try:
                    jql_structure = self._parse_jql_query(jql)
                    projects_from_jql = jql_structure.get('projects', [])
                    if projects_from_jql:
                        part_ids.extend(projects_from_jql)
                        logging.info(f"Successfully extracted part IDs from JQL: {part_ids}")
                    else:
                        logging.warning(f"No project keys found in JQL query: {jql}")
                except Exception as e:
                    logging.error(f"Error parsing JQL query '{jql}': {str(e)}")
        
        # Extract part IDs from project_key if JQL didn't provide any
        if not part_ids and 'project_key' in params:
            project_key_param = params['project_key']
            logging.info(f"Extracting part IDs from project_key param: {project_key_param}")
            
            if isinstance(project_key_param, list):
                # Process each key in the list
                for key in project_key_param:
                    if isinstance(key, str) and key.strip():
                        if self._is_graphql_query(key):
                            logging.warning(f"Project key detected as GraphQL, skipping: {key}")
                            continue
                            
                        if ',' in key:
                            # Handle comma-separated values
                            parts = [k.strip() for k in key.split(',') if k.strip()]
                            part_ids.extend(parts)
                            logging.info(f"Added comma-separated part IDs: {parts}")
                        else:
                            part_ids.append(key.strip())
                            logging.info(f"Added single part ID: {key.strip()}")
                            
            elif isinstance(project_key_param, str) and project_key_param.strip():
                if self._is_graphql_query(project_key_param):
                    logging.warning(f"Project key detected as GraphQL, skipping: {project_key_param}")
                else:
                    # Handle comma-separated string
                    parts = [k.strip() for k in project_key_param.split(',') if k.strip()]
                    part_ids.extend(parts)
                    logging.info(f"Added part IDs from string: {parts}")
                    
            elif project_key_param and not self._is_graphql_query(str(project_key_param)):
                # Handle single project key
                part_ids.append(str(project_key_param).strip())
                logging.info(f"Added single project key: {str(project_key_param).strip()}")
        
        # Remove duplicates while preserving order
        seen = set()
        unique_part_ids = [x for x in part_ids if not (x in seen or seen.add(x))]
        
        if not unique_part_ids:
            logging.warning(f"No part IDs extracted from params. Available params: {list(params.keys())}")
            # Log potential issues
            if 'jql_query' in params:
                jql = params.get('jql_query', '')
                if self._is_graphql_query(jql):
                    logging.warning(f"JQL query was classified as GraphQL: {jql}")
            if 'project_key' in params:
                proj_key = params.get('project_key', '')
                if self._is_graphql_query(str(proj_key)):
                    logging.warning(f"Project key was classified as GraphQL: {proj_key}")
        else:
            logging.info(f"Successfully extracted {len(unique_part_ids)} unique part IDs: {unique_part_ids}")
            
        return unique_part_ids

    def _extract_custom_field_value(self, work: Dict[str, Any], field_name: str) -> str:
        """Extract value of a custom field from a DevRev work item.
        
        Args:
            work: DevRev work item dictionary
            field_name: Name of the custom field (display name or ID)
            
        Returns:
            String representation of the field value
        """
        if not work or not isinstance(work, dict):
            logging.debug(f"Invalid work item for field '{field_name}'")
            return None
            
        original_field_name = field_name
        
        # Handle specific known custom fields with special character handling
        if field_name.startswith('"') and field_name.endswith('"'):
            field_name = field_name[1:-1]  # Remove quotes
            
        # Get custom fields from the work item
        custom_fields = work.get("custom_fields", {})
        if not custom_fields:
            custom_fields = {}
            
        logging.debug(f"Searching for field '{original_field_name}' in {len(custom_fields)} custom fields")
        
        # 1. First, try direct field name access
        if field_name in custom_fields:
            result = self._format_field_value(custom_fields[field_name])
            logging.debug(f"Found direct match for '{field_name}': {result}")
            return result
            
        # 2. Second, try case-insensitive matching
        field_name_lower = field_name.lower()
        for cf_name, cf_value in custom_fields.items():
            if cf_name.lower() == field_name_lower:
                result = self._format_field_value(cf_value)
                logging.debug(f"Found case-insensitive match for '{field_name}' -> '{cf_name}': {result}")
                return result
        
        # 3. Handle fields with hash prefix like "#Key Project Status"
        if field_name.startswith("#"):
            clean_name = field_name[1:].lower().replace(' ', '').replace('(', '').replace(')', '')
            
            # Try various common variations of the field name
            field_variants = [
                clean_name,
                field_name.lower(),
                field_name[1:].lower(),  # without hash
                field_name.replace('#', '').lower(),
                clean_name.replace('status', 'stat'),
                clean_name.replace('project', 'proj'),
                clean_name.replace('key', ''),  # Remove 'key' from field name
            ]
            
            # Look for matching fields in custom_fields
            for variant in field_variants:
                for cf_name, cf_value in custom_fields.items():
                    cf_name_clean = cf_name.lower().replace(' ', '').replace('_', '').replace('-', '')
                    if variant in cf_name_clean or cf_name_clean in variant:
                        result = self._format_field_value(cf_value)
                        logging.debug(f"Found hash field match for '{field_name}' -> '{cf_name}' via variant '{variant}': {result}")
                        return result
                        
            # Special handling for common field patterns
            if 'quarter' in clean_name:
                # Look for fiscal quarter format (FY26-Q1)
                fy_quarter_pattern = r'FY\d+-Q[1-4]'
                for cf_name, cf_value in custom_fields.items():
                    if cf_value and re.search(fy_quarter_pattern, str(cf_value)):
                        result = str(cf_value)
                        logging.debug(f"Found quarter pattern match for '{field_name}' -> '{cf_name}': {result}")
                        return result
                        
            if 'status' in clean_name and 'key' in clean_name:
                # Look for specific status values like "Yes-POD", "Yes-Group"
                pod_status_pattern = r'Yes-(?:POD|Group|Sub-Group)'
                for cf_name, cf_value in custom_fields.items():
                    if cf_value and re.search(pod_status_pattern, str(cf_value)):
                        result = str(cf_value)
                        logging.debug(f"Found status pattern match for '{field_name}' -> '{cf_name}': {result}")
                        return result
                
        # 4. Third, try partial matching for field names with spaces/special chars
        for cf_name, cf_value in custom_fields.items():
            cf_name_clean = cf_name.lower().replace(' ', '').replace('_', '').replace('-', '').replace('(', '').replace(')', '')
            field_clean = field_name_lower.replace(' ', '').replace('_', '').replace('-', '').replace('(', '').replace(')', '').replace('#', '')
            
            # Try bidirectional substring matching
            if (field_clean in cf_name_clean or cf_name_clean in field_clean) and len(field_clean) > 2:
                result = self._format_field_value(cf_value)
                logging.debug(f"Found partial match for '{field_name}' -> '{cf_name}': {result}")
                return result
        
        # 5. Try fuzzy matching with word-based similarity
        field_words = set(re.findall(r'\w+', field_name_lower))
        if field_words:
            best_match = None
            best_score = 0
            
            for cf_name, cf_value in custom_fields.items():
                cf_words = set(re.findall(r'\w+', cf_name.lower()))
                if cf_words:
                    # Calculate Jaccard similarity (intersection over union)
                    intersection = len(field_words & cf_words)
                    union = len(field_words | cf_words)
                    score = intersection / union if union > 0 else 0
                    
                    # Require at least 50% similarity and at least one common word
                    if score > best_score and score >= 0.5 and intersection > 0:
                        best_match = (cf_name, cf_value)
                        best_score = score
            
            if best_match:
                result = self._format_field_value(best_match[1])
                logging.debug(f"Found fuzzy match for '{field_name}' -> '{best_match[0]}' (score: {best_score:.2f}): {result}")
                return result
                
        # 6. Fourth, look in other possible locations within the work item
        # DevRev might store custom fields in different locations
        for key in work.keys():
            if key != "custom_fields" and isinstance(work[key], dict):
                nested_dict = work[key]
                if field_name in nested_dict:
                    result = self._format_field_value(nested_dict[field_name])
                    logging.debug(f"Found field in nested location '{key}': {result}")
                    return result
                    
        # Nothing found - log available fields for debugging
        available_fields = list(custom_fields.keys())
        logging.debug(f"Field '{original_field_name}' not found. Available custom fields: {available_fields}")
        return None

    def _extract_all_custom_fields(self, work: Dict[str, Any]) -> Dict[str, str]:
        """Extract all custom fields from a DevRev work item.
        
        Args:
            work: DevRev work item dictionary
            
        Returns:
            Dictionary of custom field names and values
        """
        if not work or not isinstance(work, dict):
            return {}
            
        result = {}
        
        # Get custom fields from the standard location
        custom_fields = work.get("custom_fields", {})
        if custom_fields and isinstance(custom_fields, dict):
            for field_name, field_value in custom_fields.items():
                if field_value is not None:
                    formatted_value = self._format_field_value(field_value)
                    if formatted_value and formatted_value != "None":
                        result[field_name] = formatted_value
                        
        # Also check for custom fields that might be stored elsewhere in the work item
        # DevRev API might have different structures
        potential_custom_field_keys = ["customFields", "custom_data", "metadata", "properties"]
        for key in potential_custom_field_keys:
            if key in work and isinstance(work[key], dict):
                nested_fields = work[key]
                for field_name, field_value in nested_fields.items():
                    if field_value is not None and field_name not in result:
                        formatted_value = self._format_field_value(field_value)
                        if formatted_value and formatted_value != "None":
                            result[field_name] = formatted_value
                            
        return result

    def _get_custom_field_distributions(self, works: List[Dict[str, Any]], field_names: List[str]) -> Dict[str, Dict[str, int]]:
        """Get distribution of values for custom fields across works.
        
        Args:
            works: List of DevRev work items
            field_names: List of custom field names to analyze
            
        Returns:
            Dictionary of {field_name: {value: count}} distributions
        """
        result = {}
        
        for field_name in field_names:
            field_values = {}
            
            for work in works:
                try:
                    value = self._extract_custom_field_value(work, field_name)
                    if value:
                        field_values[value] = field_values.get(value, 0) + 1
                except Exception as e:
                    logging.debug(f"Error extracting custom field '{field_name}': {str(e)}")
            
            if field_values:
                result[field_name] = field_values
                
        return result

    def _collect_available_fields(self, works: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        """Collect information about available fields across all works.
        
        Args:
            works: List of DevRev work items
            
        Returns:
            Dictionary with lists of standard_fields and custom_fields
        """
        if not works:
            return {}
            
        standard_fields = set()
        custom_fields = set()
        
        for work in works:
            if isinstance(work, dict):
                # Collect standard fields
                for key in work.keys():
                    if key != "custom_fields" and not key.startswith("_"):
                        standard_fields.add(key)
                        
                # Collect custom fields
                work_custom_fields = work.get("custom_fields", {})
                if isinstance(work_custom_fields, dict):
                    for cf_name in work_custom_fields.keys():
                        custom_fields.add(cf_name)
                        
                # Also check other potential custom field locations
                potential_custom_field_keys = ["customFields", "custom_data", "metadata", "properties"]
                for key in potential_custom_field_keys:
                    if key in work and isinstance(work[key], dict):
                        for cf_name in work[key].keys():
                            custom_fields.add(f"{key}.{cf_name}")
        
        return {
            "standard_fields": sorted(list(standard_fields)),
            "custom_fields": sorted(list(custom_fields))
        }

    def _format_field_value(self, value: Any) -> str:
        """Format a field value as a string.
        
        Args:
            value: The field value object
            
        Returns:
            String representation of the value
        """
        if value is None:
            return "None"
            
        # Handle complex objects with name attributes (like SelectField values)
        if isinstance(value, dict):
            if 'name' in value:
                return value['name']
            elif 'value' in value:
                return str(value['value'])
            elif 'displayName' in value:
                return value['displayName']
            elif 'display_name' in value:
                return value['display_name']
            else:
                # For other dict types, try to find a meaningful representation
                for key in ['title', 'label', 'text', 'description']:
                    if key in value and value[key]:
                        return str(value[key])
                # Fall back to string representation of the dict
                return str(value)
                
        # Handle array/list types with multiple values
        if isinstance(value, (list, tuple)):
            if not value:
                return ""
            
            formatted_items = []
            for item in value:
                if isinstance(item, dict):
                    if 'name' in item:
                        formatted_items.append(item['name'])
                    elif 'value' in item:
                        formatted_items.append(str(item['value']))
                    elif 'displayName' in item:
                        formatted_items.append(item['displayName'])
                    else:
                        formatted_items.append(str(item))
                else:
                    formatted_items.append(str(item))
            return ", ".join(formatted_items)
        
        # Default to string representation
        return str(value)

    def _convert_custom_field_to_tag(self, field_name: str, field_value: str) -> str:
        """Convert a custom field and value to a DevRev-compatible tag.
        
        Args:
            field_name: The custom field name
            field_value: The custom field value
            
        Returns:
            Tag string that can be used for filtering in DevRev
        """
        if not field_name or not field_value:
            logging.warning(f"Empty field name or value: '{field_name}' = '{field_value}'")
            return ""
        
        # Clean up field name for tag format
        clean_field = field_name.strip('#"\'').replace(' ', '_').replace('(', '').replace(')', '').replace('-', '_').lower()
        
        # Handle special characters in field names
        clean_field = re.sub(r'[^\w_]', '_', clean_field)
        clean_field = re.sub(r'_+', '_', clean_field).strip('_')  # Remove multiple underscores and trailing
        
        # Clean up field value 
        clean_value = str(field_value).strip('"\'')
        
        # Handle different value formats
        if ',' in clean_value:
            # Multi-value field - take first value or create multiple tags
            values = [v.strip() for v in clean_value.split(',') if v.strip()]
            if values:
                clean_value = values[0]  # Use first value for primary tag
        
        # Normalize value for tag format
        if clean_value.upper() in ['YES', 'TRUE', '1']:
            clean_value = 'true'
        elif clean_value.upper() in ['NO', 'FALSE', '0']:
            clean_value = 'false'
        else:
            # Replace spaces and special chars in value
            clean_value = clean_value.replace(' ', '_').replace('-', '_').lower()
            clean_value = re.sub(r'[^\w_]', '_', clean_value)
            clean_value = re.sub(r'_+', '_', clean_value).strip('_')
        
        # Create tag in format: fieldname:value
        if clean_field and clean_value:
            tag = f"{clean_field}:{clean_value}"
            logging.info(f"Converted custom field '{field_name}' = '{field_value}' to tag: '{tag}'")
            return tag
        else:
            logging.warning(f"Could not create valid tag from field '{field_name}' = '{field_value}'")
            return ""

    def _apply_client_side_filters(self, work_items: List[Dict[str, Any]], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Apply client-side filtering for complex JQL queries that DevRev API cannot handle.
        
        Args:
            work_items: List of work items from DevRev API
            filters: Dictionary of client-side filters
            
        Returns:
            Filtered list of work items
        """
        if not work_items or not filters:
            return work_items
        
        filtered_items = work_items.copy()
        
        # Apply custom field filters
        custom_fields_filter = filters.get("custom_fields_filter", {})
        if custom_fields_filter:
            logging.info(f"Applying custom field filters: {custom_fields_filter}")
            new_filtered_items = []
            
            for work in filtered_items:
                include_work = True
                
                for field_name, expected_value in custom_fields_filter.items():
                    actual_value = self._extract_custom_field_value(work, field_name)
                    
                    if isinstance(expected_value, list):
                        # Handle IN operator
                        if actual_value not in expected_value:
                            include_work = False
                            break
                    else:
                        # Handle exact match
                        if actual_value != expected_value:
                            include_work = False
                            break
                
                if include_work:
                    new_filtered_items.append(work)
            
            filtered_items = new_filtered_items
            logging.info(f"After custom field filtering: {len(filtered_items)} items")
        
        # Apply custom field exclusion filters
        custom_fields_exclude = filters.get("custom_fields_exclude", {})
        if custom_fields_exclude:
            logging.info(f"Applying custom field exclusion filters: {custom_fields_exclude}")
            new_filtered_items = []
            
            for work in filtered_items:
                include_work = True
                
                for field_name, excluded_value in custom_fields_exclude.items():
                    actual_value = self._extract_custom_field_value(work, field_name)
                    
                    if actual_value == excluded_value:
                        include_work = False
                        break
                
                if include_work:
                    new_filtered_items.append(work)
            
            filtered_items = new_filtered_items
            logging.info(f"After custom field exclusion: {len(filtered_items)} items")
        
        # Apply standard field exclusion filters
        exclude_filters = filters.get("exclude_filters", {})
        if exclude_filters:
            logging.info(f"Applying exclusion filters: {exclude_filters}")
            new_filtered_items = []
            
            for work in filtered_items:
                include_work = True
                
                for field_name, excluded_value in exclude_filters.items():
                    actual_value = work.get(field_name)
                    
                    if actual_value == excluded_value:
                        include_work = False
                        break
                
                if include_work:
                    new_filtered_items.append(work)
            
            filtered_items = new_filtered_items
            logging.info(f"After exclusion filtering: {len(filtered_items)} items")
        
        # Apply text-based filters (for ~ and LIKE operators)
        text_filters = filters.get("text_filters", {})
        if text_filters:
            logging.info(f"Applying text filters: {text_filters}")
            new_filtered_items = []
            
            for work in filtered_items:
                include_work = True
                
                for field_name, search_text in text_filters.items():
                    actual_value = str(work.get(field_name, "")).lower()
                    search_text_lower = str(search_text).lower()
                    
                    if search_text_lower not in actual_value:
                        include_work = False
                        break
                
                if include_work:
                    new_filtered_items.append(work)
            
            filtered_items = new_filtered_items
            logging.info(f"After text filtering: {len(filtered_items)} items")
        
        return filtered_items

    def _extract_custom_fields_from_jql(self, jql: str) -> List[str]:
        """Extract custom field names from a JQL query string.
        
        Args:
            jql: The JQL query string
            
        Returns:
            List of custom field names found in the query
        """
        if not jql:
            return []
            
        custom_fields = []
        
        # Look for field names in quotes, especially those with special characters
        quoted_pattern = r'"([^"]+)"'
        quoted_fields = re.findall(quoted_pattern, jql)
        
        for field in quoted_fields:
            # Only include if it looks like a field name (before an operator)
            if re.search(rf'"{re.escape(field)}"[ ]*=', jql) or re.search(rf'"{re.escape(field)}"[ ]*!=', jql) or \
               re.search(rf'"{re.escape(field)}"[ ]*~', jql) or re.search(rf'"{re.escape(field)}"[ ]*IN', jql, re.IGNORECASE):
                custom_fields.append(field)
        
        # Look for customfield_XXXXX pattern
        customfield_pattern = r'(customfield_\d+)'
        customfields = re.findall(customfield_pattern, jql)
        custom_fields.extend(customfields)
        
        # Look for fields with hash prefix (common in DevRev custom fields)
        hash_pattern = r'#([A-Za-z0-9\s\(\)]+)'
        hash_fields = re.findall(hash_pattern, jql)
        for field in hash_fields:
            # Check if it appears before an operator
            if re.search(rf'#{re.escape(field)}[ ]*=', jql) or re.search(rf'#{re.escape(field)}[ ]*!=', jql):
                custom_fields.append(f"#{field}")
        
        return custom_fields