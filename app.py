"""FastAPI application for the Multi-Agent Data Connector system.

This module provides the REST API endpoints for:
- Query processing via the orchestrator
- Google OAuth2 authentication flow
- Health checks and metrics
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from config import settings
from models.query import QueryRequest, QueryResponse
from services.orchestrator import Orchestrator, get_orchestrator
from services.auth.google_auth import GoogleAuthService, GoogleAuthError, get_auth_service

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Global service instances
orchestrator: Optional[Orchestrator] = None
auth_service: Optional[GoogleAuthService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown."""
    global orchestrator, auth_service
    
    logger.info("Starting Multi-Agent Data Connector...")
    
    # Initialize services
    orchestrator = Orchestrator()
    await orchestrator.initialize()
    
    auth_service = GoogleAuthService()
    await auth_service.initialize()
    
    logger.info("Application started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    if orchestrator:
        await orchestrator.shutdown()
    if auth_service:
        await auth_service.shutdown()
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Multi-Agent Data Connector",
    description="""
    An agent-based LLM system where each external data source (Google Calendar, 
    Gmail, Google Drive) is represented by a dedicated AI agent. Each agent 
    evaluates query relevance and retrieves data only when appropriate.
    """,
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------
# Request/Response Models
# -----------------

class AuthUrlResponse(BaseModel):
    """Response containing OAuth authorization URL."""
    authorization_url: str
    state: str


class AuthStatusResponse(BaseModel):
    """Response containing authentication status."""
    connected: bool
    scopes: list[str] = Field(default_factory=list)
    expires_at: Optional[str] = None
    error: Optional[str] = None


class HealthResponse(BaseModel):
    """Response containing health check status."""
    status: str
    orchestrator: dict
    auth_service: bool
    agents: dict


class MetricsResponse(BaseModel):
    """Response containing system metrics."""
    orchestrator: dict
    auth: dict


# -----------------
# Query Endpoints
# -----------------

@app.post("/api/query", response_model=QueryResponse, tags=["Query"])
async def process_query(request: QueryRequest):
    """Process a natural language query through the agent system.
    
    This endpoint:
    1. Broadcasts the query to all data connector agents
    2. Collects relevance scores from each agent
    3. Triggers data retrieval for agents above the threshold
    4. Synthesizes a coherent response from the collected data
    
    Args:
        request: Query request with user_id and query text
        
    Returns:
        QueryResponse: Synthesized response with agent contributions
        
    Raises:
        HTTPException: 401 if user not authenticated
        HTTPException: 500 if query processing fails
    """
    global orchestrator, auth_service
    
    if not orchestrator or not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Service not initialized"
        )
        
    # Get user credentials
    try:
        credentials = await auth_service.get_credentials(request.user_id)
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=401,
            detail=f"Authentication error: {str(e)}"
        )
        
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="User not authenticated. Please connect your Google account."
        )
        
    # Process query
    try:
        response = await orchestrator.process_query(request, credentials)
        return response
    except Exception as e:
        logger.error(f"Query processing failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Query processing failed: {str(e)}"
        )


# -----------------
# Authentication Endpoints
# -----------------

@app.get("/api/auth/google", response_model=AuthUrlResponse, tags=["Authentication"])
async def get_auth_url(
    user_id: str = Query(..., description="User identifier")
):
    """Get Google OAuth authorization URL.
    
    Generates a URL that the user should be redirected to for
    granting access to their Google Calendar, Gmail, and Drive.
    
    Args:
        user_id: Unique identifier for the user
        
    Returns:
        AuthUrlResponse: Authorization URL and state token
    """
    global auth_service
    
    if not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Auth service not initialized"
        )
        
    try:
        auth_url, state = auth_service.get_authorization_url(user_id)
        return AuthUrlResponse(authorization_url=auth_url, state=state)
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.get("/api/auth/callback", tags=["Authentication"])
async def auth_callback(
    code: str = Query(..., description="Authorization code from Google"),
    state: str = Query(..., description="State parameter for CSRF validation")
):
    """Handle OAuth callback from Google.
    
    Exchanges the authorization code for access tokens and stores
    them securely for future API calls.
    
    Args:
        code: Authorization code from Google
        state: State parameter containing user_id
        
    Returns:
        Redirect or success message
    """
    global auth_service
    
    if not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Auth service not initialized"
        )
        
    # Extract user_id from state (format: user_id:random_token)
    try:
        user_id = state.split(":")[0]
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=400,
            detail="Invalid state parameter"
        )
        
    try:
        await auth_service.exchange_code(code, user_id)
        return {
            "status": "success",
            "message": "Successfully connected Google account",
            "user_id": user_id
        }
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )


@app.get("/api/auth/status", response_model=AuthStatusResponse, tags=["Authentication"])
async def get_auth_status(
    user_id: str = Query(..., description="User identifier")
):
    """Check authentication status for a user.
    
    Returns whether the user has connected their Google account
    and what scopes are available.
    
    Args:
        user_id: User identifier
        
    Returns:
        AuthStatusResponse: Connection status and scopes
    """
    global auth_service
    
    if not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Auth service not initialized"
        )
        
    status = await auth_service.check_auth_status(user_id)
    return AuthStatusResponse(**status)


@app.delete("/api/auth/disconnect", tags=["Authentication"])
async def disconnect_google(
    user_id: str = Query(..., description="User identifier")
):
    """Disconnect user's Google account.
    
    Revokes access tokens and removes stored credentials.
    
    Args:
        user_id: User identifier
        
    Returns:
        Success message
    """
    global auth_service
    
    if not auth_service:
        raise HTTPException(
            status_code=503,
            detail="Auth service not initialized"
        )
        
    try:
        await auth_service.revoke_access(user_id)
        return {
            "status": "success",
            "message": "Google account disconnected"
        }
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# -----------------
# Health & Metrics Endpoints
# -----------------

@app.get("/api/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Check health of all system components.
    
    Returns status of orchestrator, auth service, and all agents.
    
    Returns:
        HealthResponse: Health status of all components
    """
    global orchestrator, auth_service
    
    health = {
        "status": "healthy",
        "orchestrator": {},
        "auth_service": False,
        "agents": {}
    }
    
    if orchestrator:
        orch_health = await orchestrator.health_check()
        health["orchestrator"] = orch_health.get("orchestrator", {})
        health["agents"] = orch_health.get("agents", {})
        
    if auth_service:
        health["auth_service"] = await auth_service.health_check()
        
    # Determine overall status
    if not health["orchestrator"].get("initialized") or not health["auth_service"]:
        health["status"] = "degraded"
        
    return HealthResponse(**health)


@app.get("/api/metrics", response_model=MetricsResponse, tags=["System"])
async def get_metrics():
    """Get system metrics.
    
    Returns metrics from orchestrator and auth service.
    
    Returns:
        MetricsResponse: System metrics
    """
    global orchestrator, auth_service
    
    metrics = {
        "orchestrator": {},
        "auth": {}
    }
    
    if orchestrator:
        metrics["orchestrator"] = orchestrator.get_metrics()
        
    if auth_service:
        metrics["auth"] = auth_service.get_metrics()
        
    return MetricsResponse(**metrics)


@app.get("/", tags=["System"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": "Multi-Agent Data Connector",
        "version": "1.0.0",
        "documentation": "/docs",
        "health": "/api/health"
    }


# -----------------
# Run Application
# -----------------

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug
    )

