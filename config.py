"""
Configuration management for the Multi-Agent Data Connector system.

Loads environment variables and provides validated configuration settings.
"""

from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables.
    
    Args:
        azure_openai_endpoint: Azure OpenAI endpoint URL
        azure_openai_api_key: Azure OpenAI API key
        azure_openai_deployment: Deployment name for the model
        azure_openai_api_version: API version to use
        google_client_id: OAuth2 client ID from Google Cloud Console
        google_client_secret: OAuth2 client secret from Google Cloud Console
        google_redirect_uri: OAuth2 callback URL
        encryption_key: Fernet key for encrypting stored tokens
        relevance_threshold: Minimum score for agent activation (0.0-1.0)
        database_url: SQLite database path for token storage
        
    Raises:
        ConfigError: If required environment variables are missing
    """
    
    # Azure OpenAI Configuration
    azure_openai_endpoint: str = Field(
        ...,
        description="Azure OpenAI endpoint URL"
    )
    azure_openai_api_key: str = Field(
        ...,
        description="Azure OpenAI API key"
    )
    azure_openai_deployment: str = Field(
        ...,
        description="Azure OpenAI deployment name"
    )
    azure_openai_api_version: str = Field(
        default="2025-01-01-preview",
        description="Azure OpenAI API version"
    )
    azure_openai_model: str = Field(
        default="gpt-5-chat",
        description="Model name for reference"
    )
    
    # Google OAuth Configuration
    google_client_id: str = Field(
        ...,
        description="Google OAuth2 client ID"
    )
    google_client_secret: str = Field(
        ...,
        description="Google OAuth2 client secret"
    )
    google_redirect_uri: str = Field(
        default="http://localhost:8000/api/auth/callback",
        description="OAuth2 callback URL"
    )
    
    # Google API Scopes
    google_scopes: list[str] = Field(
        default=[
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
        description="Google API scopes to request"
    )
    
    # Security
    encryption_key: Optional[str] = Field(
        default=None,
        description="Fernet encryption key for token storage"
    )
    secret_key: str = Field(
        default="change-me-in-production",
        description="Secret key for session management"
    )
    
    # Agent Configuration
    relevance_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Minimum relevance score to trigger agent"
    )
    
    # Database
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/tokens.db",
        description="Database URL for token storage"
    )
    
    # Server
    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8000, description="Server port")
    debug: bool = Field(default=False, description="Debug mode")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


def get_settings() -> Settings:
    """Get application settings singleton.
    
    Returns:
        Settings: Validated application settings
        
    Raises:
        ConfigError: If required environment variables are missing
    """
    return Settings()


# Export settings instance for easy import
settings = get_settings()
