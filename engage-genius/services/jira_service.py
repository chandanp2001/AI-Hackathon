import requests
import logging
import os
from typing import Dict, Any, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

def fetch_jira_project(project_key: str) -> str:
    """
    Fetch information from a JIRA project using the API
    
    Args:
        project_key: The JIRA project key (e.g., 'EHGI')
        
    Returns:
        String with the formatted JIRA project information
    """
    try:
        logger.info(f"Fetching JIRA project: {project_key}")
        
        # Get JIRA credentials from environment variables using the correct variable names from .env
        jira_email = os.environ.get("JIRA_EMAIL", "")
        jira_api_token = os.environ.get("JIRA_API_TOKEN", "")
        jira_base_url = os.environ.get("JIRA_SERVER", "https://razorpay.atlassian.net")
        
        # Log what credentials we found (without exposing sensitive values)
        logger.info(f"JIRA email: {jira_email}")
        logger.info(f"JIRA base URL: {jira_base_url}")
        logger.info(f"JIRA API token found: {'Yes' if jira_api_token else 'No'}")
        
        # Check if credentials are available
        if not jira_email or not jira_api_token:
            logger.warning(f"JIRA credentials not found in environment variables")
            return f"**{project_key} JIRA Project Summary**\n\nUnable to fetch JIRA data: Missing API credentials"
        
        # JQL query to get issues for this project
        jql_query = f"project = {project_key} ORDER BY priority DESC, updated DESC"
        
        # API endpoint
        issues_url = f"{jira_base_url}/rest/api/2/search"
        
        logger.info(f"Making JIRA API request to: {issues_url}")
        
        # Make API request
        response = requests.get(
            issues_url,
            auth=(jira_email, jira_api_token),
            params={
                'jql': jql_query,
                'maxResults': 30,
                'fields': 'summary,status,priority,issuetype,assignee,duedate,created,updated'
            },
            headers={'Content-Type': 'application/json'}
        )
        
        # Check if request was successful
        if response.status_code != 200:
            logger.error(f"JIRA API request failed: {response.status_code} - {response.text}")
            return f"**{project_key} JIRA Project Summary**\n\nUnable to fetch JIRA data: API returned status {response.status_code}"
        
        # Parse results
        jira_data = response.json()
        issues = jira_data.get('issues', [])
        
        # If no issues found
        if not issues:
            logger.warning(f"No issues found for project {project_key}")
            return f"**{project_key} JIRA Project Summary**\n\nNo issues found for this project"
        
        logger.info(f"Successfully retrieved {len(issues)} issues for project {project_key}")
        
        # Calculate statistics
        total_issues = len(issues)
        status_counts = {}
        type_counts = {}
        priority_counts = {}
        
        for issue in issues:
            # Count by status
            status = issue['fields']['status']['name']
            status_counts[status] = status_counts.get(status, 0) + 1
            
            # Count by issue type
            issue_type = issue['fields']['issuetype']['name']
            type_counts[issue_type] = type_counts.get(issue_type, 0) + 1
            
            # Count by priority
            priority = issue['fields']['priority']['name'] if 'priority' in issue['fields'] and issue['fields']['priority'] else 'Unassigned'
            priority_counts[priority] = priority_counts.get(priority, 0) + 1
        
        # Format output
        summary = f"**{project_key} JIRA Project Summary**\n\n"
        
        # Add issue statistics
        summary += f"**Issue Statistics:**\n"
        summary += f"- Total Issues: {total_issues}\n\n"
        
        # Add status breakdown
        summary += f"**Status Breakdown:**\n"
        for status, count in status_counts.items():
            percentage = (count / total_issues) * 100
            summary += f"- {status}: {count} ({percentage:.0f}%)\n"
        summary += "\n"
        
        # Add priority breakdown
        summary += f"**Priority Breakdown:**\n"
        for priority, count in priority_counts.items():
            percentage = (count / total_issues) * 100
            summary += f"- {priority}: {count} ({percentage:.0f}%)\n"
        summary += "\n"
        
        # Add issue type breakdown
        summary += f"**Issue Type Breakdown:**\n"
        for issue_type, count in type_counts.items():
            percentage = (count / total_issues) * 100
            summary += f"- {issue_type}: {count} ({percentage:.0f}%)\n"
        summary += "\n"
        
        # List recent issues (up to 5)
        summary += f"**Recent Issues:**\n"
        for i, issue in enumerate(issues[:5]):
            issue_key = issue['key']
            issue_summary = issue['fields']['summary']
            issue_status = issue['fields']['status']['name']
            issue_type = issue['fields']['issuetype']['name']
            
            summary += f"{i+1}. [{issue_key}] {issue_summary} ({issue_status}, {issue_type})\n"
        
        return summary
        
    except Exception as e:
        error_msg = f"Error fetching JIRA data for project {project_key}: {str(e)}"
        logger.error(error_msg)
        return f"**{project_key} JIRA Project Summary**\n\nError: {error_msg}" 