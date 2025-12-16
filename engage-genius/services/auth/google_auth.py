from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import os
import json
import logging
import sys
import socket

# Configure logging
logger = logging.getLogger(__name__)

# If modifying these scopes, delete the file token.json.
SCOPES = [
    'https://www.googleapis.com/auth/documents.readonly',
    'https://www.googleapis.com/auth/spreadsheets.readonly',
]

def is_running_in_container():
    """Check if we're running in a container environment without a browser"""
    # Method 1: Check for common container environment variables
    if os.environ.get('KUBERNETES_SERVICE_HOST') or os.environ.get('CONTAINER_NAME'):
        return True
    
    # Method 2: Check if containerized by looking for cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            return any(['docker' in line or 'kubepods' in line for line in f.readlines()])
    except (FileNotFoundError, PermissionError):
        pass
    
    # Method 3: Check if we can connect to X server
    try:
        import webbrowser
        available_browsers = webbrowser._tryorder
        return not available_browsers or len(available_browsers) == 0
    except (ImportError, AttributeError):
        # If we can't access browser info, assume we're in a container
        return True
    
    # Default to False if we can't determine
    return False

def setup_google_auth():
    """Sets up Google OAuth authentication and saves the credentials."""
    import os
    
    logger.info("Setting up Google authentication...")
    
    # First check environment variables (highest priority)
    credentials_path = None
    
    # Check GOOGLE_APPLICATION_CREDENTIALS (standard GCP env var)
    if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        env_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
        logger.info(f"Checking credentials from GOOGLE_APPLICATION_CREDENTIALS: {env_path}")
        if os.path.exists(env_path):
            credentials_path = env_path
            logger.info(f"Found credentials at: {env_path}")
    
    # Check GOOGLE_CREDENTIALS_PATH (our custom env var)
    if not credentials_path and os.environ.get('GOOGLE_CREDENTIALS_PATH'):
        env_path = os.environ.get('GOOGLE_CREDENTIALS_PATH')
        logger.info(f"Checking credentials from GOOGLE_CREDENTIALS_PATH: {env_path}")
        if os.path.exists(env_path):
            credentials_path = env_path
            logger.info(f"Found credentials at: {env_path}")
    
    # If not found in env vars, try common deployment paths
    if not credentials_path:
        # Common deployment locations
        credentials_paths = [
            '/app/credentials/credentials.json',  # K8s mounted path
            'credentials.json',  # Direct path
            os.path.join('..', '..', 'credentials.json'),  # From auth directory
            os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'credentials.json')),  # Absolute path
            '/etc/engage-genius/credentials.json',  # System config location
            os.path.join(os.path.expanduser('~'), '.config', 'engage-genius', 'credentials.json'),  # User config
        ]
        
        # Find first valid path
        for path in credentials_paths:
            if os.path.exists(path):
                credentials_path = path
                logger.info(f"Found credentials at: {path}")
                break
    
    if not credentials_path:
        raise FileNotFoundError("credentials.json not found in any of the expected locations")
    
    # Determine token path
    token_path = None
    
    # Check if we have an environment variable for token path
    if os.environ.get('GOOGLE_TOKEN_PATH'):
        token_env_path = os.environ.get('GOOGLE_TOKEN_PATH')
        token_dir = os.path.dirname(token_env_path)
        # Ensure directory exists
        if not os.path.exists(token_dir):
            os.makedirs(token_dir, exist_ok=True)
        token_path = token_env_path
        logger.info(f"Using token path from environment: {token_path}")
    else:
        # Token path should be relative to the credentials
        token_path = os.path.join(os.path.dirname(credentials_path), 'token.json')
        logger.info(f"Using token path relative to credentials: {token_path}")
    
    creds = None
    # The file token.json stores the user's access and refresh tokens
    if os.path.exists(token_path):
        try:
            with open(token_path, 'r') as token:
                creds = Credentials.from_authorized_user_info(json.load(token), SCOPES)
                logger.info(f"Loaded existing credentials from: {token_path}")
            
            # Check if token is expired or will expire soon
            if creds and creds.expired and creds.refresh_token:
                try:
                    logger.info("Refreshing expired credentials")
                    creds.refresh(Request())
                    # Save refreshed credentials
                    token_data = json.loads(creds.to_json())
                    with open(token_path, 'w') as token:
                        json.dump(token_data, token)
                    logger.info("Credentials refreshed successfully")
                except Exception as e:
                    logger.warning(f"Error refreshing token: {e}")
                    creds = None
        except Exception as e:
            logger.warning(f"Error loading token file: {e}")
            creds = None
    
    # If there are no valid credentials available, we need to authenticate
    if not creds or not creds.valid:
        try:
            logger.info(f"Creating new credentials flow using: {credentials_path}")
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, SCOPES)
            
            # Check if we're in a headless environment (like a deployment container)
            in_container = is_running_in_container()
            logger.info(f"Running in container/headless environment: {in_container}")
            
            if in_container:
                # In deployment - use the pre-authenticated token if available
                if os.path.exists(token_path):
                    logger.info("Using pre-authenticated token in deployment environment")
                    try:
                        with open(token_path, 'r') as token:
                            token_data = json.load(token)
                            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
                            
                            # If token is expired but we have a refresh token, try to refresh
                            if creds.expired and creds.refresh_token:
                                creds.refresh(Request())
                                # Save refreshed credentials
                                token_data = json.loads(creds.to_json())
                                with open(token_path, 'w') as token:
                                    json.dump(token_data, token)
                                logger.info("Deployment credentials refreshed successfully")
                    except Exception as e:
                        logger.warning(f"Error loading/refreshing deployment token: {e}")
                        # Continue with flow.run_console as a fallback
                        
                # If we still don't have valid credentials, try console auth (for CI/CD environments)
                if not creds or not creds.valid:
                    logger.info("Using console-based authentication flow for headless environment")
                    # Print instructions to the logs
                    logger.info("Please authenticate by visiting the URL displayed and entering the code")
                    try:
                        # Try console-based flow first
                        creds = flow.run_local_server(port=0, open_browser=False)
                    except Exception as console_error:
                        logger.warning(f"Console auth failed: {console_error}")
                        # Fallback to manual authorization flow
                        auth_url, _ = flow.authorization_url(prompt='consent')
                        logger.info(f"Please visit this URL to authorize the application: {auth_url}")
                        logger.info("After authorization, you'll get a code. Please set it as GOOGLE_AUTH_CODE environment variable and restart.")
                        
                        # Check if auth code is provided via environment variable
                        auth_code = os.environ.get('GOOGLE_AUTH_CODE')
                        if auth_code:
                            logger.info("Using provided authorization code")
                            flow.fetch_token(code=auth_code)
                            creds = flow.credentials
                        else:
                            logger.error("No authorization code provided. Please set GOOGLE_AUTH_CODE environment variable.")
                            raise Exception("Manual authorization required in headless environment")
            else:
                # In local development - open a browser
                logger.info("Using browser-based authentication flow")
                creds = flow.run_local_server(port=0)
            
            # Save the credentials for the next run
            if creds and creds.valid:
                token_data = json.loads(creds.to_json())
                with open(token_path, 'w') as token:
                    json.dump(token_data, token)
                
                logger.info("Successfully authenticated with Google!")
                return True
            else:
                logger.error("Failed to obtain valid credentials")
                raise Exception("Authentication failed to produce valid credentials")
        except Exception as e:
            logger.error(f"Error during authentication flow: {e}")
            # In deployment, fall back to using existing token even if expired
            if is_running_in_container() and os.path.exists(token_path):
                try:
                    logger.warning("Attempting to use existing token despite authentication failure")
                    with open(token_path, 'r') as token:
                        token_data = json.load(token)
                        # Create credentials without validation
                        creds = Credentials(
                            token=token_data.get('token'),
                            refresh_token=token_data.get('refresh_token'),
                            token_uri=token_data.get('token_uri'),
                            client_id=token_data.get('client_id'),
                            client_secret=token_data.get('client_secret'),
                            scopes=token_data.get('scopes')
                        )
                        logger.info("Using existing token as fallback in deployment")
                        return True
                except Exception as fallback_error:
                    logger.error(f"Fallback to existing token failed: {fallback_error}")
            raise
    
    logger.info("Using existing Google credentials")
    return False

if __name__ == "__main__":
    setup_google_auth() 