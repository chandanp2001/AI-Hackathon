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
        devrev_api_key: DevRev Personal Access Token for API access
        
    Raises:
        ConfigError: If required environment variables are missing
    """
    
    # Azure OpenAI Configuration (Chat/Completion)
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
    
    # Azure OpenAI Embedding Configuration
    azure_openai_embedding_endpoint: str = Field(
        default="https://fy26-hackon-q3.openai.azure.com/",
        description="Azure OpenAI embedding endpoint URL"
    )
    azure_openai_embedding_deployment: str = Field(
        default="fy26-hackon-q3-emb",
        description="Azure OpenAI embedding deployment name"
    )
    azure_openai_embedding_model: str = Field(
        default="text-embedding-3-large",
        description="Embedding model name"
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
    
    # Google API Scopes (Read + Write for action-capable agents)
    google_scopes: list[str] = Field(
        default=[
            # Calendar - full access for creating/updating events
            "https://www.googleapis.com/auth/calendar",
            # Gmail - send and compose emails
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.readonly",
            # Drive - file creation and sharing
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
        description="Google API scopes to request (read + write)"
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
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Minimum relevance score to trigger agent (lowered for better recall)"
    )
    
    # Database - SQLite for tokens
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/tokens.db",
        description="Database URL for token storage"
    )
    
    # MySQL for sessions
    mysql_host: str = Field(
        default="localhost",
        description="MySQL host"
    )
    mysql_port: int = Field(
        default=3306,
        description="MySQL port"
    )
    mysql_user: str = Field(
        default="root",
        description="MySQL username"
    )
    mysql_password: str = Field(
        default="Gymboi@10082001",
        description="MySQL password"
    )
    mysql_database: str = Field(
        default="multi_agent_connector",
        description="MySQL database name"
    )
    
    # Slack Configuration (tokens loaded from .env)
    slack_bot_token: str = Field(
        ...,
        description="Slack Bot User OAuth Token for channel operations (from .env)"
    )
    slack_user_token: str = Field(
        ...,
        description="Slack User OAuth Token for search operations (from .env)"
    )
    slack_max_results: int = Field(
        default=50,
        ge=10,
        le=200,
        description="Maximum Slack search results to return"
    )
    slack_max_channels: int = Field(
        default=20,
        ge=5,
        le=50,
        description="Maximum number of Slack channels to search"
    )
    slack_messages_per_channel: int = Field(
        default=15,
        ge=5,
        le=50,
        description="Maximum messages to return per Slack channel"
    )
    
    # DevRev Configuration
    devrev_api_key: Optional[str] = Field(
        default=None,
        alias="dev_rev",
        description="DevRev Personal Access Token (PAT) for API access (from .env DEV_REV)"
    )
    devrev_api_url: str = Field(
        default="https://api.devrev.ai",
        description="DevRev API base URL"
    )
    devrev_max_results: int = Field(
        default=50,
        ge=10,
        le=200,
        description="Maximum DevRev work items to return"
    )
    
    # Server
    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8000, description="Server port")
    debug: bool = Field(default=False, description="Debug mode")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        populate_by_name = True


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
