from typing import Dict, Any, List, Union, Tuple, Optional, Set
import logging
import os
import json
import re
import xml.etree.ElementTree as ET
import requests
from .base import DataSource
import time
from functools import lru_cache, wraps
import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
import pandas as pd
from difflib import get_close_matches

# Explicitly download required NLTK resources at module load time
try:
    nltk.download('punkt', quiet=True)
    nltk.download('stopwords', quiet=True)
except Exception as e:
    logging.warning(f"Failed to download NLTK resources: {e}")

# Import config module
import sys
import os.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from config import TABLEAU_SERVER, TABLEAU_API_VERSION, TABLEAU_SITE_CONTENT_URL, TABLEAU_TOKEN_NAME, TABLEAU_TOKEN_SECRET

# Add timing decorator for metrics right after imports
def timing_metric(func):
    """Decorator to measure and log function execution time for metrics."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        execution_time = end_time - start_time
        
        # First arg should be self for class methods
        if args and hasattr(args[0], '_log_metric'):
            args[0]._log_metric(
                metric_name=f"{func.__name__}_execution_time",
                value=execution_time,
                unit="seconds"
            )
            
        return result
    return wrapper

class ConfigError(Exception):
    """Exception raised for configuration errors."""
    pass

# Define structured error types
class TableauError(Exception):
    """Base class for Tableau-specific errors."""
    def __init__(self, message: str, error_type: str, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.error_type = error_type
        self.details = details or {}
        super().__init__(message)
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert error to a structured dictionary for LLM consumption."""
        return {
            "error_type": self.error_type,
            "message": self.message,
            "details": self.details
        }

class AuthenticationError(TableauError):
    """Error raised for authentication issues."""
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, "authentication_error", details)

class DataAccessError(TableauError):
    """Error raised when data cannot be accessed."""
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, "data_access_error", details)

class ParameterError(TableauError):
    """Error raised for parameter issues."""
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, "parameter_error", details)

class FilterError(TableauError):
    """Error raised for filter issues."""
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, "filter_error", details)

