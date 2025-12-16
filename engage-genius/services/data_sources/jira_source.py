from typing import Dict, Any, List
from .base import DataSource
from jira import JIRA
import logging
from config import JIRA_SERVER, JIRA_EMAIL, JIRA_API_TOKEN
from datetime import datetime
from functools import lru_cache
import re

class JiraDataSource(DataSource):
    def __init__(self):
        self.client = JIRA(
            server=JIRA_SERVER,
            basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN)
        )
        
    async def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate JIRA-specific inputs"""
        try:
            # First check if the input appears to be a JQL query directly in project_key field
            # This happens when users paste a full JQL query in the project key field
            if 'project_key' in inputs:
                keys = inputs['project_key']
                # Check if it looks like a JQL query (has special keywords or operators)
                if isinstance(keys, str) and any(keyword in keys.upper() for keyword in [' AND ', ' OR ', '=', '<', '>', 'ORDER BY', 'PROJECT =']):
                    logging.info(f"Detected JQL query in project_key field: {keys}")
                    # Move it to jql_query field and validate as JQL
                    if 'jql_query' not in inputs or not inputs['jql_query'].strip():
                        inputs['jql_query'] = keys
                    
            # Check if JQL query is provided
            if 'jql_query' in inputs and inputs['jql_query'].strip():
                # Try to execute the JQL query to validate it
                try:
                    jql = inputs['jql_query'].strip()
                    logging.info(f"Validating JQL query: {jql}")
                    # Test query with just 1 result to validate syntax
                    self.client.search_issues(jql, maxResults=1)
                    logging.info("JQL query is valid")
                    return True
                except Exception as e:
                    logging.error(f"Invalid JQL query: {str(e)}")
                    return False
            
            # If no JQL, fall back to project key validation
            # Collect board IDs from multiple possible field names
            boards = []
            
            # Check for project_key field first (newer format)
            if 'project_key' in inputs:
                keys = inputs['project_key']
                if isinstance(keys, list):
                    # Process each key in the list
                    for key in keys:
                        if isinstance(key, str) and ',' in key:
                            # Split comma-separated keys in a string
                            parts = [k.strip() for k in key.split(',') if k.strip()]
                            boards.extend(parts)
                        else:
                            boards.append(key)
                elif isinstance(keys, str):
                    # Split comma-separated string
                    boards.extend([k.strip() for k in keys.split(',') if k.strip()])
                else:
                    boards.append(keys)
            # Fall back to board field (older format)
            elif 'board' in inputs:
                board = inputs['board']
                if isinstance(board, list):
                    boards.extend(board)
                else:
                    boards.append(board)
            elif 'boards' in inputs:
                board_list = inputs['boards']
                if isinstance(board_list, list):
                    boards.extend(board_list)
                else:
                    boards.append(board_list)
            
            # Filter out empty values
            boards = [b for b in boards if b]
            
            if not boards:
                logging.error("No JIRA project specified and no JQL query provided")
                return False
            
            # First get all available projects for better error messages
            all_projects = self.client.projects()
            available_project_keys = [project.key for project in all_projects]
            available_project_ids = [project.id for project in all_projects]
            
            logging.info(f"Available project keys: {available_project_keys}")
            
            # Now check if the provided board IDs exist
            valid_boards = []
            for board_id in boards:
                # Check if board_id might actually be a JQL query
                if isinstance(board_id, str) and any(keyword in board_id.upper() for keyword in [' AND ', ' OR ', '=', '<', '>', 'ORDER BY', 'PROJECT =']):
                    logging.info(f"Board ID '{board_id}' appears to be a JQL query, attempting to validate as JQL")
                    try:
                        # Try to execute it as a JQL query
                        self.client.search_issues(board_id, maxResults=1)
                        logging.info(f"Successfully validated as JQL query: {board_id}")
                        # Add to jql_query field for fetch_data to use
                        inputs['jql_query'] = board_id
                        return True
                    except Exception as e:
                        logging.error(f"Failed to validate as JQL query: {e}")
                        # Continue to try as project key
                
                # Check if it's a valid project key or ID
                if board_id in available_project_keys:
                    logging.info(f"Project key {board_id} is valid")
                    valid_boards.append(board_id)
                elif str(board_id) in available_project_ids:
                    logging.info(f"Project ID {board_id} is valid")
                    valid_boards.append(board_id)
                else:
                    # Try to match by partial key or name
                    matching_projects = []
                    for project in all_projects:
                        if (board_id.lower() in project.key.lower() or 
                            (hasattr(project, 'name') and board_id.lower() in project.name.lower())):
                            matching_projects.append(project.key)
                    
                    if matching_projects:
                        logging.warning(f"Project '{board_id}' not found. Did you mean one of these? {', '.join(matching_projects)}")
                    else:
                        logging.warning(f"Project '{board_id}' not found. Available projects: {', '.join(available_project_keys[:5])}...")
            
            return len(valid_boards) > 0
            
        except Exception as e:
            logging.error(f"Error validating Jira project(s): {str(e)}")
            return False
    
    async def fetch_data(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch data from JIRA.
        
        Args:
            params: Dictionary of parameters including:
                - project_key: JIRA project key or a list of project keys
                - jql_query: Custom JQL query to use instead of project_key
                - days: Optional days to look back (int)
                
        Returns:
            Dictionary of JIRA data organized by project or by query
        """
        try:
            # First check if a project_key contains a JQL query
            if 'project_key' in params:
                project_key_param = params['project_key']
                
                # Check if it's a string that looks like a JQL query
                if isinstance(project_key_param, str) and any(keyword in project_key_param.upper() for keyword in [' AND ', ' OR ', '=', '<', '>', 'ORDER BY', 'PROJECT =']):
                    logging.info(f"Detected JQL query in project_key field: {project_key_param}")
                    # Move it to jql_query field if no JQL query is already specified
                    if 'jql_query' not in params or not params['jql_query'].strip():
                        params['jql_query'] = project_key_param
                
                # If it's a list, check each item
                elif isinstance(project_key_param, list):
                    for i, key in enumerate(project_key_param):
                        if isinstance(key, str) and any(keyword in key.upper() for keyword in [' AND ', ' OR ', '=', '<', '>', 'ORDER BY', 'PROJECT =']):
                            logging.info(f"Detected JQL query in project_key[{i}]: {key}")
                            # Move it to jql_query field if no JQL query is already specified
                            if 'jql_query' not in params or not params['jql_query'].strip():
                                params['jql_query'] = key
                                break
            
            # Check if JQL query is provided
            if 'jql_query' in params and params['jql_query'].strip():
                jql = params['jql_query'].strip()
                logging.info(f"Using custom JQL query: {jql}")
                
                # Set search parameters - ensure we're not passing 'jql' as a keyword argument
                # Use "*all" to fetch all fields including custom fields
                search_params = {
                    "maxResults": 50,  # Limit to 50 issues for performance
                    "fields": "*all"   # Fetch all fields including custom fields
                }
                
                try:
                    # Execute API call with jql as positional argument
                    logging.info(f"Executing JQL query: {jql}")
                    
                    # Implement pagination to fetch all results
                    all_issues = []
                    start_at = 0
                    max_results = 100  # JIRA's maximum per call

                    while True:
                        response = self.client.search_issues(
                            jql,
                            startAt=start_at,
                            maxResults=max_results,
                            fields="*all"  # to include all standard + custom fields
                        )
                        if not response:
                            break
                        all_issues.extend(response)
                        logging.info(f"Fetched {len(response)} issues, total so far: {len(all_issues)}")
                        if len(response) < max_results:
                            break
                        start_at += max_results
                    
                    # Use all_issues as the response for further processing
                    response = all_issues
                    
                    # Log total number of results
                    logging.info(f"JQL query returned {len(response)} issues after pagination")
                    
                    # Extract JQL query components for better understanding
                    query_components = self._parse_jql_query(jql)
                    logging.info(f"JQL query components: {query_components}")
                    
                    # Verify each issue has the required attributes and log details for troubleshooting
                    valid_issues = []
                    partial_issues = []  # For issues that may be partially valid
                    
                    for issue in response:
                        try:
                            # First, try to access the raw structure to log what's available
                            if logging.getLogger().level <= logging.DEBUG:
                                try:
                                    raw_attrs = dir(issue)
                                    logging.debug(f"Raw issue attributes: {raw_attrs}")
                                    # Log available fields if fields attribute exists
                                    if hasattr(issue, 'fields'):
                                        field_attrs = dir(issue.fields)
                                        custom_fields = [f for f in field_attrs if f.startswith('customfield_')]
                                        logging.debug(f"Custom fields found: {custom_fields}")
                                except Exception as e:
                                    logging.debug(f"Error examining raw issue structure: {str(e)}")
                            
                            # Check if issue has key attribute - this is our minimum requirement
                            if hasattr(issue, 'key'):
                                # If issue has fields attribute, consider it valid even if some fields are missing
                                if hasattr(issue, 'fields'):
                                    valid_issues.append(issue)
                                else:
                                    # Issues with key but no fields are partial - we can still display some info
                                    logging.warning(f"Issue {issue.key} has no fields attribute but will be included")
                                    partial_issues.append(issue)
                            else:
                                # Try to extract any useful information for debugging
                                logging.warning(f"Issue without key attribute detected in project {project_key}")
                                if hasattr(issue, 'id'):
                                    logging.warning(f"Issue has ID: {issue.id} but no key")
                                    partial_issues.append(issue)  # Include issues with ID but no key
                                elif hasattr(issue, 'self'):
                                    logging.warning(f"Issue has self URL: {issue.self} but no key")
                                    partial_issues.append(issue)  # Include issues with self URL but no key
                                else:
                                    # Log detailed structure for debugging
                                    if logging.getLogger().level <= logging.DEBUG:
                                        issue_structure = self._dump_issue_structure(issue)
                                        logging.debug(f"Invalid issue structure in project {project_key}: {issue_structure}")
                        except Exception as issue_err:
                            logging.warning(f"Error validating issue in project {project_key}: {str(issue_err)}")
                            # Try to get any useful information we can
                            try:
                                if hasattr(issue, 'id') or hasattr(issue, 'key') or hasattr(issue, 'self'):
                                    logging.warning("Issue has some identifiable attributes and will be included despite errors")
                                    partial_issues.append(issue)
                            except Exception:
                                pass
                                
                            if logging.getLogger().level <= logging.DEBUG:
                                try:
                                    issue_structure = self._dump_issue_structure(issue)
                                    logging.debug(f"Issue with error structure in project {project_key}: {issue_structure}")
                                except Exception:
                                    logging.debug(f"Could not dump issue structure in project {project_key}")
                    
                    # Include partial issues if we don't have any valid ones
                    if not valid_issues and partial_issues:
                        logging.info(f"Project {project_key}: No fully valid issues found, but {len(partial_issues)} partial issues will be used")
                        valid_issues = partial_issues
                    
                    # Log stats about what we found
                    if len(valid_issues) < len(response):
                        invalid_count = len(response) - len(valid_issues)
                        partial_count = len([i for i in valid_issues if i in partial_issues])
                        logging.warning(f"Project {project_key}: Found {invalid_count} invalid issues and {partial_count} partial issues out of {len(response)} total")
                        
                        # Log the first invalid issue in detail at warning level for easier debugging
                        for issue in response:
                            if issue not in valid_issues and issue not in partial_issues:
                                logging.warning(f"Example invalid issue structure in project {project_key}: {self._dump_issue_structure(issue)}")
                                break
                    
                    # Process and store the result
                    issues_count = len(valid_issues)
                    query = params.get('query', '').strip().lower()
                    
                    # Calculate relevance based on query match
                    query_matches = 0
                    if query:
                        for issue in valid_issues:
                            try:
                                summary = getattr(issue.fields, 'summary', '')
                                description = getattr(issue.fields, 'description', '')
                                issue_text = f"{summary} {description}".lower()
                                if query in issue_text:
                                    query_matches += 1
                            except Exception as e:
                                logging.warning(f"Error processing issue for query matching: {str(e)}")
                                
                    # Calculate relevance score
                    relevance = 0.5  # Default relevance
                    if issues_count > 0 and query:
                        match_percentage = query_matches / issues_count
                        relevance = 0.5 + (match_percentage * 0.5)  # Scale from 0.5 to 1.0
                    
                    results = {
                        "jql_custom_query": {
                            "issues": valid_issues,
                            "total": issues_count,
                            "relevance": relevance,
                            "metadata": {
                                "query_matches": query_matches,
                                "issues_count": issues_count,
                                "total_fetched": len(response),
                                "invalid_issues": len(response) - len(valid_issues),
                                "partial_issues": len([i for i in valid_issues if i in partial_issues]),
                                "jql_query": jql,
                                "processing_time": str(datetime.now())
                            }
                        },
                        "_metadata": {
                            "query_type": "custom_jql",
                            "jql": jql,
                            "total_issues": issues_count,
                            "query_matches": query_matches,
                            "invalid_issues": len(response) - len(valid_issues),
                            "partial_issues": len([i for i in valid_issues if i in partial_issues])
                        }
                    }
                except Exception as query_err:
                    logging.error(f"Error executing JQL query: {str(query_err)}")
                    results = {
                        "jql_custom_query": {
                            "error": f"Error executing JQL query: {str(query_err)}",
                            "relevance": 0.2,
                            "issues": [],
                            "total": 0,
                            "metadata": {
                                "jql_query": jql,
                                "error": str(query_err)
                            }
                        },
                        "_metadata": {
                            "query_type": "custom_jql",
                            "jql": jql,
                            "error": str(query_err)
                        }
                    }
                
                return results
            
            # If no JQL provided, use project key approach
            # Get JIRA project keys (support both single string and list)
            project_keys = []
            if 'project_key' in params:
                project_key_param = params['project_key']
                if isinstance(project_key_param, list):
                    # Process each key in the list
                    for key in project_key_param:
                        if isinstance(key, str) and ',' in key:
                            # Handle comma-separated values
                            parts = [k.strip() for k in key.split(',') if k.strip()]
                            project_keys.extend(parts)
                        else:
                            project_keys.append(key)
                elif isinstance(project_key_param, str):
                    # Handle comma-separated string
                    project_keys.extend([k.strip() for k in project_key_param.split(',') if k.strip()])
                else:
                    # Handle single project key
                    project_keys.append(project_key_param)
            
            if not project_keys:
                return {"error": "No JIRA project key or JQL query provided"}
                
            # Get days parameter (optional)
            days = None
            if 'days' in params:
                try:
                    days = int(params['days'])
                except (ValueError, TypeError):
                    pass
                    
            # Log what we're processing
            logging.info(f"Fetching data for {len(project_keys)} JIRA project(s): {project_keys}")
            if days:
                logging.info(f"Limiting to issues updated in the last {days} days")
            else:
                logging.info("No time limit applied for JIRA queries")
                
            # Container for results
            results = {}
            project_relevance = {}  # Track relevance scores per project
            query = params.get('query', '').strip().lower()
            
            # Process each project
            for project_key in project_keys:
                try:
                    logging.info(f"Processing JIRA project: {project_key}")
                    
                    # Validate project key exists
                    if not self._validate_project_key(project_key):
                        results[project_key] = {
                            "error": f"Invalid project key: {project_key}",
                            "relevance": 0.1  # Very low relevance for invalid projects
                        }
                        continue
                    
                    # Use the board_id as the project key for the query
                    logging.info(f"Using board_id as project key: {project_key}")
                    
                    # Build JQL query
                    jql = f"project = {project_key}"
                    
                    # Add time constraint if days parameter is provided
                    if days:
                        jql += f" AND updated >= -{days}d"
                    
                    # Implement pagination to fetch all results
                    all_issues = []
                    start_at = 0
                    max_results = 100  # JIRA's maximum per call

                    while True:
                        logging.info(f"Fetching issues for project {project_key}, startAt={start_at}, maxResults={max_results}")
                        batch = self.client.search_issues(
                            jql,
                            startAt=start_at,
                            maxResults=max_results,
                            fields="*all"  # to include all standard + custom fields
                        )
                        if not batch:
                            break
                        all_issues.extend(batch)
                        logging.info(f"Fetched {len(batch)} issues for project {project_key}, total so far: {len(all_issues)}")
                        if len(batch) < max_results:
                            break
                        start_at += max_results
                    
                    # Use all_issues as the response for further processing
                    response = all_issues
                    
                    # Log total number of results
                    logging.info(f"Project {project_key} query returned {len(response)} issues after pagination")
                    
                    # Validate response data
                    valid_issues = []
                    partial_issues = []  # For issues that may be partially valid
                    
                    for issue in response:
                        try:
                            # First, try to access the raw structure to log what's available
                            if logging.getLogger().level <= logging.DEBUG:
                                try:
                                    raw_attrs = dir(issue)
                                    logging.debug(f"Raw issue attributes: {raw_attrs}")
                                    # Log available fields if fields attribute exists
                                    if hasattr(issue, 'fields'):
                                        field_attrs = dir(issue.fields)
                                        custom_fields = [f for f in field_attrs if f.startswith('customfield_')]
                                        logging.debug(f"Custom fields found: {custom_fields}")
                                except Exception as e:
                                    logging.debug(f"Error examining raw issue structure: {str(e)}")
                            
                            # Check if issue has key attribute - this is our minimum requirement
                            if hasattr(issue, 'key'):
                                # If issue has fields attribute, consider it valid even if some fields are missing
                                if hasattr(issue, 'fields'):
                                    valid_issues.append(issue)
                                else:
                                    # Issues with key but no fields are partial - we can still display some info
                                    logging.warning(f"Issue {issue.key} has no fields attribute but will be included")
                                    partial_issues.append(issue)
                            else:
                                # Try to extract any useful information for debugging
                                logging.warning(f"Issue without key attribute detected in project {project_key}")
                                if hasattr(issue, 'id'):
                                    logging.warning(f"Issue has ID: {issue.id} but no key")
                                    partial_issues.append(issue)  # Include issues with ID but no key
                                elif hasattr(issue, 'self'):
                                    logging.warning(f"Issue has self URL: {issue.self} but no key")
                                    partial_issues.append(issue)  # Include issues with self URL but no key
                                else:
                                    # Log detailed structure for debugging
                                    if logging.getLogger().level <= logging.DEBUG:
                                        issue_structure = self._dump_issue_structure(issue)
                                        logging.debug(f"Invalid issue structure in project {project_key}: {issue_structure}")
                        except Exception as issue_err:
                            logging.warning(f"Error validating issue in project {project_key}: {str(issue_err)}")
                            # Try to get any useful information we can
                            try:
                                if hasattr(issue, 'id') or hasattr(issue, 'key') or hasattr(issue, 'self'):
                                    logging.warning("Issue has some identifiable attributes and will be included despite errors")
                                    partial_issues.append(issue)
                            except Exception:
                                pass
                                
                            if logging.getLogger().level <= logging.DEBUG:
                                try:
                                    issue_structure = self._dump_issue_structure(issue)
                                    logging.debug(f"Issue with error structure in project {project_key}: {issue_structure}")
                                except Exception:
                                    logging.debug(f"Could not dump issue structure in project {project_key}")
                    
                    # Include partial issues if we don't have any valid ones
                    if not valid_issues and partial_issues:
                        logging.info(f"Project {project_key}: No fully valid issues found, but {len(partial_issues)} partial issues will be used")
                        valid_issues = partial_issues
                    
                    # Log stats about what we found
                    if len(valid_issues) < len(response):
                        invalid_count = len(response) - len(valid_issues)
                        partial_count = len([i for i in valid_issues if i in partial_issues])
                        logging.warning(f"Project {project_key}: Found {invalid_count} invalid issues and {partial_count} partial issues out of {len(response)} total")
                        
                        # Log the first invalid issue in detail at warning level for easier debugging
                        for issue in response:
                            if issue not in valid_issues and issue not in partial_issues:
                                logging.warning(f"Example invalid issue structure in project {project_key}: {self._dump_issue_structure(issue)}")
                                break
                    
                    # Calculate basic project relevance based on number of issues and matches to query
                    issues_count = len(valid_issues)
                    query_matches = 0
                    if query:
                        for issue in valid_issues:
                            try:
                                summary = getattr(issue.fields, 'summary', '')
                                description = getattr(issue.fields, 'description', '')
                                issue_text = f"{summary} {description}".lower()
                                if query in issue_text:
                                    query_matches += 1
                            except Exception as e:
                                logging.warning(f"Error processing issue for query matching: {str(e)}")
                    
                    # Calculate relevance score (0.3-1.0 range)
                    if issues_count == 0:
                        relevance = 0.3  # Base relevance for empty projects
                    else:
                        # Start with a base relevance of 0.5
                        relevance = 0.5
                        
                        # If there's a query, adjust relevance based on matches
                        if query and issues_count > 0:
                            match_percentage = query_matches / issues_count
                            relevance = 0.5 + (match_percentage * 0.5)  # Scale from 0.5 to 1.0
                    
                    # Store project relevance for later use
                    project_relevance[project_key] = relevance
                    
                    # Process and store the result with metadata
                    results[project_key] = {
                        "issues": valid_issues,
                        "total": len(valid_issues),
                        "relevance": relevance,
                        "metadata": {
                            "query_matches": query_matches,
                            "issues_count": issues_count,
                            "total_fetched": len(response),
                            "invalid_issues": len(response) - len(valid_issues),
                            "partial_issues": len([i for i in valid_issues if i in partial_issues]),
                            "processing_time": str(datetime.now())
                        }
                    }
                    
                    # Log the raw structure of the first issue for debugging
                    if len(response) > 0 and logging.getLogger().level <= logging.DEBUG:
                        first_issue = response[0]
                        logging.debug(f"First issue structure:")
                        try:
                            if hasattr(first_issue, 'key'):
                                logging.debug(f"Issue key: {first_issue.key}")
                            
                            raw_attrs = dir(first_issue)
                            logging.debug(f"Raw attributes: {[a for a in raw_attrs if not a.startswith('_')]}")
                            
                            if hasattr(first_issue, 'fields'):
                                logging.debug(f"Fields: {[f for f in dir(first_issue.fields) if not f.startswith('_') and not callable(getattr(first_issue.fields, f))]}")
                        except Exception as e:
                            logging.debug(f"Error examining first issue: {str(e)}")
                except Exception as e:
                    logging.error(f"Error processing JIRA project {project_key}: {str(e)}")
                    results[project_key] = {
                        "error": f"Error fetching project data: {str(e)}",
                        "relevance": 0.2  # Low relevance for error cases
                    }
            
            # Normalize relevance scores across projects
            if len(project_keys) > 1:
                max_relevance = max(project_relevance.values())
                if max_relevance > 0:
                    for project_key in project_relevance:
                        normalized_relevance = (project_relevance[project_key] / max_relevance) * 0.9 + 0.1
                        if project_key in results and isinstance(results[project_key], dict):
                            results[project_key]['relevance'] = normalized_relevance
            
            # Add global metadata
            results['_metadata'] = {
                "query_type": "project_based",
                "projects_processed": len(project_keys),
                "projects_succeeded": sum(1 for p in project_keys if p in results and "error" not in results[p]),
                "projects_failed": sum(1 for p in project_keys if p in results and "error" in results[p]),
                "total_issues": sum(results[p].get("total", 0) for p in project_keys if p in results and isinstance(results[p], dict) and "total" in results[p]),
                "query_matches": sum(results[p].get("metadata", {}).get("query_matches", 0) for p in project_keys if p in results and isinstance(results[p], dict) and "metadata" in results[p]),
                "project_relevance": project_relevance
            }
            
            return results
            
        except Exception as e:
            logging.error(f"Error in JIRA fetch_data: {str(e)}")
            return {"error": f"Failed to fetch JIRA data: {str(e)}"}
    
    def format_for_llm(self, data: Dict[str, Any]) -> str:
        """Format JIRA data for LLM consumption.
        
        Args:
            data: Dictionary containing JIRA project data
            
        Returns:
            Formatted string ready for LLM context
        """
        if not data or not isinstance(data, dict):
            return "No JIRA data available"
            
        if "error" in data:
            return f"Error retrieving JIRA data: {data['error']}"
            
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
                f"JIRA Data Summary: {projects_processed} Projects with {total_issues} total issues",
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
                f"JIRA Data from JQL Query: {jql}",
                f"Total Issues: {total_issues}"
            ]
            formatted_sections.append("\n".join(summary))
            formatted_sections.append("-" * 40)
        
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
            is_jql_query = project_key == "jql_custom_query"
            
            # Format header with relevance
            if is_jql_query:
                jql = metadata.get('jql', project_data.get('metadata', {}).get('jql_query', 'Custom JQL Query'))
                formatted_sections.append(f"## JQL QUERY: {jql} (Relevance: {relevance:.2f})")
            else:
                formatted_sections.append(f"## PROJECT: {project_key} (Relevance: {relevance:.2f})")
            
            # Handle issues
            issues = []
            if isinstance(project_data, dict) and "issues" in project_data:
                issues = project_data["issues"]
            elif isinstance(project_data, list):
                issues = project_data
            
            # Add metadata about invalid issues if available
            invalid_issues_count = 0
            partial_issues_count = 0
            if isinstance(project_data, dict) and "metadata" in project_data:
                meta = project_data["metadata"]
                invalid_issues_count = meta.get("invalid_issues", 0)
                partial_issues_count = meta.get("partial_issues", 0)
                if invalid_issues_count > 0:
                    formatted_sections.append(f"Note: {invalid_issues_count} issues had invalid format and were filtered out.")
                if partial_issues_count > 0:
                    formatted_sections.append(f"Note: {partial_issues_count} issues had partial data but were included.")
                
            if not issues:
                if invalid_issues_count > 0:
                    # If we have invalid issues but no valid ones, provide clearer messaging
                    formatted_sections.append("All issues returned from this query had invalid format. This might indicate one of these issues:")
                    formatted_sections.append("1. The JQL query uses custom fields with special characters (like # or spaces)")
                    formatted_sections.append("2. The issues contain custom fields that need special handling in the API")
                    formatted_sections.append("3. The issues may have an unusual structure (like Epics or Portfolio items)")
                    formatted_sections.append("\nRecommendations:")
                    formatted_sections.append("- Verify the JQL syntax in JIRA's native interface")
                    formatted_sections.append("- Try using field IDs instead of display names in JQL (e.g., customfield_10001 instead of \"#Custom Field\")")
                    formatted_sections.append("- For complex queries, consider breaking them into smaller parts")
                else:
                    formatted_sections.append("No valid issues found for this query.")
                formatted_sections.append("-" * 40)
                continue
                
            # Add issues count
            formatted_sections.append(f"Total Issues: {len(issues)}")
            
            # Extract custom field names from JQL query to know what to display
            custom_field_names = self._extract_custom_fields_from_jql(jql)
            if custom_field_names:
                formatted_sections.append(f"Custom fields in query: {', '.join(custom_field_names)}")
            
            # Try to gather distribution of values for custom fields in the query
            if custom_field_names and issues:
                custom_field_values = self._get_custom_field_distributions(issues, custom_field_names)
                if custom_field_values:
                    formatted_sections.append("\nCustom Field Distributions:")
                    for field_name, values in custom_field_values.items():
                        formatted_sections.append(f"- {field_name}:")
                        for value, count in values.items():
                            formatted_sections.append(f"  - {value}: {count}")
            
            # Collect all available fields from issues to understand what data we have
            all_available_fields = self._collect_available_fields(issues)
            if all_available_fields:
                standard_fields = all_available_fields.get("standard_fields", [])
                custom_fields = all_available_fields.get("custom_fields", [])
                
                # Include information about available fields
                if standard_fields:
                    formatted_sections.append("\nStandard fields available: " + ", ".join(standard_fields))
                if custom_fields:
                    formatted_sections.append("Custom fields available: " + ", ".join(custom_fields[:10]) + 
                                             (f" and {len(custom_fields) - 10} more..." if len(custom_fields) > 10 else ""))
            
            # Status distribution
            status_counts = {}
            for issue in issues:
                try:
                    if hasattr(issue, 'fields') and hasattr(issue.fields, 'status'):
                        status = getattr(issue.fields.status, 'name', 'Unknown')
                        status_counts[status] = status_counts.get(status, 0) + 1
                except Exception:
                    status_counts['Unknown'] = status_counts.get('Unknown', 0) + 1
            
            if status_counts:
                formatted_sections.append("\nStatus Distribution:")
                for status, count in status_counts.items():
                    formatted_sections.append(f"- {status}: {count}")
            
            # Format issues (top 10 most recently updated)
            formatted_sections.append("\nRecent Issues:")
            
            # Sort by updated date if possible
            try:
                sorted_issues = sorted(
                    issues,
                    key=lambda issue: getattr(getattr(issue, 'fields', None), 'updated', ''),
                    reverse=True
                )[:10]  # Limit to top 10
            except Exception:
                sorted_issues = issues[:10]  # Fallback if sorting fails
                logging.warning("Failed to sort issues by updated date")
            
            # Format each issue
            for issue in sorted_issues:
                try:
                    # Extract minimal information if only partial data is available
                    if not hasattr(issue, 'key'):
                        # Try to get any ID information
                        issue_id = getattr(issue, 'id', 'Unknown-ID')
                        formatted_issue = [f"- Issue {issue_id} (Partial data available)"]
                        
                        # Try to extract any available attributes for partial issues
                        raw_attrs = {}
                        try:
                            # Get all direct attributes from the issue object
                            for attr_name in dir(issue):
                                if attr_name.startswith('_') or callable(getattr(issue, attr_name)):
                                    continue
                                try:
                                    value = getattr(issue, attr_name)
                                    if value is not None:
                                        raw_attrs[attr_name] = value
                                except Exception:
                                    pass
                            
                            # If the issue has 'raw' attribute, try to extract more from it
                            if hasattr(issue, 'raw') and isinstance(issue.raw, dict):
                                for key, value in issue.raw.items():
                                    if value is not None and key not in raw_attrs:
                                        raw_attrs[key] = value
                                        
                            # Special handling for fields attribute
                            if hasattr(issue, 'fields'):
                                fields_obj = issue.fields
                                for field_name in dir(fields_obj):
                                    if field_name.startswith('_') or callable(getattr(fields_obj, field_name)):
                                        continue
                                    try:
                                        value = getattr(fields_obj, field_name)
                                        if value is not None:
                                            raw_attrs[f"fields.{field_name}"] = value
                                    except Exception:
                                        pass
                        except Exception as e:
                            formatted_issue.append(f"  Error extracting attributes: {str(e)}")
                        
                        # Format raw attributes for display
                        if raw_attrs:
                            # First, show ID-like fields that might help with identification
                            id_fields = []
                            for name, value in raw_attrs.items():
                                if any(id_term in name.lower() for id_term in ['key', 'id', 'self']):
                                    id_fields.append(f"{name}: {value}")
                            if id_fields:
                                formatted_issue.append(f"  Identifiers: {' | '.join(id_fields)}")
                            
                            # Then show custom fields related to the JQL query
                            query_fields = []
                            for name, value in raw_attrs.items():
                                if custom_field_names and any(cf.lower() in name.lower() for cf in custom_field_names):
                                    query_fields.append(f"{name}: {value}")
                            if query_fields:
                                formatted_issue.append(f"  Query-Related Fields: {' | '.join(query_fields)}")
                            
                            # Then show status, summary, etc.
                            common_fields = []
                            for field in ['status', 'summary', 'description', 'assignee']:
                                if field in raw_attrs:
                                    common_fields.append(f"{field}: {raw_attrs[field]}")
                                elif f"fields.{field}" in raw_attrs:
                                    common_fields.append(f"{field}: {raw_attrs[f'fields.{field}']}")
                            if common_fields:
                                formatted_issue.append(f"  Common Fields: {' | '.join(common_fields)}")
                            
                            # Finally, show other fields that might be useful
                            other_fields = []
                            for name, value in raw_attrs.items():
                                if 'customfield_' in name:
                                    # Skip if we've already included in query fields
                                    if not any(name in qf for qf in query_fields):
                                        other_fields.append(f"{name}: {value}")
                                        if len(other_fields) >= 3:  # Limit to 3 additional fields
                                            break
                            if other_fields:
                                formatted_issue.append(f"  Other Fields: {' | '.join(other_fields)}")
                                
                            # Add a note about raw data being available
                            formatted_issue.append("  Note: Issue has partial data structure. All available data has been shown.")
                        else:
                            formatted_issue.append("  No additional data could be extracted from this issue.")
                                
                        formatted_sections.append("\n".join(formatted_issue))
                        continue
                        
                    issue_key = getattr(issue, 'key', 'Unknown-ID')
                    
                    # Handle case where fields attribute may be missing
                    if not hasattr(issue, 'fields'):
                        formatted_issue = [f"- {issue_key} (Limited data: no fields attribute)"]
                        
                        # Try to extract any direct attributes of the issue
                        raw_attrs = {}
                        for attr_name in dir(issue):
                            if attr_name.startswith('_') or callable(getattr(issue, attr_name)):
                                continue
                            try:
                                value = getattr(issue, attr_name)
                                if value is not None and attr_name != 'key':
                                    raw_attrs[attr_name] = str(value)[:100]
                            except Exception:
                                pass
                                
                        if raw_attrs:
                            attr_pairs = []
                            for name, value in raw_attrs.items():
                                attr_pairs.append(f"{name}: {value}")
                            formatted_issue.append("  Available attributes: " + " | ".join(attr_pairs[:5]))
                            
                            # Try to access raw data if available
                            if hasattr(issue, 'raw') and isinstance(issue.raw, dict):
                                raw_data_preview = []
                                for k, v in list(issue.raw.items())[:5]:  # Limit to first 5 items
                                    if v is not None:
                                        raw_data_preview.append(f"{k}: {str(v)[:50]}")
                                if raw_data_preview:
                                    formatted_issue.append("  Raw data: " + " | ".join(raw_data_preview))
                                
                            formatted_sections.append("\n".join(formatted_issue))
                            continue
                        
                        # Access required fields safely with fallbacks
                        summary = self._safe_get_field(issue, 'summary', 'No summary')
                        status_obj = self._safe_get_field(issue, 'status')
                        status = getattr(status_obj, 'name', 'Unknown') if status_obj else 'Unknown'
                        
                        issuetype_obj = self._safe_get_field(issue, 'issuetype')
                        issue_type = getattr(issuetype_obj, 'name', 'Unknown') if issuetype_obj else 'Unknown'
                        
                        assignee_obj = self._safe_get_field(issue, 'assignee')
                        assignee = getattr(assignee_obj, 'displayName', 'Unassigned') if assignee_obj else 'Unassigned'
                        
                        formatted_issue = [
                            f"- {issue_key}: {summary}",
                            f"  Type: {issue_type} | Status: {status} | Assignee: {assignee}"
                        ]
                        
                        # Extract and display ALL custom fields available (not just from JQL)
                        custom_field_values = self._extract_all_custom_fields(issue)
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
                                formatted_issue.append(f"  Query Fields: {' | '.join(prioritized_fields)}")
                            if other_fields:
                                formatted_issue.append(f"  Other Custom Fields: {' | '.join(other_fields)}")
                        
                        # Add description snippet if available
                        description = self._safe_get_field(issue, 'description', '')
                        if description:
                            # Truncate long descriptions
                            desc_preview = description[:100] + ('...' if len(description) > 100 else '')
                            # Clean up newlines for better formatting
                            desc_preview = ' '.join(desc_preview.split())
                            formatted_issue.append(f"  Description: {desc_preview}")
                        
                        # Check for components
                        try:
                            components = self._safe_get_field(issue, 'components', [])
                            if components:
                                component_names = []
                                for comp in components:
                                    if hasattr(comp, 'name'):
                                        component_names.append(comp.name)
                                if component_names:
                                    formatted_issue.append(f"  Components: {', '.join(component_names)}")
                        except Exception:
                            pass
                        
                        # Check for labels
                        try:
                            labels = self._safe_get_field(issue, 'labels', [])
                            if labels:
                                formatted_issue.append(f"  Labels: {', '.join(labels[:5])}" + 
                                                     (f" and {len(labels) - 5} more" if len(labels) > 5 else ""))
                        except Exception:
                            pass
                            
                        # Include links
                        if hasattr(issue, 'self'):
                            formatted_issue.append(f"  Link: {issue.self}")
                            
                        # Check for comments
                        try:
                            comments_obj = self._safe_get_field(issue, 'comment')
                            comments = getattr(comments_obj, 'comments', []) if comments_obj else []
                            if comments:
                                formatted_issue.append(f"  Comments: {len(comments)}")
                                if len(comments) > 0:
                                    # Include latest comment
                                    latest = comments[0]
                                    author = getattr(getattr(latest, 'author', None), 'displayName', 'Unknown')
                                    body = getattr(latest, 'body', '')
                                    if body:
                                        body_preview = body[:100] + ('...' if len(body) > 100 else '')
                                        body_preview = ' '.join(body_preview.split())
                                        formatted_issue.append(f"  Latest comment by {author}: {body_preview}")
                        except Exception:
                            pass
                            
                        formatted_sections.append("\n".join(formatted_issue))
                        
                except Exception as e:
                    logging.error(f"Error formatting issue: {str(e)}")
                    formatted_sections.append(f"- Error formatting issue {getattr(issue, 'key', 'unknown')}: {str(e)}")
            
            # Add separator between projects
            formatted_sections.append("-" * 40)
        
        return "\n".join(formatted_sections)
    
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
            "conditions": []
        }
        
        # Extract project information
        project_pattern = r'\bproject\s*=\s*(\w+)'
        projects = re.findall(project_pattern, jql, re.IGNORECASE)
        result["projects"] = projects
        
        # Extract field names in quotes (often custom fields)
        quoted_fields = re.findall(r'"([^"]+)"', jql)
        for field in quoted_fields:
            # Check if it looks like a field name (before an operator)
            if any(re.search(rf'"{re.escape(field)}"[ ]*{op}', jql) for op in ['=', '!=', '~', 'IN', '>', '<']):
                result["custom_fields"].append(field)
                result["fields"].append(field)
        
        # Extract standard field names
        standard_fields = ["status", "assignee", "reporter", "priority", "issuetype", "created", "updated"]
        for field in standard_fields:
            if re.search(rf'\b{field}\b', jql, re.IGNORECASE):
                result["fields"].append(field)
        
        # Extract conditions (field = value)
        condition_pattern = r'(\w+|\"[^\"]+\")\s*(=|!=|~|>|<|\bIN\b)\s*("[^"]*"|\'[^\']*\'|\([^\)]*\)|\w+)'
        conditions = re.findall(condition_pattern, jql, re.IGNORECASE)
        for field, op, value in conditions:
            # Clean up the value
            value = value.strip('"\'()')
            result["conditions"].append({
                "field": field.strip('"'),
                "operator": op.strip(),
                "value": value
            })
            
        # Extract custom fields (those starting with customfield_)
        customfield_pattern = r'(customfield_\d+)'
        customfields = re.findall(customfield_pattern, jql)
        for field in customfields:
            if field not in result["custom_fields"]:
                result["custom_fields"].append(field)
                result["fields"].append(field)
                
        return result
        
    def _extract_custom_field_value(self, issue: Any, field_name: str) -> str:
        """Extract value of a custom field from an issue.
        
        Args:
            issue: JIRA issue object
            field_name: Name of the custom field (display name or ID)
            
        Returns:
            String representation of the field value
        """
        if not hasattr(issue, 'fields'):
            return None
            
        fields = issue.fields
        
        # Handle specific known custom fields with special character handling
        if field_name.startswith('"') and field_name.endswith('"'):
            field_name = field_name[1:-1]  # Remove quotes
            
        # Handle specific known custom fields used in the original JQL query
        if field_name == "#Key Project Status":
            # Try various common variations of this field name
            key_project_status_variants = [
                "keyprojstatus", "keyprojectstatus", "projectstatus", 
                "keystatus", "keyproject"
            ]
            
            for attr_name in dir(fields):
                if attr_name.startswith('customfield_'):
                    # Check if attribute name contains any of our variants
                    attr_lower = attr_name.lower()
                    if any(variant in attr_lower for variant in key_project_status_variants):
                        value = getattr(fields, attr_name)
                        if value is not None:
                            return self._format_field_value(value)
                            
            # Also try direct field values that might contain the field name
            for attr_name in dir(fields):
                if attr_name.startswith('customfield_'):
                    try:
                        value = getattr(fields, attr_name)
                        if value is not None:
                            # Check if the value might be or contain the field name
                            value_str = str(value)
                            if "key project status" in value_str.lower() or "project status" in value_str.lower():
                                return self._format_field_value(value)
                    except:
                        pass
        
        elif field_name == "#Quarter(New)" or field_name == "#Quarter(New)":
            # Try various common variations of this field name
            quarter_variants = [
                "quarter", "qtr", "quarterfiscal", "fiscalquarter", "qtrfiscal"
            ]
            
            for attr_name in dir(fields):
                if attr_name.startswith('customfield_'):
                    # Check if attribute name contains any of our variants
                    attr_lower = attr_name.lower()
                    if any(variant in attr_lower for variant in quarter_variants):
                        value = getattr(fields, attr_name)
                        if value is not None:
                            return self._format_field_value(value)
                            
            # Also try looking for values like "FY26-Q1"
            fy_pattern = r"FY\d+-Q[1-4]"
            for attr_name in dir(fields):
                if attr_name.startswith('customfield_'):
                    try:
                        value = getattr(fields, attr_name)
                        if value is not None:
                            value_str = str(value)
                            if re.search(fy_pattern, value_str):
                                return value_str
                    except:
                        pass
        
        # First, try direct attribute access for customfield_XXXXX format
        if field_name.startswith('customfield_') and hasattr(fields, field_name):
            value = getattr(fields, field_name)
            return self._format_field_value(value)
            
        # Second, try to find by field name (for display names)
        # Convert field names to lowercase for case-insensitive matching
        field_name_lower = field_name.lower()
        
        # This loop handles cases where the API might use different attribute names
        for attr_name in dir(fields):
            # Skip internal attributes and methods
            if attr_name.startswith('_') or callable(getattr(fields, attr_name)):
                continue
                
            # Check if this might be our field by looking for matching words
            # This handles cases where "#Key Project Status" might be represented differently in the API
            if attr_name.startswith('customfield_'):
                try:
                    # Check if the value of this field contains our field name
                    # (sometimes JIRA API includes field name in the value)
                    value = getattr(fields, attr_name)
                    if value is not None:
                        value_str = self._format_field_value(value)
                        # If the field value or attribute name contains parts of our field name
                        if (field_name_lower in value_str.lower() or 
                            any(word.lower() in attr_name.lower() for word in field_name_lower.split() if len(word) > 2)):
                            return value_str
                except Exception:
                    pass
                    
        # Look for common patterns in custom fields:
        # 1. Fields with hashes - often mapped differently in the API
        if field_name.startswith('#'):
            clean_name = field_name[1:].lower().replace(' ', '')
            for attr_name in dir(fields):
                if attr_name.startswith('customfield_'):
                    attr_clean = attr_name.lower().replace('_', '')
                    if clean_name in attr_clean:
                        value = getattr(fields, attr_name)
                        return self._format_field_value(value)
                    
            # Handle fields with parentheses like "#Quarter(New)"
            if '(' in clean_name:
                base_name = clean_name.split('(')[0]
                for attr_name in dir(fields):
                    if attr_name.startswith('customfield_'):
                        attr_clean = attr_name.lower().replace('_', '')
                        if base_name in attr_clean:
                            value = getattr(fields, attr_name)
                            return self._format_field_value(value)
                            
            # Try to extract values from custom field values if they match expected formats
            if 'quarter' in clean_name.lower():
                # Look for fiscal quarter format (FY26-Q1)
                fy_quarter_pattern = r'FY\d+-Q[1-4]'
                for attr_name in dir(fields):
                    if attr_name.startswith('customfield_'):
                        try:
                            value = getattr(fields, attr_name)
                            if value:
                                value_str = str(value)
                                if re.search(fy_quarter_pattern, value_str):
                                    return value_str
                        except:
                            pass
            
            if 'status' in clean_name.lower() and 'key' in clean_name.lower():
                # Look for specific status values like "Yes-POD", "Yes-Group"
                pod_status_pattern = r'Yes-(?:POD|Group|Sub-Group)'
                for attr_name in dir(fields):
                    if attr_name.startswith('customfield_'):
                        try:
                            value = getattr(fields, attr_name)
                            if value:
                                value_str = str(value)
                                if re.search(pod_status_pattern, value_str):
                                    return value_str
                        except:
                            pass

        # Nothing found
        return None
    
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
        
        return custom_fields
    
    def _get_custom_field_distributions(self, issues: List[Any], field_names: List[str]) -> Dict[str, Dict[str, int]]:
        """Get distribution of values for custom fields across issues.
        
        Args:
            issues: List of JIRA issues
            field_names: List of custom field names to analyze
            
        Returns:
            Dictionary of {field_name: {value: count}} distributions
        """
        result = {}
        
        for field_name in field_names:
            field_values = {}
            
            for issue in issues:
                try:
                    if hasattr(issue, 'fields'):
                        value = self._extract_custom_field_value(issue, field_name)
                        if value:
                            field_values[value] = field_values.get(value, 0) + 1
                except Exception as e:
                    logging.debug(f"Error extracting custom field '{field_name}': {str(e)}")
            
            if field_values:
                result[field_name] = field_values
                
        return result
    
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
        if hasattr(value, 'name'):
            return value.name
            
        # Handle objects with value attributes
        if hasattr(value, 'value'):
            return value.value
            
        # Handle array/list types with multiple values
        if isinstance(value, (list, tuple)):
            if all(hasattr(item, 'name') for item in value):
                return ", ".join(item.name for item in value)
            elif all(hasattr(item, 'value') for item in value):
                return ", ".join(item.value for item in value)
            else:
                return ", ".join(str(item) for item in value)
        
        # Default to string representation
        return str(value)
    
    def _safe_get_field(self, issue: Any, field_name: str, default_value: Any = None) -> Any:
        """Safely get a field value from a JIRA issue with error handling.
        
        Args:
            issue: The JIRA issue object
            field_name: Name of the field to retrieve
            default_value: Default value to return if field doesn't exist
            
        Returns:
            The field value or default_value if field doesn't exist
        """
        try:
            if not hasattr(issue, 'fields'):
                return default_value
                
            return getattr(issue.fields, field_name, default_value)
        except Exception as e:
            logging.debug(f"Error accessing field '{field_name}': {str(e)}")
            return default_value
            
    def _get_custom_field(self, issue: Any, field_display_name: str) -> Any:
        """Try to find a custom field by its display name.
        
        Args:
            issue: The JIRA issue object
            field_display_name: Display name of the custom field to find
            
        Returns:
            The custom field value or None if not found
        """
        try:
            if not hasattr(issue, 'fields'):
                return None
                
            # First check if we can find the field by a direct name match
            # This covers fields that have been properly mapped
            for attr_name in dir(issue.fields):
                # Skip internal attributes and methods
                if attr_name.startswith('_') or callable(getattr(issue.fields, attr_name)):
                    continue
                    
                # Try to match on field name (for custom fields often the ID is used)
                if field_display_name.lower() in attr_name.lower():
                    return getattr(issue.fields, attr_name)
            
            # Look for custom fields in the format customfield_XXXXX
            custom_field_pattern = r'customfield_\d+'
            for attr_name in dir(issue.fields):
                if re.match(custom_field_pattern, attr_name):
                    value = getattr(issue.fields, attr_name)
                    # For some custom fields, if they match what we're looking for, return them
                    # This is a heuristic approach since we don't have the field definition
                    if value is not None:
                        return value
                        
            return None
        except Exception as e:
            logging.debug(f"Error accessing custom field '{field_display_name}': {str(e)}")
            return None
    
    def get_form_fields(self) -> Dict[str, Any]:
        """Return form fields for JIRA configuration"""
        return {
            'type': 'jira',
            'fields': [
                {
                    'type': 'text',
                    'label': 'Project Key',
                    'name': 'project_key',
                    'placeholder': 'Enter JIRA project key or ID (e.g., PROJ or PROJ, DEMO)',
                    'help_text': 'Enter one or more JIRA project keys, separated by commas.',
                    'optional': True,
                    'add_more': True
                },
                {
                    'type': 'textarea',
                    'label': 'JQL Query',
                    'name': 'jql_query',
                    'placeholder': 'Enter a custom JQL query (e.g., project = PROJ AND status = "In Progress")',
                    'help_text': 'Advanced: Enter a JQL query to fetch specific issues. Takes precedence over Project Key if both provided.',
                    'optional': True
                }
            ]
        }
    
    def _validate_project_key(self, project_key: str) -> bool:
        """Validate if a project key exists in JIRA.
        
        Args:
            project_key: The JIRA project key to validate
            
        Returns:
            Boolean indicating if the project key is valid
        """
        try:
            # First check our cache of available project keys
            available_keys = self._get_available_project_keys()
            logging.info(f"Available project keys: {available_keys}")
            
            if project_key in available_keys:
                logging.info(f"Project key {project_key} is valid")
                return True
                
            # If not in cache, try a direct API call as fallback
            jql_test = f"project = {project_key}"
            test_issues = self.client.search_issues(jql_test, maxResults=1)
            # If we get here without an exception, the project key is valid
            logging.info(f"Project key {project_key} is valid via API check")
            
            # Add to our cache for future checks
            available_keys.append(project_key)
            
            return True
            
        except Exception as e:
            logging.error(f"Project key validation failed for {project_key}: {str(e)}")
            return False
            
    @lru_cache(maxsize=1)
    def _get_available_project_keys(self) -> List[str]:
        """Get a list of all available project keys in JIRA.
        
        Returns:
            List of project keys
        """
        try:
            # Get all projects the user has access to
            projects = self.client.projects()
            return [project.key for project in projects]
        except Exception as e:
            logging.error(f"Error fetching available project keys: {str(e)}")
            return []
    
    def _dump_issue_structure(self, issue: Any) -> Dict[str, Any]:
        """Dump the structure of a JIRA issue for debugging purposes.
        
        Args:
            issue: JIRA issue object
            
        Returns:
            Dictionary with the structure of the issue
        """
        if not issue:
            return {"error": "No issue provided"}
            
        try:
            result = {
                "has_key": hasattr(issue, "key"),
                "has_fields": hasattr(issue, "fields"),
                "available_attributes": dir(issue)
            }
            
            # Add key if available
            if hasattr(issue, "key"):
                result["key"] = issue.key
                
            # Process fields if available
            if hasattr(issue, "fields"):
                result["fields"] = {}
                
                # Get all field attributes
                field_attrs = dir(issue.fields)
                result["available_fields"] = [attr for attr in field_attrs if not attr.startswith('_')]
                
                # Get standard fields
                standard_fields = ["summary", "status", "assignee", "description", 
                                  "priority", "issuetype", "created", "updated"]
                                  
                for field in standard_fields:
                    if hasattr(issue.fields, field):
                        field_value = getattr(issue.fields, field)
                        if field_value:
                            if field in ["status", "issuetype", "priority"]:
                                # For complex objects, just get name
                                if hasattr(field_value, "name"):
                                    result["fields"][field] = field_value.name
                                else:
                                    result["fields"][field] = "Complex object without name"
                            elif field == "assignee":
                                # For user objects, get displayName
                                if hasattr(field_value, "displayName"):
                                    result["fields"][field] = field_value.displayName
                                else:
                                    result["fields"][field] = "User without displayName"
                            else:
                                # For simple fields like summary and description
                                result["fields"][field] = str(field_value)[:50] + "..." if len(str(field_value)) > 50 else str(field_value)
                        else:
                            result["fields"][field] = None
                
                # Check for custom fields
                custom_fields = [f for f in field_attrs if f.startswith("customfield_")]
                if custom_fields:
                    result["custom_fields"] = {}
                    for cf in custom_fields:
                        try:
                            value = getattr(issue.fields, cf)
                            if value is not None:
                                result["custom_fields"][cf] = str(value)[:50] + "..." if len(str(value)) > 50 else str(value)
                        except Exception as e:
                            result["custom_fields"][cf] = f"Error: {str(e)}"
            
            return result
        except Exception as e:
            return {
                "error": f"Error dumping issue structure: {str(e)}",
                "available_attributes": dir(issue) if issue else []
            }
    
    def _extract_all_custom_fields(self, issue: Any) -> Dict[str, str]:
        """Extract all custom fields from an issue.
        
        Args:
            issue: JIRA issue object
            
        Returns:
            Dictionary of custom field names and values
        """
        if not hasattr(issue, 'fields'):
            return {}
            
        result = {}
        fields = issue.fields
        
        # Process all fields that start with customfield_
        for attr_name in dir(fields):
            if attr_name.startswith('customfield_'):
                try:
                    value = getattr(fields, attr_name)
                    if value is not None:
                        # Try to get a display name for this field if possible
                        display_name = attr_name
                        
                        # Format the value based on its type
                        formatted_value = self._format_field_value(value)
                        
                        # Only include non-empty values
                        if formatted_value and formatted_value != "None":
                            result[display_name] = formatted_value
                except Exception:
                    pass
                    
        return result
    
    def _collect_available_fields(self, issues: List[Any]) -> Dict[str, List[str]]:
        """Collect information about available fields across all issues.
        
        Args:
            issues: List of JIRA issues
            
        Returns:
            Dictionary with lists of standard_fields and custom_fields
        """
        if not issues:
            return {}
            
        standard_fields = set()
        custom_fields = set()
        
        for issue in issues:
            if hasattr(issue, 'fields'):
                fields = issue.fields
                for attr_name in dir(fields):
                    if attr_name.startswith('_') or callable(getattr(fields, attr_name)):
                        continue
                        
                    if attr_name.startswith('customfield_'):
                        custom_fields.add(attr_name)
                    else:
                        standard_fields.add(attr_name)
        
        return {
            "standard_fields": sorted(list(standard_fields)),
            "custom_fields": sorted(list(custom_fields))
        } 