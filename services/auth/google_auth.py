"""Google OAuth2 authentication service.

Handles OAuth2 flow for Google APIs supporting both web and desktop credentials.
"""

import json
import logging
import os
from typing import Optional, Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
import webbrowser

from config import settings

logger = logging.getLogger(__name__)


class GoogleAuthError(Exception):
    """Custom exception for Google authentication errors."""
    
    def __init__(self, message: str, error_code: Optional[str] = None):
        super().__init__(message)
        self.error_code = error_code


# Scopes for Google APIs - loaded from config for action support
SCOPES = settings.google_scopes


class GoogleAuthService:
    """Google OAuth2 authentication service.
    
    Supports both web and desktop OAuth credentials.
    Stores tokens per user in the data directory.
    """
    
    def __init__(
        self,
        credentials_path: Optional[str] = None,
        token_dir: Optional[str] = None
    ):
        self._credentials_path = credentials_path
        self._token_dir = token_dir or "./data/tokens"
        self._initialized = False
        self._client_config = None
        
    async def initialize(self) -> None:
        """Initialize the auth service."""
        # Find credentials.json file
        self._credentials_path = self._find_credentials_file()
        
        # Load client config
        with open(self._credentials_path, 'r') as f:
            self._client_config = json.load(f)
            
        # Ensure token directory exists
        os.makedirs(self._token_dir, exist_ok=True)
        
        self._initialized = True
        logger.info(f"Google Auth Service initialized with credentials: {self._credentials_path}")
        
    def _find_credentials_file(self) -> str:
        """Find the credentials.json file in various locations."""
        # Check environment variables first
        for env_var in ['GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_CREDENTIALS_PATH']:
            path = os.environ.get(env_var)
            if path and os.path.exists(path):
                return path
        
        # Common locations to check
        search_paths = [
            'credentials.json',
            './credentials.json',
            './data/credentials.json',
            os.path.join(os.path.dirname(__file__), '..', '..', 'credentials.json'),
        ]
        
        # Also search for client_secret_*.json files in Downloads and project
        for directory in ['.', os.path.join(os.path.expanduser('~'), 'Downloads')]:
            if os.path.exists(directory):
                for f in os.listdir(directory):
                    if f.startswith('client_secret_') and f.endswith('.json'):
                        search_paths.append(os.path.join(directory, f))
        
        for path in search_paths:
            if os.path.exists(path):
                logger.info(f"Found credentials at: {path}")
                return os.path.abspath(path)
                
        raise FileNotFoundError(
            "credentials.json not found. Please download it from Google Cloud Console."
        )
        
    def _get_token_path(self, user_id: str) -> str:
        """Get the token file path for a user."""
        return os.path.join(self._token_dir, f"token_{user_id}.json")
        
    def _get_redirect_uri(self) -> str:
        """Get the appropriate redirect URI."""
        # Check if web credentials have redirect URIs configured
        if 'web' in self._client_config:
            redirect_uris = self._client_config['web'].get('redirect_uris', [])
            if redirect_uris:
                # Prefer localhost callback
                for uri in redirect_uris:
                    if 'localhost' in uri:
                        return uri
                return redirect_uris[0]
        return settings.google_redirect_uri
        
    def get_authorization_url(
        self,
        user_id: str,
        state: Optional[str] = None
    ) -> tuple[str, str]:
        """Generate Google OAuth2 authorization URL."""
        if not self._initialized:
            raise GoogleAuthError("Auth service not initialized")
        
        # Determine credential type
        if 'web' in self._client_config:
            client_config = self._client_config
        elif 'installed' in self._client_config:
            client_config = self._client_config
        else:
            raise GoogleAuthError("Invalid credentials format")
            
        redirect_uri = self._get_redirect_uri()
        
        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
        
        # Generate state if not provided
        if state is None:
            import secrets
            state = f"{user_id}:{secrets.token_urlsafe(32)}"
            
        auth_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent',
            state=state
        )
        
        logger.info(f"Generated auth URL for user {user_id}")
        return auth_url, state
        
    async def authenticate_interactive(self, user_id: str) -> Credentials:
        """Authenticate interactively - opens browser and waits for callback.
        
        This method prints the auth URL and waits for manual code entry.
        """
        if not self._initialized:
            raise GoogleAuthError("Auth service not initialized")
            
        auth_url, state = self.get_authorization_url(user_id)
        
        print("\n" + "="*60)
        print("Google Authentication Required")
        print("="*60)
        print(f"\n1. Open this URL in your browser:\n\n{auth_url}\n")
        print("2. Sign in and grant permissions")
        print("3. You'll be redirected to a URL like:")
        print("   http://localhost:8000/api/auth/callback?code=XXXX&state=XXXX")
        print("\n4. Copy the 'code' parameter value and paste it below:")
        print("="*60)
        
        # Try to open browser automatically
        try:
            webbrowser.open(auth_url)
            print("\n(Browser should open automatically)")
        except Exception:
            pass
            
        code = input("\nEnter the authorization code: ").strip()
        
        if not code:
            raise GoogleAuthError("No authorization code provided")
            
        return await self.exchange_code(code, user_id)
        
    async def exchange_code(self, code: str, user_id: str) -> Credentials:
        """Exchange authorization code for tokens."""
        import os
        
        if not self._initialized:
            raise GoogleAuthError("Auth service not initialized")
            
        try:
            redirect_uri = self._get_redirect_uri()
            
            # Set environment variable to disable scope change warning
            # Google may return additional scopes (e.g., calendar.readonly with calendar)
            os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
            
            flow = Flow.from_client_config(
                self._client_config,
                scopes=SCOPES,
                redirect_uri=redirect_uri
            )
            
            flow.fetch_token(code=code)
            creds = flow.credentials
            
            # Log the actual scopes received
            logger.info(f"Received scopes: {creds.scopes}")
            
            # Save credentials
            await self._store_credentials(user_id, creds)
            
            logger.info(f"Successfully authenticated user {user_id}")
            return creds
            
        except Exception as e:
            logger.error(f"Code exchange failed for user {user_id}: {e}")
            raise GoogleAuthError(f"Failed to exchange authorization code: {str(e)}")
            
    async def get_credentials(self, user_id: str) -> Optional[Credentials]:
        """Get stored credentials for a user."""
        if not self._initialized:
            raise GoogleAuthError("Auth service not initialized")
            
        token_path = self._get_token_path(user_id)
        
        if not os.path.exists(token_path):
            logger.debug(f"No stored credentials for user {user_id}")
            return None
            
        try:
            with open(token_path, 'r') as f:
                token_data = json.load(f)
                
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            
            # Refresh if expired
            if creds and creds.expired and creds.refresh_token:
                logger.info(f"Refreshing expired credentials for user {user_id}")
                creds.refresh(Request())
                await self._store_credentials(user_id, creds)
                logger.info(f"Credentials refreshed for user {user_id}")
                
            return creds
            
        except Exception as e:
            logger.error(f"Failed to load credentials for user {user_id}: {e}")
            return None
            
    async def _store_credentials(self, user_id: str, creds: Credentials) -> None:
        """Store credentials to file."""
        token_path = self._get_token_path(user_id)
        token_data = json.loads(creds.to_json())
        
        with open(token_path, 'w') as f:
            json.dump(token_data, f)
            
        logger.debug(f"Stored credentials for user {user_id}")
        
    async def revoke_access(self, user_id: str) -> bool:
        """Revoke user's Google access and delete stored tokens."""
        token_path = self._get_token_path(user_id)
        
        if os.path.exists(token_path):
            os.remove(token_path)
            logger.info(f"Deleted tokens for user {user_id}")
            return True
            
        return False
        
    async def check_auth_status(self, user_id: str) -> dict[str, Any]:
        """Check authentication status for a user."""
        if not self._initialized:
            return {"connected": False, "error": "Service not initialized"}
            
        try:
            creds = await self.get_credentials(user_id)
            
            if creds and creds.valid:
                return {
                    "connected": True,
                    "scopes": list(creds.scopes) if creds.scopes else SCOPES,
                    "expires_at": creds.expiry.isoformat() if creds.expiry else None
                }
            else:
                return {"connected": False}
                
        except Exception as e:
            return {"connected": False, "error": str(e)}
            
    async def health_check(self) -> bool:
        """Check if auth service is healthy."""
        return self._initialized and self._credentials_path is not None
        
    async def shutdown(self) -> None:
        """Shutdown auth service."""
        self._initialized = False
        logger.info("Google Auth Service shutdown")
        
    def get_metrics(self) -> dict[str, Any]:
        """Get auth service metrics."""
        return {
            "initialized": self._initialized,
            "credentials_path": self._credentials_path,
            "scopes": SCOPES
        }


# Global auth service instance
_auth_service: Optional[GoogleAuthService] = None


async def get_auth_service() -> GoogleAuthService:
    """Get or create the global auth service instance."""
    global _auth_service
    
    if _auth_service is None:
        _auth_service = GoogleAuthService()
        await _auth_service.initialize()
        
    return _auth_service


def setup_google_auth(user_id: str = "default") -> bool:
    """Setup Google auth interactively (for CLI use)."""
    import asyncio
    
    async def _setup():
        service = GoogleAuthService()
        await service.initialize()
        
        # Check if already authenticated
        creds = await service.get_credentials(user_id)
        if creds and creds.valid:
            print("Already authenticated with Google!")
            return True
            
        # Authenticate interactively
        await service.authenticate_interactive(user_id)
        print("\nSuccessfully authenticated with Google!")
        return True
        
    return asyncio.run(_setup())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    setup_google_auth()