class TableauDataSource(DataSource):
    """Data source for Tableau dashboards and views."""
    
    def __init__(self):
        """Initialize the Tableau data source."""
        super().__init__()
        
        # Load configuration from config module
        # Get configuration values with validation
        self.base_url = self._get_required_config('TABLEAU_SERVER', TABLEAU_SERVER)
        # Ensure base URL doesn't have trailing slash
        if self.base_url.endswith('/'):
            self.base_url = self.base_url[:-1]
            
        # Full API endpoint for authentication
        self.api_version = TABLEAU_API_VERSION or "3.15"  # Store API version for reuse
        self.auth_url = f"{self.base_url}/api/{self.api_version}/auth/signin"
        self.token_name = self._get_required_config('TABLEAU_TOKEN_NAME', TABLEAU_TOKEN_NAME)
        self.token_secret = self._get_required_config('TABLEAU_TOKEN_SECRET', TABLEAU_TOKEN_SECRET)
        self.site_content_url = TABLEAU_SITE_CONTENT_URL or ""
        self.auth_token = None
        self.site_id = None
        self.connection_error = None
        
        # Cache for storing views
        self.views_cache = {}
        self.views_cache_time = 0
        self.views_cache_ttl = 3600  # Cache TTL in seconds (1 hour)
        
        # Cache for storing parameters
        self.parameters_cache = {}
        self.parameters_cache_time = 0
        self.parameters_cache_ttl = 3600  # Cache TTL in seconds (1 hour)
        
        # Cache for storing filters
        self.filters_cache = {}
        self.filters_cache_time = 0
        self.filters_cache_ttl = 3600  # Cache TTL in seconds (1 hour)
        
        # Metrics tracking
        self.metrics = {
            "queries_made": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "total_response_size_bytes": 0,
            "total_request_time_seconds": 0,
            "error_count": 0
        }
        
        # Log configuration details (masking sensitive info)
        logging.info(f"Tableau configuration: server={self.base_url}, api_version={self.api_version}, token_name={self.token_name}, site_content_url='{self.site_content_url}'")
        
        # Initialize auth token on startup
        try:
            logging.info(f"Initializing Tableau connection to {self.base_url}")
            self.auth_token, self.site_id = self._fetch_auth_token()
            if not self.auth_token or not self.site_id:
                self.connection_error = "Failed to obtain valid authentication token and site ID"
                logging.error(self.connection_error)
            else:
                logging.info(f"Successfully authenticated with Tableau. Token: {self.auth_token[:5]}..., Site ID: {self.site_id}")
                # Prefetch views
                self._prefetch_views()
        except Exception as e:
            self.connection_error = f"Error initializing Tableau connection: {str(e)}"
            logging.error(self.connection_error)
    
    def _log_metric(self, metric_name: str, value: Union[int, float], unit: str = None) -> None:
        """
        Log a metric for the Tableau data source.
        
        Args:
            metric_name: Name of the metric to log
            value: Value to add to the metric
            unit: Optional unit for the metric
        """
        if metric_name in self.metrics:
            self.metrics[metric_name] += value
        else:
            self.metrics[metric_name] = value
    
    def _get_required_config(self, key: str, value: Optional[str]) -> str:
        """
        Get a required configuration value and raise an exception if it's missing.
        
        Args:
            key: The configuration key name
            value: The configuration value
            
        Returns:
            str: The configuration value
            
        Raises:
            ConfigError: If the configuration key is missing or empty
        """
        if not value:
            error_msg = f"Missing required configuration: {key}"
            logging.error(error_msg)
            raise ConfigError(error_msg)
        return value
    
    def _fetch_auth_token(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Fetch the Tableau authentication token and site ID.
        
        Returns:
            Tuple[Optional[str], Optional[str]]: Authentication token and site ID if successful, None otherwise
        """
        try:
            url = self.auth_url
            
            # Set headers to match the successful curl command exactly
            headers = {
                "Content-Type": "application/json",
                "Accept": "*/*"  # Accept any content type, just like curl
            }
            
            # Ensure credentials have no extra whitespace
            token_name = self.token_name.strip() if self.token_name else ""
            token_secret = self.token_secret.strip() if self.token_secret else ""
            
            # Use empty string for site contentUrl to match the working curl format
            payload = {
                "credentials": {
                    "personalAccessTokenName": token_name,
                    "personalAccessTokenSecret": token_secret,
                    "site": {"contentUrl": ""}
                }
            }
            
            logging.info(f"Authenticating with Tableau server at {url}")
            
            # Log the full request details with masked secret
            masked_payload = {
                "credentials": {
                    "personalAccessTokenName": token_name,
                    "personalAccessTokenSecret": "***MASKED***",
                    "site": {"contentUrl": ""}
                }
            }
            logging.info(f"Auth request details: URL={url}, Headers={headers}, Payload={json.dumps(masked_payload)}")
            
            # Make authentication request
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            # Log full response details for debugging
            logging.info(f"Response status: {response.status_code}")
            logging.info(f"Response headers: {dict(response.headers)}")
            
            # Log the full response content for 401 errors (usually contains error details)
            if response.status_code == 401:
                logging.error(f"Authentication failed (401): {response.text}")
                error_msg = "Invalid credentials or authentication token"
                if "invalid" in response.text.lower():
                    error_msg = "The personal access token appears to be invalid. Please check your token name and secret."
                logging.error(error_msg)
                return None, None
            
            # Check response status for other errors
            if response.status_code != 200:
                logging.error(f"Failed to authenticate with Tableau: Status {response.status_code}")
                logging.error(f"Response content: {response.text}")
                return None, None
            
            # Log the full successful response
            logging.info(f"Successful authentication response: {response.text[:100]}...")
            
            # Process XML response
            try:
                # Parse XML with namespace handling
                root = ET.fromstring(response.text)
                
                # Define known namespace
                ns = {"ts": "http://tableau.com/api"}
                
                # Extract credentials element
                credentials = root.find(".//ts:credentials", ns)
                if credentials is None:
                    credentials = root.find(".//credentials")
                
                if credentials is None:
                    logging.error("Could not find credentials element in XML response")
                    return None, None
                
                # Extract token from credentials attributes
                auth_token = credentials.get("token")
                if not auth_token:
                    logging.error("Auth token missing from XML response")
                    return None, None
                
                # Extract site element and ID
                site = credentials.find(".//ts:site", ns)
                if site is None:
                    site = credentials.find(".//site")
                
                if site is None:
                    logging.error("Could not find site element in XML response")
                    return None, None
                
                site_id = site.get("id")
                if not site_id:
                    logging.error("Site ID missing from XML response")
                    return None, None
                
                logging.info(f"Successfully retrieved auth token and site ID: {site_id}")
                return auth_token, site_id
                
            except ET.ParseError as e:
                logging.error(f"Error parsing Tableau XML response: {e}")
                logging.error(f"Response content: {response.text}")
                return None, None
                
        except requests.RequestException as e:
            logging.error(f"Request error during Tableau authentication: {e}")
            return None, None
        except Exception as e:
            logging.error(f"Error fetching Tableau auth token: {e}")
            logging.error(f"Exception details: {str(e)}")
            return None, None
    
    def _refresh_token_if_needed(self) -> bool:
        """
        Refresh the authentication token if it has expired or doesn't exist.
        
        Returns:
            bool: True if token refresh was successful or not needed, False otherwise
        """
        if not self.auth_token or not self.site_id:
            logging.info("Refreshing Tableau authentication token")
            self.auth_token, self.site_id = self._fetch_auth_token()
            
            if not self.auth_token or not self.site_id:
                logging.error("Failed to refresh Tableau authentication token")
                return False
                
            return True
        return True
    
    async def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """
        Validate the inputs for this data source.
        
        Args:
            inputs: Dictionary containing input parameters including 'view_url'
            
        Returns:
            bool: True if inputs are valid, False otherwise
        """
        try:
            logging.info(f"Validating Tableau inputs: {inputs}")
            
            # Check for required keys
            if 'view_url' not in inputs:
                logging.error("No 'view_url' key found in inputs")
                return False
            
            view_url = inputs.get('view_url')
            if not view_url:
                logging.error("Empty view URL provided")
                return False
            
            # Extract domain from base_url for comparison
            base_domain = self.base_url.split('//')[1].split('/')[0] if '//' in self.base_url else self.base_url
            
            # Validate that the URL is for the configured Tableau server
            if base_domain not in view_url:
                logging.error(f"Invalid Tableau URL: {view_url}, expected to contain {base_domain}")
                return False
            
            # First extract the view name
            view_name = self._extract_view_name(view_url)
            if not view_name:
                logging.error(f"Could not extract view name from URL: {view_url}")
                return False
                
            logging.info(f"Extracted view name: {view_name}")
            
            # Ensure we have a valid auth token
            if not self._refresh_token_if_needed():
                logging.error("Failed to validate auth token")
                return False
            
            return True
            
        except Exception as e:
            logging.error(f"Error validating Tableau inputs: {str(e)}")
            return False
    
    def _extract_view_id(self, url: str) -> Optional[str]:
        """
        Extract the view ID from a Tableau URL by first getting the view name,
        then using the view name to fetch the view ID from Tableau API.
        
        Args:
            url: The Tableau view URL
            
        Returns:
            str: The extracted view ID or None if not found
        """
        # First extract the view name from the URL
        view_name = self._extract_view_name(url)
        if not view_name:
            logging.error(f"Could not extract view name from URL: {url}")
            return None

        # Now use the view name to fetch the view ID
        return self._get_view_id_from_name(view_name)
    
    def _extract_view_name(self, url: str) -> Optional[str]:
        """
        Extract the view name from a Tableau URL.
        
        Args:
            url: The Tableau view URL
            
        Returns:
            str: The extracted view name or None if not found
        """
        # Common URL patterns for Tableau views
        # 1. /views/workbook/viewname
        # 2. /views/viewname
        # 3. /t/site/views/workbook/viewname
        patterns = [
            r'/views/([^/]+)/([^/\?]+)',  # Extract from /views/workbook/viewname
            r'/views/([^/\?]+)',          # Extract from /views/viewname
            r'/t/[^/]+/views/([^/]+)/([^/\?]+)'  # Extract from /t/site/views/workbook/viewname
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                # If pattern has 2 groups, second group is the view name (workbook/viewname pattern)
                if len(match.groups()) == 2:
                    return match.group(2)
                # If pattern has 1 group, it's the view name directly
                else:
                    return match.group(1)
        
        # Try to extract from query parameter if present
        query_patterns = [
            r'[\?&]viewname=([^&]+)'
        ]
        
        for pattern in query_patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
                
        logging.error(f"Could not extract view name using any pattern from URL: {url}")
        return None
    
    async def prefetch_views_async(self) -> None:
        """
        Async wrapper for prefetching views - can be called from background tasks.
        This method calls the synchronous _prefetch_views method.
        """
        try:
            logging.info("Starting asynchronous view prefetch")
            # Call the synchronous method
            self._prefetch_views()
            logging.info("Asynchronous view prefetch completed")
        except Exception as e:
            logging.error(f"Error in asynchronous view prefetch: {str(e)}")
            
    def _prefetch_views(self) -> None:
        """
        Prefetch all views from the Tableau site and store them in cache.
        Handles pagination to get all views, not just the first 100.
        """
        if not self._refresh_token_if_needed():
            logging.error("Failed to authenticate with Tableau for view prefetching")
            return
            
        try:
            logging.info("Prefetching all views from Tableau site")
            
            # Reset cache
            self.views_cache = {}
            self.views_cache_time = time.time()
            
            page_size = 100  # Maximum page size supported by Tableau API
            page_number = 1
            total_views = 0
            more_pages = True
            max_retries = 3
            
            while more_pages:
                api_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views"
                
                # Add pagination parameters
                params = {
                    'pageSize': str(page_size),  # Convert to string to avoid type issues
                    'pageNumber': str(page_number)  # Convert to string to avoid type issues
                }
                
                headers = {
                    "X-Tableau-Auth": self.auth_token,
                    "Accept": "application/json"
                }
                
                # Add retry logic for reliability
                for retry in range(max_retries):
                    try:
                        logging.info(f"Fetching views batch {page_number} from: {api_url}")
                        response = requests.get(api_url, headers=headers, params=params, timeout=30)
                        
                        if response.status_code == 401:
                            logging.warning("Authentication token expired during view prefetch, refreshing...")
                            if not self._refresh_token_if_needed():
                                logging.error("Failed to refresh authentication token")
                                return
                            headers["X-Tableau-Auth"] = self.auth_token
                            continue
                            
                        if response.status_code != 200:
                            logging.error(f"Failed to fetch views: Status {response.status_code}")
                            logging.error(f"Response: {response.text[:200]}...")
                            if retry < max_retries - 1:
                                logging.info(f"Retrying batch {page_number} (attempt {retry+2}/{max_retries})...")
                                time.sleep(1 * (retry + 1))  # Exponential backoff
                                continue
                            else:
                                break
                        
                        # Parse the JSON response
                        data = response.json()
                        views = data.get('views', {}).get('view', [])
                        
                        batch_count = len(views)
                        total_views += batch_count
                        logging.info(f"Fetched batch {page_number} with {batch_count} views")
                        
                        # Process and cache the views
                        for view in views:
                            view_id = view.get('id')
                            view_name = view.get('name', '')
                            workbook_name = view.get('workbook', {}).get('name', '')
                            content_url = view.get('contentUrl', '')
                            
                            # Create variations of the view name for better lookup
                            variations = [
                                view_name,  # Original name
                                view_name.lower(),  # Lowercase
                                view_name.replace(' ', ''),  # No spaces
                                view_name.lower().replace(' ', ''),  # Lowercase, no spaces
                                content_url.split('/')[-1] if '/' in content_url else content_url  # Content URL basename
                            ]
                            
                            # Also handle URLs with query parameters
                            if '?' in content_url:
                                base_content_url = content_url.split('?')[0]
                                if '/' in base_content_url:
                                    variations.append(base_content_url.split('/')[-1])
                            
                            # Store in cache with all variations as keys
                            for var in variations:
                                if var and isinstance(var, str) and len(var) > 1:  # Ensure not empty and is string
                                    self.views_cache[var] = {
                                        'id': view_id,
                                        'name': view_name,
                                        'workbook': workbook_name,
                                        'content_url': content_url
                                    }
                        
                        # Check if there are more pages - safely handle type conversions
                        pagination = data.get('pagination', {})
                        total_available_str = pagination.get('totalAvailable', '0')
                        
                        # Convert to int safely
                        try:
                            total_available = int(total_available_str)
                        except (ValueError, TypeError):
                            logging.warning(f"Invalid totalAvailable value: {total_available_str}, assuming no more pages")
                            more_pages = False
                            break
                            
                        # Safe comparison with proper types
                        more_pages = (total_views < total_available) and (batch_count > 0)
                        
                        if more_pages:
                            page_number += 1
                            # Small pause between successful batch fetches
                            time.sleep(0.5)
                        
                        # Successfully processed this batch, break retry loop
                        break
                        
                    except requests.RequestException as e:
                        logging.error(f"Network error when fetching views batch {page_number}: {str(e)}")
                        if retry < max_retries - 1:
                            logging.info(f"Retrying batch {page_number} after network error...")
                            time.sleep(1 * (retry + 1))  # Exponential backoff
                        else:
                            logging.error(f"Failed to fetch views batch {page_number} after {max_retries} attempts")
                            more_pages = False
                            break
                    except json.JSONDecodeError as e:
                        logging.error(f"Invalid JSON in response for batch {page_number}: {str(e)}")
                        if retry < max_retries - 1:
                            logging.info(f"Retrying batch {page_number} after JSON error...")
                            time.sleep(1 * (retry + 1))
                        else:
                            logging.error(f"Failed to parse JSON for batch {page_number} after {max_retries} attempts")
                            more_pages = False
                            break
                    except Exception as e:
                        logging.error(f"Unexpected error in batch {page_number}: {str(e)}")
                        if retry < max_retries - 1:
                            logging.info(f"Retrying batch {page_number} after unexpected error...")
                            time.sleep(1 * (retry + 1))
                        else:
                            logging.error(f"Failed batch {page_number} after {max_retries} attempts")
                            more_pages = False
                            break
            
            # Generate stats about cached variations
            variation_counts = {}
            for var in self.views_cache.keys():
                var_type = 'original'
                if var.islower() and ' ' not in var:
                    var_type = 'lowercase_nospace'
                elif var.islower():
                    var_type = 'lowercase'
                elif ' ' not in var:
                    var_type = 'nospace'
                
                variation_counts[var_type] = variation_counts.get(var_type, 0) + 1
                
            logging.info(f"Successfully prefetched {total_views} views with {len(self.views_cache)} name variations")
            logging.info(f"Variation types: {variation_counts}")
            
            # Save cache to disk for persistence
            self._save_views_cache_to_disk()
            
        except Exception as e:
            logging.error(f"Error prefetching views: {str(e)}")
            # Try to load cache from disk as fallback
            self._load_views_cache_from_disk()
    
    def _save_views_cache_to_disk(self):
        """Save views cache to disk for persistence between restarts"""
        try:
            import os
            import json
            
            cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'cache')
            os.makedirs(cache_dir, exist_ok=True)
            
            cache_file = os.path.join(cache_dir, 'tableau_views_cache.json')
            
            # Save views data
            cache_data = {
                'timestamp': self.views_cache_time,
                'views': self.views_cache
            }
            
            with open(cache_file, 'w') as f:
                json.dump(cache_data, f)
                
            logging.info(f"Successfully saved {len(self.views_cache)} views to disk cache")
            return True
        except Exception as e:
            logging.error(f"Error saving views cache to disk: {e}")
            return False
            
    def _load_views_cache_from_disk(self):
        """Load views cache from disk if available"""
        try:
            import os
            import json
            
            cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'cache')
            cache_file = os.path.join(cache_dir, 'tableau_views_cache.json')
            
            if not os.path.exists(cache_file):
                logging.info("No views cache file found on disk")
                return False
                
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
                
            # Check if cache is still valid (less than 1 day old)
            cache_timestamp = cache_data.get('timestamp', 0)
            current_time = time.time()
            
            if current_time - cache_timestamp > 86400:  # 24 hours
                logging.info("Disk cache is older than 24 hours, not using it")
                return False
                
            self.views_cache = cache_data.get('views', {})
            self.views_cache_time = cache_timestamp
            
            logging.info(f"Successfully loaded {len(self.views_cache)} views from disk cache")
            return True
        except Exception as e:
            logging.error(f"Error loading views cache from disk: {e}")
            return False
    
    def _get_view_id_from_cache(self, view_name: str) -> Optional[str]:
        """
        Get view ID from the cache using various name variations.
        
        Args:
            view_name: The name of the view to look up
            
        Returns:
            str: View ID if found, None otherwise
        """
        # Check if cache is still valid
        if time.time() - self.views_cache_time > self.views_cache_ttl:
            logging.info("Views cache expired, refreshing...")
            self._prefetch_views()
        
        # Try various name variations
        variations = [
            view_name,  # Original
            view_name.lower(),  # Lowercase
            view_name.replace(' ', ''),  # No spaces
            view_name.lower().replace(' ', '')  # Lowercase, no spaces
        ]
        
        # Try each variation
        for var in variations:
            if var in self.views_cache:
                view_data = self.views_cache[var]
                logging.info(f"Found view in cache: {view_name} → {view_data['name']} (ID: {view_data['id']})")
                return view_data['id']
        
        logging.warning(f"View not found in cache: {view_name}")
        return None
    
    def _get_view_id_from_name(self, view_name: str) -> Optional[str]:
        """
        Get the view ID from the view name using Tableau API.
        First checks the cache, then falls back to API lookup if needed.
        
        Args:
            view_name: The name of the Tableau view
            
        Returns:
            str: The view ID or None if not found
        """
        # First check the cache
        view_id = self._get_view_id_from_cache(view_name)
        if view_id:
            return view_id
            
        # If not in cache, try refreshing the cache once
        logging.info(f"View '{view_name}' not found in cache, refreshing cache...")
        self._prefetch_views()
        
        # Check cache again after refresh
        view_id = self._get_view_id_from_cache(view_name)
        if view_id:
            return view_id
        
        # If still not found, try a direct API lookup
        if not self._refresh_token_if_needed():
            logging.error("Failed to authenticate with Tableau")
            return None
            
        try:
            # Get views with filter for this specific name
            api_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views"
            params = {
                'filter': f"name:eq:{view_name}"
            }
            
            headers = {
                "X-Tableau-Auth": self.auth_token,
                "Accept": "application/json"
            }
            
            logging.info(f"Doing direct API lookup for view: {view_name}")
            response = requests.get(api_url, headers=headers, params=params, timeout=30)
            
            if response.status_code != 200:
                logging.error(f"Failed to fetch view: Status {response.status_code}")
                logging.error(f"Response: {response.text[:200]}...")
                return None
                
            data = response.json()
            views = data.get('views', {}).get('view', [])
            
            if views:
                view_id = views[0].get('id')
                logging.info(f"Found view ID {view_id} via direct API lookup")
                
                # Add to cache
                self.views_cache[view_name] = {
                    'id': view_id,
                    'name': views[0].get('name', ''),
                    'workbook': views[0].get('workbook', {}).get('name', ''),
                    'content_url': views[0].get('contentUrl', '')
                }
                
                return view_id
            
            logging.error(f"Could not find view with name {view_name}")
            return None
            
        except Exception as e:
            logging.error(f"Error in direct view lookup: {str(e)}")
            return None
    
    @timing_metric
    async def fetch_data(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch data from Tableau view.
        
        Args:
            inputs: Dictionary containing input parameters including 'view_url' and optional 'query'
            
        Returns:
            Dict[str, Any]: Dictionary containing the fetched data
        """
        try:
            if not self._refresh_token_if_needed():
                return {'error': 'Failed to authenticate with Tableau'}
            
            view_url = inputs.get('view_url', '')
            user_query = inputs.get('query', '')
            
            if not view_url:
                return {'error': 'No view URL provided'}
                
            logging.info(f"Extracting view information from URL: {view_url}")
            
            # First get the view name
            view_name = self._extract_view_name(view_url)
            if not view_name:
                return {'error': f'Could not extract view name from URL: {view_url}'}
                
            logging.info(f"Extracted view name: {view_name}")
                
            # Then get the view ID using the view name
            view_id = self._get_view_id_from_name(view_name)
            if not view_id:
                return {'error': f'Could not find view ID for view name: {view_name}'}
                
            logging.info(f"Found view ID: {view_id}")
            
            # Fetch metadata first to understand the view structure
            view_metadata = self._fetch_view_metadata(view_id)
            if 'error' in view_metadata:
                return view_metadata
                
            # If we have a user query, parse it and determine relevant data fields
            query_components = {}
            relevant_fields = []
            if user_query:
                logging.info(f"Parsing user query: {user_query}")
                query_components = self._parse_user_query(user_query)
                
                # If view metadata has available fields, match query terms to fields
                if 'available_fields' in view_metadata:
                    relevant_fields = self._match_query_terms_to_fields(
                        query_components.get('entities', []),
                        view_metadata.get('available_fields', [])
                    )
                    logging.info(f"Matched query terms to fields: {relevant_fields}")
            
            # Fetch the view data with any query-derived filters
            additional_data = await self._fetch_additional_view_data_async(
                view_id, 
                query_components=query_components,
                relevant_fields=relevant_fields
            )
            
            # Combine everything into a response
            view_data = {
                    'metadata': {
                    'view_id': view_id,
                    'view_name': view_name,
                    'title': view_metadata.get('title', 'Unknown View'),
                    'workbook': view_metadata.get('workbook', 'Unknown Workbook'),
                    'owner': view_metadata.get('owner', 'Unknown'),
                    'created_at': view_metadata.get('created_at', 'Unknown'),
                    'updated_at': view_metadata.get('updated_at', 'Unknown'),
                    'view_url': view_url,
                    'query': user_query if user_query else None,
                    'query_components': query_components if query_components else None,
                    'relevant_fields': relevant_fields if relevant_fields else None
                },
                'content': additional_data
            }
                
            return view_data
                
        except requests.RequestException as e:
            logging.error(f"Request error when fetching view data: {e}")
            return {'error': f'Network error: {str(e)}'}
        except Exception as e:
            logging.error(f"Error in fetch_data: {str(e)}", exc_info=True)
            return {'error': str(e)}
    
    def _safely_extract(self, data: Dict[str, Any], path: List[str], default: Any) -> Any:
        """
        Safely extract a value from a nested dictionary using a path.
        
        Args:
            data: The dictionary to extract from
            path: List of keys to navigate the nested dictionary
            default: Default value if the path doesn't exist
            
        Returns:
            Any: The extracted value or the default
        """
        try:
            current = data
            for key in path:
                current = current.get(key, {})
                # If we hit a non-dict value before the end of the path, return default
                if not isinstance(current, dict) and key != path[-1]:
                    return default
            # Check if we ended up with an empty dict at the end
            if current == {} and path:
                return default
            return current
        except Exception:
            return default
    
    def _fetch_view_metadata(self, view_id: str) -> Dict[str, Any]:
        """
        Fetch metadata for a view to understand its structure.
        
        Args:
            view_id: The ID of the Tableau view
            
        Returns:
            Dict[str, Any]: Dictionary containing view metadata
        """
        try:
            # Ensure authentication token is valid
            if not self._refresh_token_if_needed():
                return {'error': 'Failed to authenticate with Tableau'}
                
            # Fetch the view data
            api_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views/{view_id}"
            
            headers = {
                "X-Tableau-Auth": self.auth_token,
                "Accept": "application/json"
            }
            
            logging.info(f"Fetching view metadata from API: {api_url}")
            response = requests.get(api_url, headers=headers, timeout=30)
            
            if response.status_code != 200:
                logging.error(f"Failed to fetch view metadata: Status {response.status_code}")
                return {'error': f'Failed to fetch Tableau view metadata: {response.status_code}'}
            
            # Parse the response
            try:
                data = response.json()
                
                # Extract metadata
                metadata = {
                    'title': self._safely_extract(data, ['view', 'name'], 'Unknown View'),
                    'workbook': self._safely_extract(data, ['view', 'workbook', 'name'], 'Unknown Workbook'),
                    'owner': self._safely_extract(data, ['view', 'owner', 'name'], 'Unknown'),
                    'created_at': self._safely_extract(data, ['view', 'createdAt'], 'Unknown'),
                    'updated_at': self._safely_extract(data, ['view', 'updatedAt'], 'Unknown'),
                }
                
                # Try to fetch available fields from the view
                fields = self._fetch_available_fields(view_id)
                if fields:
                    metadata['available_fields'] = fields
                
                return metadata
                
            except json.JSONDecodeError as e:
                logging.error(f"Error decoding JSON response for view metadata: {e}")
                return {'error': 'Invalid response format from Tableau API'}
                
        except Exception as e:
            logging.error(f"Error in fetch_view_metadata: {str(e)}")
            return {'error': str(e)}
    
    def _fetch_available_fields(self, view_id: str) -> List[Dict[str, Any]]:
        """
        Fetch available fields for a view.
        
        Args:
            view_id: The ID of the Tableau view
            
        Returns:
            List[Dict[str, Any]]: List of available fields
        """
        try:
            # This endpoint might vary depending on Tableau version
            # We'll try a few common patterns
            endpoints = [
                f"/api/{self.api_version}/sites/{self.site_id}/views/{view_id}/fields",
                f"/api/{self.api_version}/sites/{self.site_id}/views/{view_id}/data/fields"
            ]
            
            for endpoint in endpoints:
                api_url = f"{self.base_url}{endpoint}"
                
                headers = {
                    "X-Tableau-Auth": self.auth_token,
                    "Accept": "application/json"
                }
                
                logging.info(f"Trying to fetch fields from: {api_url}")
                response = requests.get(api_url, headers=headers, timeout=30)
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        fields = data.get('fields', {}).get('field', [])
                        if fields:
                            # Convert to standardized format
                            return [
                                {
                                    'name': field.get('name', ''),
                                    'dataType': field.get('dataType', ''),
                                    'description': field.get('description', '')
                                }
                                for field in fields
                            ]
                    except json.JSONDecodeError:
                        continue
            
            # If direct field API fails, fall back to data extract
            # We can infer fields from the column headers
            logging.info("Falling back to data extract to infer fields")
            data_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views/{view_id}/data"
            headers = {
                "X-Tableau-Auth": self.auth_token,
                "Accept": "application/json"
            }
            
            response = requests.get(data_url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    # Structure depends on Tableau API version
                    columns = []
                    
                    # Try different ways to extract column names
                    if 'columns' in data:
                        columns = data.get('columns', [])
                    elif 'table' in data and 'headers' in data['table']:
                        columns = data['table']['headers']
                    elif 'data' in data and len(data['data']) > 0:
                        # Infer columns from first row
                        first_row = data['data'][0]
                        if isinstance(first_row, dict):
                            columns = list(first_row.keys())
                    
                    if columns:
                        return [{'name': col} for col in columns]
                except:
                    pass
                    
            # If all direct methods fail, try to extract from view data response
            # which might contain columns/fields information
            logging.info("Trying to extract fields from view data")
            view_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views/{view_id}"
            response = requests.get(view_url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    
                    # Look for field information in the response
                    # (structure depends on Tableau API version)
                    fields = []
                    
                    # Check all possible paths where field info could be
                    paths = [
                        ['view', 'columns'],
                        ['view', 'fields'],
                        ['view', 'dataSource', 'fields'],
                        ['fields']
                    ]
                    
                    for path in paths:
                        field_data = self._safely_extract(data, path, None)
                        if field_data and isinstance(field_data, (list, dict)):
                            if isinstance(field_data, dict):
                                fields = [{'name': k} for k in field_data.keys()]
                            else:
                                fields = [{'name': f} if isinstance(f, str) else f for f in field_data]
                            break
                    
                    if fields:
                        return fields
                except:
                    pass
            
            # If all API approaches fail, return empty list
            logging.warning("Could not retrieve field information for the view")
            return []
            
        except Exception as e:
            logging.error(f"Error fetching available fields: {str(e)}")
            return []
            
    def _parse_user_query(self, query: str) -> Dict[str, Any]:
        """
        Parse a natural language query to extract entities, timeframes, and aggregations.
        
        Args:
            query: The user's natural language query
            
        Returns:
            Dict[str, Any]: Dictionary with query components
        """
        # Initialize components
        components = {
            'entities': [],      # Extracted entities (e.g., 'sales', 'revenue', 'region')
            'timeframes': [],    # Extracted time ranges (e.g., 'Q1 2024', 'last month')
            'aggregations': [],  # Extracted aggregations (e.g., 'total', 'average')
            'filters': {},       # Extracted filters as field:value pairs
            'sort_by': None,     # Sort field
            'sort_order': None,  # Sort order (asc/desc)
            'limit': None,       # Result limit
            'original_query': query
        }
        
        try:
            # Try to use NLTK for tokenization
            stop_words = set()
            try:
                stop_words = set(stopwords.words('english'))
            except Exception as e:
                logging.warning(f"Could not load stopwords: {e}")
            
            tokens = []
            try:
                # Use word_tokenize with error handling
                tokens = word_tokenize(query.lower())
            except LookupError as e:
                # Fallback to simple tokenization if NLTK resources aren't available
                logging.warning(f"NLTK tokenization failed: {e}. Using simple tokenization.")
                tokens = query.lower().split()
            except Exception as e:
                logging.warning(f"Tokenization error: {e}. Using simple tokenization.")
                tokens = query.lower().split()
                
            # Clean tokens (remove punctuation and stopwords)
            clean_tokens = []
            for t in tokens:
                # Keep only alphanumeric tokens and remove stopwords
                if t.isalnum() and t not in stop_words:
                    clean_tokens.append(t)
            
            components['entities'] = clean_tokens
        except Exception as e:
            logging.warning(f"Error during NLP processing: {e}. Using simple word extraction.")
            # Ultra fallback - just split the query by spaces and remove common words
            simple_tokens = query.lower().split()
            common_words = {'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'from', 'about', 'as', 'of', 'me', 'give', 'show', 'display', 'get'}
            components['entities'] = [token for token in simple_tokens if token not in common_words and len(token) > 2]
        
        # Extract timeframes using regex patterns
        # This doesn't rely on NLTK so should work regardless
        time_patterns = [
            r'q[1-4]\s+\d{4}',           # Q1 2024
            r'\d{4}',                     # 2024
            r'jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?',  # months
            r'last\s+(?:day|week|month|quarter|year)',  # relative time
            r'yesterday|today|this\s+(?:week|month|year)',  # more relative time
            r'\d{1,2}/\d{1,2}/\d{2,4}',   # MM/DD/YYYY
            r'\d{4}-\d{2}-\d{2}'          # YYYY-MM-DD
        ]
        
        for pattern in time_patterns:
            matches = re.findall(pattern, query.lower())
            if matches:
                components['timeframes'].extend(matches)
        
        # Extract aggregations - simple token matching
        agg_terms = ['total', 'sum', 'average', 'avg', 'mean', 'median', 'max', 'maximum', 'min', 'minimum', 'count']
        for token in components['entities']:
            if token.lower() in agg_terms:
                components['aggregations'].append(token.lower())
                # Remove from entities since we've identified it as aggregation
                if token in components['entities']:
                    components['entities'].remove(token)
        
        # Extract sort instructions
        sort_patterns = [
            r'(?:sort|order)\s+by\s+(\w+)',  # 'sort by sales', 'order by date'
            r'(?:top|bottom)\s+(\d+)'        # 'top 10', 'bottom 5'
        ]
        
        for pattern in sort_patterns:
            matches = re.findall(pattern, query.lower())
            if matches:
                if 'top' in query.lower() or 'bottom' in query.lower():
                    try:
                        components['limit'] = int(matches[0])
                        components['sort_order'] = 'desc' if 'top' in query.lower() else 'asc'
                    except (ValueError, IndexError):
                        pass
                else:
                    components['sort_by'] = matches[0]
                    # Determine sort direction
                    if 'desc' in query.lower() or 'decreasing' in query.lower() or 'highest' in query.lower():
                        components['sort_order'] = 'desc'
                    elif 'asc' in query.lower() or 'increasing' in query.lower() or 'lowest' in query.lower():
                        components['sort_order'] = 'asc'
            
        # Clean up entities list by removing duplicates and already processed items
        processed_terms = set(components['timeframes'] + components['aggregations'])
        if components['sort_by']:
            processed_terms.add(components['sort_by'])
            
        components['entities'] = list(set([e for e in components['entities'] if e not in processed_terms]))
        
        return components
    
    def _match_query_terms_to_fields(self, query_terms: List[str], available_fields: List[Dict[str, Any]]) -> List[str]:
        """
        Match query terms to available fields using fuzzy matching.
        
        Args:
            query_terms: List of terms extracted from user query
            available_fields: List of available fields from Tableau
            
        Returns:
            List[str]: List of field names that match the query terms
        """
        if not query_terms or not available_fields:
            return []
            
        # Extract just the field names for easier matching
        field_names = [f['name'].lower() for f in available_fields if 'name' in f]
        
        # Handle common synonyms and aliases
        synonyms = {
            'sales': ['revenue', 'income', 'earnings'],
            'profit': ['margin', 'earnings', 'gains'],
            'customer': ['client', 'user', 'buyer', 'consumer'],
            'product': ['item', 'goods', 'merchandise'],
            'date': ['time', 'period', 'day', 'month', 'year'],
            'region': ['area', 'location', 'territory', 'country', 'state', 'city'],
            'category': ['group', 'class', 'type', 'segment']
        }
        
        # Build expanded query terms with synonyms
        expanded_terms = set(query_terms)
        for term in query_terms:
            # Check if term is in our synonyms keys
            if term in synonyms:
                expanded_terms.update(synonyms[term])
            # Check if term is in any synonym values
            for key, values in synonyms.items():
                if term in values:
                    expanded_terms.add(key)
                    expanded_terms.update(values)
                    
        # Match expanded terms to field names
        matched_fields = set()
        
        for term in expanded_terms:
            # Try direct match first
            direct_matches = [f for f in field_names if term in f or f in term]
            if direct_matches:
                matched_fields.update(direct_matches)
                continue
                
            # If no direct match, try fuzzy matching
            close_matches = get_close_matches(term, field_names, n=2, cutoff=0.7)
            if close_matches:
                matched_fields.update(close_matches)
                
        # Get the original case versions of the field names
        original_case_fields = []
        for matched in matched_fields:
            for field in available_fields:
                if field.get('name', '').lower() == matched:
                    original_case_fields.append(field.get('name'))
                    break
        
        return original_case_fields
    
    @timing_metric
    async def _fetch_additional_view_data_async(self, view_id: str, query_components: Dict[str, Any] = None, relevant_fields: List[str] = None) -> Dict[str, Any]:
        """
        Fetch additional data for a view asynchronously to support async fetch_data.
        
        Args:
            view_id: The ID of the Tableau view
            query_components: Components extracted from user query
            relevant_fields: Fields that are relevant to the query
            
        Returns:
            Dict[str, Any]: Dictionary containing additional view data
        """
        # Delegate to the synchronous method for now
        return self._fetch_additional_view_data(view_id, query_components, relevant_fields)
    
    @timing_metric
    def _fetch_view_filters(self, view_id: str) -> List[Dict[str, Any]]:
        """
        Fetch available filters for a view.
        
        Args:
            view_id: The ID of the Tableau view
            
        Returns:
            List[Dict[str, Any]]: List of available filters
        """
        try:
            # Check cache first
            cache_key = f"filters_{view_id}"
            if hasattr(self, 'filters_cache') and cache_key in self.filters_cache and hasattr(self, 'filters_cache_time') and (time.time() - self.filters_cache_time) < getattr(self, 'filters_cache_ttl', 3600):
                self._log_metric("cache_hits", 1)
                return self.filters_cache[cache_key]
                
            self._log_metric("cache_misses", 1)
            
            # Try the filters endpoint
            api_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views/{view_id}/filters"
            
            headers = {
                "X-Tableau-Auth": self.auth_token,
                "Accept": "application/json"
            }
            
            self._log_metric("queries_made", 1)
            logging.info(f"Trying to fetch filters from: {api_url}")
            response = requests.get(api_url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    self._log_metric("total_response_size_bytes", len(response.content))
                    
                    filters_data = data.get('filters', {}).get('filter', [])
                    
                    if isinstance(filters_data, dict):
                        # Handle case where there's only one filter
                        filters_data = [filters_data]
                    
                    # Process and format filters
                    filters = []
                    for filter_item in filters_data:
                        filter_obj = {
                            'name': filter_item.get('name', ''),
                            'type': filter_item.get('type', ''),
                            'values': filter_item.get('values', {}).get('value', []),
                            'field': filter_item.get('field', {}).get('name', '')
                        }
                        filters.append(filter_obj)
                    
                    # Ensure cache exists
                    if not hasattr(self, 'filters_cache'):
                        self.filters_cache = {}
                        self.filters_cache_time = 0
                    
                    # Store in cache
                    self.filters_cache[cache_key] = filters
                    self.filters_cache_time = time.time()
                    
                    logging.info(f"Successfully fetched {len(filters)} filters for view {view_id}")
                    return filters
                except Exception as e:
                    logging.warning(f"Error parsing filter data: {str(e)}")
                    # Continue to fallback method
                    
            # Fallback method - try alternative API approach
            try:
                # Try data API approach
                api_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views/{view_id}/data/filters"
                response = requests.get(api_url, headers=headers, timeout=30)
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        filters_data = data.get('filters', {}).get('filter', [])
                        
                        if isinstance(filters_data, dict):
                            filters_data = [filters_data]
                        
                        filters = []
                        for filter_item in filters_data:
                            filter_obj = {
                                'name': filter_item.get('name', ''),
                                'type': filter_item.get('type', ''),
                                'values': filter_item.get('values', {}).get('value', []),
                                'field': filter_item.get('field', {}).get('name', '')
                            }
                            filters.append(filter_obj)
                        
                        # Ensure cache exists
                        if not hasattr(self, 'filters_cache'):
                            self.filters_cache = {}
                            self.filters_cache_time = 0
                            
                        # Store in cache
                        self.filters_cache[cache_key] = filters
                        self.filters_cache_time = time.time()
                        
                        logging.info(f"Successfully fetched {len(filters)} filters for view {view_id} using alternative method")
                        return filters
                    except Exception as e:
                        logging.warning(f"Error parsing filter data from alternative method: {str(e)}")
            except Exception as e:
                logging.warning(f"Error using alternative method for filters: {str(e)}")
            
            # If all API approaches fail, return empty list
            logging.warning("Could not retrieve filter information for the view")
            return []
            
        except Exception as e:
            logging.error(f"Error fetching available filters: {str(e)}")
            return []
    
    @timing_metric
    def _fetch_additional_view_data(self, view_id: str, query_components: Dict[str, Any] = None, relevant_fields: List[str] = None) -> Dict[str, Any]:
        """
        Fetch additional data for a view.
        
        Args:
            view_id: The ID of the Tableau view
            query_components: Components extracted from user query
            relevant_fields: Fields that are relevant to the query
            
        Returns:
            Dict[str, Any]: Dictionary containing additional view data
        """
        try:
            # Ensure authentication token is valid
            if not self._refresh_token_if_needed():
                error = AuthenticationError("Failed to authenticate with Tableau")
                logging.error(f"{error.error_type}: {error.message}")
                return {
                    'error': error.to_dict(),
                    'note': 'Could not authenticate with Tableau server to fetch additional data'
                }
                
            # 1. Fetch filters available for this view
            filters = self._fetch_view_filters(view_id)
            
            # 2. Fetch parameters available for this view
            parameters = self._fetch_view_parameters(view_id)
            
            # 3. Fetch sheets in this view
            sheets = self._fetch_view_sheets(view_id)
            
            # 4. Determine filter parameters based on query components
            filter_params = {}
            if query_components:
                filter_params = self._build_filter_params(query_components, filters)
                
            # 5. Determine parameter values based on query components
            parameter_values = {}
            if query_components:
                parameter_values = self._build_parameter_values(query_components, parameters)
            
            # 6. Fetch the actual data with filters applied
            data_table, data_summary = self._fetch_view_data(view_id, filter_params, parameter_values, relevant_fields)
            
            # 7. If this is a dashboard, fetch data from each contained sheet
            sheet_data = {}
            if sheets and len(sheets) > 1:  # If there are multiple sheets, this is a dashboard
                for sheet in sheets:
                    if sheet['id'] != view_id:  # Don't re-fetch data for the main view
                        try:
                            sheet_id = sheet['id']
                            sheet_name = sheet['name']
                            
                            # Fetch the data for this sheet with the same filters and parameters
                            sheet_table, sheet_summary = self._fetch_view_data(
                                sheet_id, 
                                filter_params, 
                                parameter_values, 
                                relevant_fields
                            )
                            
                            if sheet_table is not None:
                                sheet_data[sheet_name] = {
                                    'id': sheet_id,
                                    'name': sheet_name,
                                    'data': sheet_table,
                                    'summary': sheet_summary
                                }
                        except Exception as e:
                            logging.warning(f"Error fetching data for sheet {sheet.get('name', 'unknown')}: {str(e)}")
                            # Continue with other sheets even if one fails
            
            result = {
                'sheets': sheets,
                'available_filters': filters,
                'applied_filters': filter_params,
                'available_parameters': parameters,
                'applied_parameters': parameter_values,
                'summary': data_summary
            }
            
            # Add the data if available
            if data_table is not None:
                result['data'] = data_table
                
            # Add sheet data if available
            if sheet_data:
                result['sheet_data'] = sheet_data
            
            return result
            
        except TableauError as e:
            logging.error(f"{e.error_type}: {e.message}")
            return {
                'error': e.to_dict(),
                'note': 'Error occurred while fetching additional data from Tableau'
            }
        except Exception as e:
            error_msg = f"Error fetching additional view data: {str(e)}"
            logging.error(error_msg)
            error = DataAccessError(
                error_msg,
                {"view_id": view_id}
            )
            
            return {
                'error': error.to_dict(),
                'note': 'Error occurred while fetching additional data from Tableau'
            }
    
    def _fetch_view_sheets(self, view_id: str) -> List[Dict[str, Any]]:
        """
        Fetch available sheets for a view.
        
        Args:
            view_id: The ID of the Tableau view
            
        Returns:
            List[Dict[str, Any]]: List of available sheets
        """
        try:
            # This endpoint might vary depending on Tableau version
            # We'll try a few common patterns
            endpoints = [
                f"/api/{self.api_version}/sites/{self.site_id}/views/{view_id}/sheets",
                f"/api/{self.api_version}/sites/{self.site_id}/views/{view_id}/data/sheets"
            ]
            
            headers = {
                "X-Tableau-Auth": self.auth_token,
                "Accept": "application/json"
            }
            
            self._log_metric("queries_made", 1)
            
            for endpoint in endpoints:
                api_url = f"{self.base_url}{endpoint}"
                logging.info(f"Trying to fetch sheets from: {api_url}")
                
                try:
                    response = requests.get(api_url, headers=headers, timeout=30)
                    
                    if response.status_code == 200:
                        try:
                            data = response.json()
                            self._log_metric("total_response_size_bytes", len(response.content))
                            
                            sheets_data = data.get('sheets', {}).get('sheet', [])
                            
                            if isinstance(sheets_data, dict):
                                # Handle case where there's only one sheet
                                sheets_data = [sheets_data]
                            
                            # Process and format sheets
                            sheets = []
                            for sheet_item in sheets_data:
                                sheet_obj = {
                                    'id': sheet_item.get('id', ''),
                                    'name': sheet_item.get('name', ''),
                                    'type': sheet_item.get('sheetType', ''),
                                    'url': sheet_item.get('contentUrl', '')
                                }
                                sheets.append(sheet_obj)
                                
                            logging.info(f"Successfully fetched {len(sheets)} sheets for view {view_id}")
                            return sheets
                        except Exception as e:
                            logging.warning(f"Error parsing sheets data: {str(e)}")
                            # Continue to next endpoint
                except Exception as e:
                    logging.warning(f"Error fetching sheets from {api_url}: {str(e)}")
                    # Continue to next endpoint
            
            # If direct API calls fail, try to parse the sheet info from view metadata
            try:
                metadata = self._fetch_view_metadata(view_id)
                workbook = metadata.get('workbook', {})
                sheets = workbook.get('sheets', {}).get('sheet', [])
                
                if isinstance(sheets, dict):
                    sheets = [sheets]
                    
                if sheets:
                    formatted_sheets = [
                        {
                            'id': sheet.get('id', ''),
                            'name': sheet.get('name', ''),
                            'type': sheet.get('sheetType', ''),
                            'url': sheet.get('contentUrl', '')
                        } for sheet in sheets
                    ]
                    
                    if sheets:
                        return formatted_sheets
            except:
                pass
            
            # If all API approaches fail, return empty list
            logging.warning("Could not retrieve sheet information for the view")
            return []
            
        except Exception as e:
            logging.error(f"Error fetching available sheets: {str(e)}")
            return []
    
    def _build_filter_params(self, query_components: Dict[str, Any], available_filters: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Build filter parameters based on query components and available filters.
        
        Args:
            query_components: Components extracted from user query
            available_filters: List of available filters for the view
            
        Returns:
            Dict[str, Any]: Dictionary containing filter parameters
        """
        filter_params = {}
        
        for filter_item in available_filters:
            filter_name = filter_item['name'].lower()
            
            # Check if filter is in query components
            if filter_name in query_components:
                filter_value = query_components[filter_name]
                
                # Convert filter value to appropriate format
                if isinstance(filter_value, list):
                    filter_params[filter_name] = {
                        'type': 'multi',
                        'values': filter_value
                    }
                elif isinstance(filter_value, dict):
                    filter_params[filter_name] = {
                        'type': 'range',
                        'min': filter_value.get('min'),
                        'max': filter_value.get('max')
                    }
                else:
                    filter_params[filter_name] = filter_value
            else:
                filter_params[filter_name] = None
        
        return filter_params
    
    @timing_metric
    def _fetch_view_parameters(self, view_id: str) -> List[Dict[str, Any]]:
        """
        Fetch available parameters for a view.
        
        Args:
            view_id: The ID of the Tableau view
            
        Returns:
            List[Dict[str, Any]]: List of available parameters
        """
        try:
            # Check cache first
            cache_key = f"parameters_{view_id}"
            if cache_key in self.parameters_cache and (time.time() - self.parameters_cache_time) < self.parameters_cache_ttl:
                self._log_metric("cache_hits", 1)
                return self.parameters_cache[cache_key]
                
            self._log_metric("cache_misses", 1)
            
            # Try the parameters endpoint
            api_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views/{view_id}/parameters"
            
            headers = {
                "X-Tableau-Auth": self.auth_token,
                "Accept": "application/json"
            }
            
            self._log_metric("queries_made", 1)
            logging.info(f"Fetching view parameters from: {api_url}")
            response = requests.get(api_url, headers=headers, timeout=30)
            
            request_time = time.time() - time.time()  # This will be close to 0, but we're just initializing
            self._log_metric("total_request_time_seconds", request_time)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    self._log_metric("total_response_size_bytes", len(response.content))
                    
                    parameters_data = data.get('parameters', {}).get('parameter', [])
                    
                    if parameters_data:
                        parameters = [
                            {
                                'name': p.get('name', ''),
                                'dataType': p.get('dataType', ''),
                                'allowableValues': p.get('allowableValues', {}).get('allowableValue', []),
                                'currentValue': p.get('currentValue', {}).get('value', None)
                            }
                            for p in parameters_data
                        ]
                        
                        # Cache the parameters
                        self.parameters_cache[cache_key] = parameters
                        self.parameters_cache_time = time.time()
                        
                        return parameters
                except json.JSONDecodeError:
                    logging.warning("Failed to parse parameters JSON response")
            
            # If parameters endpoint fails, try alternative approaches
            # Workbooks endpoint might have parameter information
            workbook_id = self._get_workbook_id_for_view(view_id)
            if workbook_id:
                alternate_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/workbooks/{workbook_id}/parameters"
                logging.info(f"Trying workbook parameters endpoint: {alternate_url}")
                
                response = requests.get(alternate_url, headers=headers, timeout=30)
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        parameters_data = data.get('parameters', {}).get('parameter', [])
                        
                        if parameters_data:
                            parameters = [
                                {
                                    'name': p.get('name', ''),
                                    'dataType': p.get('dataType', ''),
                                    'allowableValues': p.get('allowableValues', {}).get('allowableValue', []),
                                    'currentValue': p.get('currentValue', {}).get('value', None)
                                }
                                for p in parameters_data
                            ]
                            
                            # Cache the parameters
                            self.parameters_cache[cache_key] = parameters
                            self.parameters_cache_time = time.time()
                            
                            return parameters
                    except Exception as e:
                        logging.warning(f"Error parsing parameters from workbook endpoint: {str(e)}")
            
            # If all API approaches fail, return empty list
            logging.warning("Could not retrieve parameter information for the view")
            return []
            
        except Exception as e:
            logging.error(f"Error fetching available parameters: {str(e)}")
            return []
    
    def _get_workbook_id_for_view(self, view_id: str) -> Optional[str]:
        """
        Get the workbook ID for a given view ID.
        
        Args:
            view_id: The ID of the Tableau view
            
        Returns:
            Optional[str]: The workbook ID if found, None otherwise
        """
        try:
            # Try to get the view details which should contain workbook info
            api_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views/{view_id}"
            
            headers = {
                "X-Tableau-Auth": self.auth_token,
                "Accept": "application/json"
            }
            
            self._log_metric("queries_made", 1)
            logging.info(f"Fetching view details to get workbook ID: {api_url}")
            response = requests.get(api_url, headers=headers, timeout=30)
            
            request_time = time.time() - time.time()  # Close to 0, but initializing
            self._log_metric("total_request_time_seconds", request_time)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    self._log_metric("total_response_size_bytes", len(response.content))
                    
                    # Extract workbook ID from view data
                    workbook = self._safely_extract(data, ['view', 'workbook'], None)
                    if workbook and 'id' in workbook:
                        return workbook['id']
                except json.JSONDecodeError:
                    logging.warning("Failed to parse view JSON response")
            
            return None
            
        except Exception as e:
            logging.error(f"Error getting workbook ID for view: {str(e)}")
            return None
    
    def _format_bytes(self, bytes_value: int) -> str:
        """
        Format bytes value to human-readable string.
        
        Args:
            bytes_value: Size in bytes
            
        Returns:
            str: Formatted string with appropriate unit
        """
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_value < 1024 or unit == 'GB':
                return f"{bytes_value:.2f} {unit}"
            bytes_value /= 1024
        return f"{bytes_value:.2f} GB"
    
    def _calculate_cache_health(self) -> str:
        """
        Calculate cache health percentage.
        
        Returns:
            str: Cache health as a percentage
        """
        hits = self.metrics.get("cache_hits", 0)
        misses = self.metrics.get("cache_misses", 0)
        
        total = hits + misses
        if total == 0:
            return "N/A"
        
        health = (hits / total) * 100
        return f"{health:.1f}%"
    
    def reset_metrics(self) -> None:
        """Reset all metrics counters to zero."""
        self.metrics = {
            "queries_made": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "total_response_size_bytes": 0,
            "total_request_time_seconds": 0,
            "error_count": 0
        }
        logging.info("Tableau metrics have been reset")
    
    def get_metrics(self) -> Dict[str, Any]:
        """
        Get current metrics for the Tableau data source.
        
        Returns:
            Dict[str, Any]: Dictionary containing current metrics
        """
        return {
            "queries_made": self.metrics.get("queries_made", 0),
            "cache_hits": self.metrics.get("cache_hits", 0),
            "cache_misses": self.metrics.get("cache_misses", 0),
            "total_response_size": self._format_bytes(self.metrics.get("total_response_size_bytes", 0)),
            "total_request_time": f"{self.metrics.get('total_request_time_seconds', 0):.2f} seconds",
            "error_count": self.metrics.get("error_count", 0),
            "cache_health": self._calculate_cache_health()
        }
    
    def health_check(self) -> Dict[str, Any]:
        """
        Perform a health check for the Tableau service.
        
        Returns:
            Dict[str, Any]: Dictionary containing health status information
        """
        if self.connection_error:
            # Try to re-authenticate if failed previously
            success = self.test_authentication()
            if success:
                return {
                    "status": "healthy",
                    "message": "Tableau service is connected after re-authentication",
                    "site_id": self.site_id,
                    "metrics": self.get_metrics()
                }
            return {
                "status": "error",
                "message": self.connection_error,
                "metrics": self.get_metrics()
            }
        
        if not self.auth_token or not self.site_id:
            return {
                "status": "error",
                "message": "No valid authentication token or site ID",
                "metrics": self.get_metrics()
            }
        
        return {
            "status": "healthy",
            "message": "Tableau service is connected",
            "site_id": self.site_id,
            "metrics": self.get_metrics()
        }
    
    def format_for_llm(self, data: Dict[str, Any]) -> str:
        """
        Format the fetched Tableau data for LLM consumption.
        
        Args:
            data: Dictionary containing the Tableau data
            
        Returns:
            str: Formatted string for LLM consumption
        """
        if not data:
            return "Error retrieving Tableau data: No data available"
            
        if isinstance(data, dict) and 'error' in data:
            # Check if it's a structured error
            error = data.get('error')
            if isinstance(error, dict) and 'error_type' in error:
                return f"""
# ERROR RETRIEVING TABLEAU DATA
Error Type: {error.get('error_type', 'Unknown')}
Message: {error.get('message', 'Unknown error')}
Details: {json.dumps(error.get('details', {}), indent=2)}
Note: {data.get('note', 'No additional information available')}
"""
            else:
                # Legacy error format
                return f"Error retrieving Tableau data: {data.get('error', 'Unknown error')}"
        
        formatted = []
        formatted.append("# TABLEAU DASHBOARD DATA")
        formatted.append("The following contains data from a Tableau dashboard view.")
        
        # Add metadata section
        if 'metadata' in data:
            formatted.append("\n## METADATA")
            formatted.append("```metadata")
            for key, value in data['metadata'].items():
                if key not in ['query_components', 'relevant_fields'] or value:  # Skip empty query components
                    formatted.append(f"{key}: {value}")
            formatted.append("```")
        
        # Add content section
        if 'content' in data:
            formatted.append("\n## CONTENT")
            
            content = data['content']
            
            # Add note
            if 'note' in content:
                formatted.append(f"\n### NOTE")
                formatted.append(content['note'])
            
            # Add summary
            if 'summary' in content:
                formatted.append(f"\n### SUMMARY")
                formatted.append(content['summary'])
            
            # Add available filters if any
            if 'available_filters' in content and content['available_filters']:
                formatted.append(f"\n### AVAILABLE FILTERS")
                formatted.append("```filters")
                for filter_item in content['available_filters']:
                    formatted.append(f"- {filter_item['name']} ({filter_item.get('type', 'unknown')})")
                formatted.append("```")
            
            # Add applied filters if any
            if 'applied_filters' in content and content['applied_filters']:
                formatted.append(f"\n### APPLIED FILTERS")
                formatted.append("```applied_filters")
                for name, value in content['applied_filters'].items():
                    formatted.append(f"- {name}: {value}")
                formatted.append("```")
            
            # Add available parameters if any
            if 'available_parameters' in content and content['available_parameters']:
                formatted.append(f"\n### AVAILABLE PARAMETERS")
                formatted.append("```parameters")
                for param in content['available_parameters']:
                    formatted.append(f"- {param['name']} ({param.get('dataType', 'unknown')}) = {param.get('currentValue', 'None')}")
                formatted.append("```")
            
            # Add applied parameters if any
            if 'applied_parameters' in content and content['applied_parameters']:
                formatted.append(f"\n### APPLIED PARAMETERS")
                formatted.append("```applied_parameters")
                for name, value in content['applied_parameters'].items():
                    formatted.append(f"- {name}: {value}")
                formatted.append("```")
                
            # Add data preview
            if 'data' in content and content['data']:
                formatted.append(f"\n### DATA PREVIEW")
                formatted.append("```data")
                
                # Get all column names from the data
                columns = set()
                for row in content['data'][:10]:  # Look at first 10 rows
                    columns.update(row.keys())
                
                columns = sorted(list(columns))
                
                # Add column headers
                formatted.append(" | ".join(columns))
                formatted.append("-" * (sum(len(c) for c in columns) + 3 * (len(columns) - 1)))
                
                # Add rows (max 20)
                for row in content['data'][:20]:
                    formatted.append(" | ".join(str(row.get(col, '')) for col in columns))
                
                # Add indicator if there are more rows
                if len(content['data']) > 20:
                    formatted.append(f"... and {len(content['data']) - 20} more rows")
                    
                formatted.append("```")
            
            # Add sheets if available
            if 'sheets' in content and content['sheets']:
                formatted.append(f"\n### SHEETS")
                formatted.append("```sheets")
                for sheet in content['sheets']:
                    formatted.append(f"- {sheet.get('name', 'Unknown')} ({sheet.get('type', 'unknown')})")
                formatted.append("```")
            
            # Add sheet data if available
            if 'sheet_data' in content and content['sheet_data']:
                formatted.append(f"\n### SHEET DATA")
                for sheet_name, sheet_info in content['sheet_data'].items():
                    formatted.append(f"\n#### {sheet_name}")
                    formatted.append(f"Summary: {sheet_info.get('summary', 'No summary available')}")
                    
                    # Add preview of sheet data
                    sheet_rows = sheet_info.get('data', [])
                    if sheet_rows:
                        formatted.append("```data")
                        
                        # Get columns for this sheet
                        sheet_columns = set()
                        for row in sheet_rows[:10]:
                            sheet_columns.update(row.keys())
                        
                        sheet_columns = sorted(list(sheet_columns))
                        
                        # Add column headers
                        formatted.append(" | ".join(sheet_columns))
                        formatted.append("-" * (sum(len(c) for c in sheet_columns) + 3 * (len(sheet_columns) - 1)))
                        
                        # Add rows (max 10 per sheet)
                        for row in sheet_rows[:10]:
                            formatted.append(" | ".join(str(row.get(col, '')) for col in sheet_columns))
                        
                        # Add indicator if there are more rows
                        if len(sheet_rows) > 10:
                            formatted.append(f"... and {len(sheet_rows) - 10} more rows")
                            
                        formatted.append("```")
        
        # Add link to the original dashboard
        if 'metadata' in data and 'view_url' in data['metadata']:
            formatted.append("\n## LINK TO DASHBOARD")
            formatted.append(f"For a complete interactive experience, view the dashboard at: {data['metadata']['view_url']}")
        
        # Add query information if present
        if 'metadata' in data and 'query' in data['metadata'] and data['metadata']['query']:
            formatted.append("\n## USER QUERY")
            formatted.append(f"Original query: {data['metadata']['query']}")
            
            # Add query interpretation if available
            if 'query_components' in data['metadata'] and data['metadata']['query_components']:
                components = data['metadata']['query_components']
                formatted.append("\nQuery interpretation:")
                if components.get('entities'):
                    formatted.append(f"- Entities: {', '.join(components['entities'])}")
                if components.get('timeframes'):
                    formatted.append(f"- Time frames: {', '.join(components['timeframes'])}")
                if components.get('aggregations'):
                    formatted.append(f"- Aggregations: {', '.join(components['aggregations'])}")
        
        # Add metrics information
        formatted.append("\n## PROCESSING METRICS")
        formatted.append("```metrics")
        formatted.append(f"Data size: {self._format_bytes(self.metrics.get('total_response_size_bytes', 0))}")
        formatted.append(f"Queries made: {self.metrics.get('queries_made', 0)}")
        formatted.append(f"Processing time: {self.metrics.get('total_request_time_seconds', 0):.2f} seconds")
        formatted.append(f"Cache hits: {self.metrics.get('cache_hits', 0)}")
        formatted.append(f"Cache misses: {self.metrics.get('cache_misses', 0)}")
        formatted.append("```")
        
        return "\n".join(formatted)
    
    def get_form_fields(self) -> Dict[str, Any]:
        """
        Get form fields for data source configuration.
        
        Returns:
            Dict[str, Any]: Dictionary defining the form fields for this data source
        """
        return {
            "title": "Tableau Dashboard",
            "description": "Query data from a Tableau dashboard or view",
            "fields": [
                {
                    "name": "view_url",
                    "type": "text",
                    "label": "Tableau View URL",
                    "placeholder": "https://tableau.razorpay.in/views/workbook/view",
                    "required": True,
                    "help_text": "The URL of the Tableau view or dashboard you want to analyze"
                }
            ]
        }
    
    def test_authentication(self) -> bool:
        """
        Test the Tableau authentication explicitly and return detailed results.
        This method can be called directly to troubleshoot authentication issues.
        
        Returns:
            bool: True if authentication was successful, False otherwise
        """
        print(f"\n=== TESTING TABLEAU AUTHENTICATION ===")
        print(f"Base URL: {self.base_url}")
        print(f"API Version: {self.api_version}")
        print(f"Auth URL: {self.auth_url}")
        print(f"Token Name: {self.token_name}")
        print(f"Token Secret: {'*' * 10}")  # Masked for security
        
        try:
            # Construct the auth request manually for testing
            url = self.auth_url
            headers = {
                "Content-Type": "application/json",
                "Accept": "*/*"
            }
            
            # Use the format known to work
            payload = {
                "credentials": {
                    "personalAccessTokenName": self.token_name,
                    "personalAccessTokenSecret": self.token_secret,
                    "site": {"contentUrl": ""}
                }
            }
            
            print(f"\nTrying authentication...")
            masked_payload = {
                "credentials": {
                    "personalAccessTokenName": self.token_name,
                    "personalAccessTokenSecret": "***MASKED***",
                    "site": {"contentUrl": ""}
                }
            }
            print(f"Request: POST {url}")
            print(f"Headers: {headers}")
            print(f"Payload: {json.dumps(masked_payload, indent=2)}")
            
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            print(f"Response Status: {response.status_code}")
            print(f"Response Headers: {dict(response.headers)}")
            print(f"Response Content: {response.text[:200]}...")
            
            if response.status_code == 200:
                print("\n✅ AUTHENTICATION SUCCESSFUL!")
                
                # Parse the XML response
                try:
                    root = ET.fromstring(response.text)
                    ns = {"ts": "http://tableau.com/api"}
                    
                    credentials = root.find(".//ts:credentials", ns) or root.find(".//credentials")
                    site = None
                    if credentials is not None:
                        site = credentials.find(".//ts:site", ns) or credentials.find(".//site")
                    
                    if credentials is not None and site is not None:
                        auth_token = credentials.get("token")
                        site_id = site.get("id")
                        
                        print(f"Token: {auth_token[:10]}...")
                        print(f"Site ID: {site_id}")
                        
                        # Save these values for future use
                        self.auth_token = auth_token
                        self.site_id = site_id
                        return True
                except ET.ParseError as e:
                    print(f"XML Parse Error: {e}")
            else:
                print(f"❌ Authentication failed with status {response.status_code}")
                if response.status_code == 401:
                    print("The credentials appear to be invalid. Please check token name and secret.")
                elif "xml" in response.text.lower():
                    try:
                        root = ET.fromstring(response.text)
                        error = root.find(".//error")
                        if error is not None:
                            summary = error.find(".//summary")
                            detail = error.find(".//detail")
                            if summary is not None and detail is not None:
                                print(f"Error: {summary.text} - {detail.text}")
                    except:
                        pass
            
            return False
                
        except Exception as e:
            print(f"❌ ERROR: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
        
    def _build_parameter_values(self, query_components: Dict[str, Any], available_parameters: List[Dict[str, Any]]) -> Dict[str, Any]:
            """
            Build parameter values based on query components and available parameters.
            
            Args:
                query_components: Components extracted from user query
                available_parameters: Available parameters for the view
                
            Returns:
                Dict[str, Any]: Parameter values to apply
            """
            if not query_components or not available_parameters:
                return {}
                
            parameter_values = {}
            
            # Attempt to map entities in the query to parameters
            for param in available_parameters:
                param_name = param.get('name', '').lower()
                
                # Check if any entity matches the parameter name
                for entity in query_components.get('entities', []):
                    if entity.lower() in param_name or param_name in entity.lower():
                        # Found a potential match, now look for a value
                        # This is a simplified approach and might need to be enhanced
                        # with more sophisticated NLP to extract parameter values
                        
                        # For now, just check if there's a numeric value in the query
                        # that might correspond to this parameter
                        numeric_pattern = r'\b\d+(?:\.\d+)?\b'
                        matches = re.findall(numeric_pattern, query_components.get('original_query', ''))
                        
                        if matches:
                            # Use the first numeric value found
                            parameter_values[param_name] = matches[0]
                            break
                
                # Also check for date/time parameters
                if 'date' in param_name or 'time' in param_name:
                    for timeframe in query_components.get('timeframes', []):
                        # Map the timeframe to a parameter value
                        # This is a simplified approach that would need to be enhanced
                        parameter_values[param_name] = timeframe
                        break
            
            return parameter_values
    @timing_metric
    def _fetch_view_data(self, view_id: str, filter_params: Dict[str, Any] = None, parameter_values: Dict[str, Any] = None, fields: List[str] = None, max_rows: int = 10000, page_size: int = 1000) -> Tuple[Optional[List[Dict[str, Any]]], str]:
        """
        Fetch the actual data from a view with filters and parameters applied.
        Supports pagination for large datasets.
        
        Args:
            view_id: The ID of the Tableau view
            filter_params: Filter parameters to apply
            parameter_values: Parameter values to apply
            fields: Fields to include in the result
            max_rows: Maximum number of rows to fetch in total
            page_size: Number of rows to fetch per page
            
        Returns:
            Tuple[Optional[List[Dict[str, Any]]], str]: Tuple of data table and summary
        """
        try:
            # Build URL for data
            api_url = f"{self.base_url}/api/{self.api_version}/sites/{self.site_id}/views/{view_id}/data"
            
            headers = {
                "X-Tableau-Auth": self.auth_token,
                "Accept": "application/json"
            }
            
            # Add query parameters for filters
            params = {}
            if filter_params:
                for name, value in filter_params.items():
                    if isinstance(value, dict) and 'min' in value and 'max' in value:
                        params[f"vf_{name}_min"] = value['min']
                        params[f"vf_{name}_max"] = value['max']
                    else:
                        params[f"vf_{name}"] = value
            
            # Add parameters if provided
            if parameter_values:
                for name, value in parameter_values.items():
                    params[f"vp_{name}"] = value
            
            # Initialize for pagination
            all_rows = []
            current_page = 1
            more_data = True
            total_rows_processed = 0
            
            logging.info(f"Fetching view data from: {api_url} with filters: {params} and parameters: {parameter_values}")
            
            while more_data and total_rows_processed < max_rows:
                # Add pagination parameters
                pagination_params = dict(params)
                pagination_params['maxRows'] = str(page_size)
                pagination_params['page'] = str(current_page)
                
                self._log_metric("queries_made", 1)
                start_time = time.time()
                response = requests.get(api_url, headers=headers, params=pagination_params, timeout=30)
                request_time = time.time() - start_time
                
                self._log_metric("total_request_time_seconds", request_time)
                
                if response.status_code != 200:
                    logging.error(f"Failed to fetch view data: Status {response.status_code}")
                    error = DataAccessError(
                        f"Failed to fetch data: HTTP {response.status_code}", 
                        {"view_id": view_id, "status_code": response.status_code}
                    )
                    self._log_metric("error_count", 1)
                    return None, f"Failed to fetch data: HTTP {response.status_code}"
                
                self._log_metric("total_response_size_bytes", len(response.content))
                
                # Parse the data
                try:
                    data = response.json()
                    
                    # Handle different response formats based on Tableau version
                    page_rows = []
                    columns = []
                    
                    # Try to extract data and columns from various possible structures
                    if 'dataTable' in data:
                        # Format 1: Modern Tableau REST API
                        data_table = data['dataTable']
                        columns = [col.get('fieldName') for col in data_table.get('headers', {}).get('columns', [])]
                        page_rows = data_table.get('rows', [])
                    elif 'table' in data:
                        # Format 2: Alternative structure
                        columns = data.get('table', {}).get('headers', [])
                        page_rows = data.get('table', {}).get('rows', [])
                    elif 'data' in data and isinstance(data['data'], list):
                        # Format 3: Direct data array
                        page_rows = data['data']
                        # Try to extract columns from first row
                        if page_rows and isinstance(page_rows[0], dict):
                            columns = list(page_rows[0].keys())
                    
                    # Process rows for this page
                    result_data = []
                    
                    if page_rows and columns:
                        for row in page_rows:
                            if isinstance(row, dict):
                                # Row is already a dict
                                result_data.append(row)
                            elif isinstance(row, list):
                                # Convert list to dict using column names
                                result_data.append({columns[i]: val for i, val in enumerate(row) if i < len(columns)})
                    
                    # Add these rows to our collection
                    all_rows.extend(result_data)
                    rows_in_this_page = len(result_data)
                    total_rows_processed += rows_in_this_page
                    
                    # Determine if there might be more data
                    # This depends on how the Tableau API handles pagination
                    more_data = rows_in_this_page >= page_size
                    
                    # If no rows or fewer rows than page size, we've reached the end
                    if rows_in_this_page < page_size:
                        more_data = False
                    
                    # Move to next page if there's more data
                    if more_data:
                        current_page += 1
                        
                except json.JSONDecodeError as e:
                    logging.error(f"Error parsing data JSON: {e}")
                    self._log_metric("error_count", 1)
                    
                    # Check if it's CSV data
                    content_type = response.headers.get('Content-Type', '')
                    if 'text/csv' in content_type:
                        try:
                            # Read CSV data
                            import io
                            import csv
                            
                            csv_data = io.StringIO(response.text)
                            reader = csv.DictReader(csv_data)
                            csv_rows = list(reader)
                            
                            # Add to our collection
                            all_rows.extend(csv_rows)
                            more_data = False  # Can't paginate CSV response
                            
                        except Exception as csv_e:
                            logging.error(f"Error parsing CSV data: {csv_e}")
                            self._log_metric("error_count", 1)
                            return None, "Error parsing data response"
                    else:
                        # If not CSV and JSON parsing failed, we can't continue
                        return None, "Error parsing data response"
            
            # Filter to only requested fields if specified
            if fields and all_rows:
                field_set = set(fields)
                all_rows = [
                    {k: v for k, v in row.items() if k in field_set}
                    for row in all_rows
                ]
            
            # Generate summary
            data_summary = f"Retrieved {len(all_rows)} rows"
            if total_rows_processed >= max_rows:
                data_summary += f" (limited to {max_rows} maximum rows)"
                
            if filter_params:
                filters_desc = ", ".join(f"{k}={v}" for k, v in filter_params.items())
                data_summary += f" (filtered by: {filters_desc})"
                
            if parameter_values:
                params_desc = ", ".join(f"{k}={v}" for k, v in parameter_values.items())
                data_summary += f" (parameters: {params_desc})"
            
            return all_rows, data_summary
                
        except Exception as e:
            error_msg = f"Error fetching view data: {str(e)}"
            logging.error(error_msg)
            self._log_metric("error_count", 1)
            
            error = DataAccessError(
                error_msg,
                {"view_id": view_id}
            )
            
            return None, error_msg
    
            