# Multi-Agent Data Connector

An agent-based LLM system where each external data source (Google Calendar, Gmail, Google Drive) is represented by a dedicated AI agent. Each agent evaluates query relevance and retrieves data only when appropriate.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI Backend                          │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────────┐     │
│  │  REST API   │───▶│ Orchestrator │───▶│  Auth Service  │     │
│  └─────────────┘    └──────────────┘    └────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Data Connector Agents                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Calendar   │  │    Gmail     │  │    Drive     │          │
│  │    Agent     │  │    Agent     │  │    Agent     │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
└─────────────────────────────────────────────────────────────────┘
         │                   │                   │
         ▼                   ▼                   ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│ Google Calendar │ │   Gmail API     │ │  Drive API      │
│      API        │ │                 │ │                 │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

## Features

- **Intelligent Query Routing**: Each agent evaluates query relevance (0.0-1.0) and only fetches data when appropriate
- **Parallel Processing**: All agents evaluate queries simultaneously using async/await
- **Response Synthesis**: Azure OpenAI GPT-5 synthesizes coherent responses from multiple data sources
- **OAuth2 Authentication**: Secure multi-user Google authentication with token encryption
- **Extensible Architecture**: Easy to add new data connector agents

## Query Processing Flow

1. User submits a natural language query
2. Query is broadcast to all registered agents
3. Each agent evaluates relevance using GPT-5
4. Agents scoring above threshold (default 0.5) are triggered
5. Triggered agents fetch data from their respective APIs
6. Orchestrator synthesizes a final response using GPT-5

## Setup

### Prerequisites

- Python 3.11+
- Google Cloud Project with OAuth2 configured
- Azure OpenAI API access (GPT-5)

### Google Cloud Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable the following APIs:
   - Google Calendar API
   - Gmail API
   - Google Drive API
4. Configure OAuth consent screen
5. Create OAuth2 credentials (Web application)
6. Add `http://localhost:8000/api/auth/callback` to authorized redirect URIs

### Installation

```bash
# Clone the repository
cd AI-Hackathon

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create .env file with your credentials (see below)
```

### Environment Variables

Create a `.env` file with the following (this file is gitignored and will NOT be pushed):

```env
# Azure OpenAI Configuration
AZURE_OPENAI_ENDPOINT=https://your-endpoint.cognitiveservices.azure.com/
AZURE_OPENAI_API_KEY=your-api-key
AZURE_OPENAI_DEPLOYMENT=your-deployment-name
AZURE_OPENAI_API_VERSION=2025-01-01-preview

# Google OAuth Configuration
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_REDIRECT_URI=http://localhost:8000/api/auth/callback

# Security (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
ENCRYPTION_KEY=your-fernet-key

# Agent Configuration
RELEVANCE_THRESHOLD=0.5

# Server
DEBUG=true
```

**⚠️ IMPORTANT: Never commit the `.env` file or any credentials to version control!**

### Running the Application

```bash
# Start the server
python app.py

# Or with uvicorn directly
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

## API Endpoints

### Query

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/query` | Submit natural language query |

**Request:**
```json
{
  "query": "What meetings do I have tomorrow?",
  "user_id": "user_123",
  "threshold_override": 0.5
}
```

**Response:**
```json
{
  "query": "What meetings do I have tomorrow?",
  "response": "You have 3 meetings scheduled for tomorrow...",
  "agents_triggered": [
    {
      "agent_name": "calendar",
      "relevance_score": 0.95,
      "justification": "Query explicitly asks about meetings",
      "data_count": 3,
      "execution_time_ms": 523.4
    }
  ],
  "total_execution_time_ms": 1234.5,
  "timestamp": "2024-01-15T10:30:00Z"
}
```

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/auth/google?user_id=xxx` | Get OAuth authorization URL |
| GET | `/api/auth/callback` | OAuth callback handler |
| GET | `/api/auth/status?user_id=xxx` | Check connection status |
| DELETE | `/api/auth/disconnect?user_id=xxx` | Revoke access |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/metrics` | System metrics |
| GET | `/docs` | OpenAPI documentation |

## Example Queries

```python
import httpx

# First, authenticate the user
auth_response = httpx.get("http://localhost:8000/api/auth/google?user_id=user_123")
print(f"Visit: {auth_response.json()['authorization_url']}")

# After OAuth callback, query the system
query_response = httpx.post(
    "http://localhost:8000/api/query",
    json={
        "query": "What meetings do I have this week and are there any related emails?",
        "user_id": "user_123"
    }
)
print(query_response.json()["response"])
```

## Adding New Agents

1. Create a new agent class extending `BaseDataAgent`:

```python
from services.agents.base_agent import BaseDataAgent
from models.agent_response import AgentType, AgentResult

class MyNewAgent(BaseDataAgent):
    @property
    def agent_name(self) -> str:
        return "my_agent"
    
    @property
    def agent_type(self) -> AgentType:
        return AgentType.CUSTOM  # Add to enum
    
    @property
    def data_source_description(self) -> str:
        return "Description of what this agent handles..."
    
    async def fetch_data(self, query, credentials, search_terms=None):
        # Implement data retrieval
        pass
```

2. Register the agent in `services/orchestrator.py`

## Project Structure

```
├── app.py                      # FastAPI entry point
├── config.py                   # Configuration management
├── requirements.txt            # Python dependencies
├── models/
│   ├── query.py                # Request/response models
│   └── agent_response.py       # Agent output schemas
├── services/
│   ├── orchestrator.py         # Central query orchestrator
│   ├── agents/
│   │   ├── base_agent.py       # Abstract agent interface
│   │   ├── calendar_agent.py   # Google Calendar connector
│   │   ├── gmail_agent.py      # Gmail connector
│   │   └── drive_agent.py      # Google Drive connector
│   ├── llm/
│   │   └── openai_service.py   # Azure OpenAI GPT-5 wrapper
│   └── auth/
│       └── google_auth.py      # OAuth2 handling
└── storage/
    └── token_store.py          # Secure token persistence
```

## Security Notes

- **Never commit credentials** to version control
- All secrets should be in `.env` file (gitignored)
- Tokens are encrypted at rest using Fernet encryption
- OAuth tokens are automatically refreshed when expired

## License

MIT
