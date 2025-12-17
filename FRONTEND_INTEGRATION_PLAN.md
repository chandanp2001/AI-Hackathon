# 🎯 COMPLETE FRONTEND INTEGRATION PLAN FOR MULTI-AGENT DATA CONNECTOR

## 📊 EXECUTIVE SUMMARY

Your backend is a **Multi-Agent LLM-powered personal assistant** that connects to:
- **Google Calendar** (read events + create/update/delete)
- **Gmail** (read emails + send/draft/reply/forward)
- **Google Drive** (read files + create docs/sheets/share)
- **Slack** (search messages across channels)

The system uses **relevance scoring** where the LLM evaluates each query to determine which agents (data sources) are relevant. Only agents scoring above the threshold (default: 0.5) are activated.

---

## 🌐 BACKEND API REFERENCE

### Base URL: `http://localhost:8000`

---

### 1. MAIN QUERY ENDPOINT (Conversational Interface)

```
POST /api/query
```

**Request:**
```json
{
  "query": "What meetings do I have tomorrow?",
  "user_id": "user_123",
  "threshold_override": 0.5
}
```

**Three Possible Response Types:**

#### Response Type A: READ Query Response
```json
{
  "query": "What meetings do I have tomorrow?",
  "response": "You have **2 meetings** scheduled for tomorrow:\n\n- **Team Standup** at 9:00 AM...",
  "agents_triggered": [
    {
      "agent_name": "calendar",
      "relevance_score": 0.95,
      "justification": "Query explicitly asks about meetings",
      "data_count": 2,
      "execution_time_ms": 523.4
    }
  ],
  "raw_data": {
    "calendar": [
      {
        "event_id": "abc123",
        "title": "Team Standup",
        "start_time": "2025-12-17T09:00:00Z",
        "end_time": "2025-12-17T09:30:00Z",
        "attendees": ["alice@co.com"],
        "meeting_link": "https://meet.google.com/xxx"
      }
    ]
  },
  "total_execution_time_ms": 1234.5,
  "timestamp": "2025-12-16T10:30:00Z"
}
```

#### Response Type B: ACTION Plan Response
```json
{
  "type": "action_plan",
  "plan_id": "a1b2c3d4",
  "query": "Schedule a meeting with john@company.com tomorrow at 2pm",
  "summary": "Create a calendar event",
  "preview": "📅 **New Meeting**\n🕐 Tomorrow at 2:00 PM\n👥 Attendees: john@company.com",
  "steps": [
    {
      "step_number": 1,
      "action": "create_event",
      "description": "Create calendar event",
      "agent": "calendar"
    }
  ],
  "risk_level": "medium",
  "requires_confirmation": true,
  "estimated_duration": "< 1 minute",
  "execution_time_ms": 456.7
}
```

#### Response Type C: Clarification Needed
```json
{
  "type": "clarification_needed",
  "message": "What is Sarah's email address? What would you like to say?",
  "missing_parameters": ["to (full email)", "body"],
  "partial_plan": {
    "to": ["Sarah"],
    "subject": "Regarding the deadline"
  },
  "execution_time_ms": 234.5
}
```

---

### 2. ACTION EXECUTION ENDPOINTS

#### Execute Confirmed Action
```
POST /api/action/execute
```

**Request:**
```json
{
  "plan_id": "a1b2c3d4",
  "user_id": "user_123"
}
```

**Response:**
```json
{
  "plan_id": "a1b2c3d4",
  "success": true,
  "summary": "✅ Calendar event created successfully",
  "step_results": [
    {
      "step_number": 1,
      "success": true,
      "result_data": {
        "event_id": "xyz789",
        "html_link": "https://calendar.google.com/event?eid=xxx",
        "meeting_link": "https://meet.google.com/yyy"
      },
      "execution_time_ms": 890.2
    }
  ],
  "links": {
    "html_link": "https://calendar.google.com/event?eid=xxx",
    "meeting_link": "https://meet.google.com/yyy"
  },
  "rollback_available": true
}
```

#### Cancel Action
```
POST /api/action/cancel
```

**Request:**
```json
{
  "plan_id": "a1b2c3d4"
}
```

#### Get Action Plan Details
```
GET /api/action/{plan_id}
```

---

### 3. SLACK SEARCH ENDPOINTS

#### Search Slack Messages
```
POST /api/slack/search
```

**Request:**
```json
{
  "query": "deployment issues",
  "user_id": "user_123",
  "channel": "engineering",
  "time_range": "last_week",
  "limit": 10,
  "max_channels": 10,
  "include_dms": false,
  "deep_search": false
}
```

**Response:**
```json
{
  "query": "deployment issues",
  "results": {
    "engineering": {
      "channel_name": "engineering",
      "channel_id": "C123456",
      "messages": [
        {
          "text": "We had a deployment issue yesterday...",
          "user": "U789",
          "timestamp": "1702700000.000000",
          "relevance_score": 0.85,
          "permalink": "https://slack.com/archives/..."
        }
      ]
    }
  },
  "channel_summaries": [
    {
      "channel_id": "C123456",
      "channel_name": "engineering",
      "message_count": 5,
      "relevance_score": 0.85,
      "top_message_preview": "We had a deployment issue..."
    }
  ],
  "total_message_count": 15,
  "channels_searched": 3,
  "follow_up_suggestions": [
    {
      "type": "channel",
      "value": "devops",
      "label": "Search in #devops",
      "description": "3 related messages found"
    }
  ],
  "summary": "Found 15 messages across 3 channels about deployment issues",
  "is_global_search": true,
  "processing_time_ms": 1234.5
}
```

#### List Slack Channels
```
GET /api/slack/channels?limit=50&include_private=true
```

**Response:**
```json
{
  "channels": [
    {"name": "engineering", "id": "C123456", "is_private": false},
    {"name": "team-devops", "id": "C789012", "is_private": true}
  ],
  "total": 25,
  "cache_age_seconds": 3600
}
```

---

### 4. AUTHENTICATION ENDPOINTS

#### Get Google Auth URL
```
GET /api/auth/google?user_id=user_123
```

**Response:**
```json
{
  "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?...",
  "state": "user_123:random_token"
}
```

#### Check Auth Status
```
GET /api/auth/status?user_id=user_123
```

**Response:**
```json
{
  "connected": true,
  "scopes": ["https://www.googleapis.com/auth/calendar", "..."],
  "expires_at": "2025-12-17T10:00:00Z"
}
```

#### OAuth Callback
```
GET /api/auth/callback?code=xxx&state=user_123:token
```

#### Disconnect Account
```
DELETE /api/auth/disconnect?user_id=user_123
```

---

### 5. SYSTEM ENDPOINTS

#### Health Check
```
GET /api/health
```

**Response:**
```json
{
  "status": "healthy",
  "orchestrator": {"initialized": true, "threshold": 0.5, "agent_count": 3},
  "auth_service": true,
  "agents": {
    "calendar": true,
    "gmail": true,
    "drive": true
  }
}
```

#### Metrics
```
GET /api/metrics
```

---

## 🎨 FRONTEND UI/UX SPECIFICATIONS

### OVERALL DESIGN PHILOSOPHY
Create a **conversational AI chat interface** that feels modern and responsive, similar to ChatGPT/Claude but specialized for personal productivity.

### COLOR SCHEME & THEME
```css
:root {
  /* Dark theme primary */
  --bg-primary: #0f0f0f;
  --bg-secondary: #1a1a1a;
  --bg-tertiary: #262626;
  
  /* Accent colors for agents */
  --calendar-color: #4285F4;  /* Google Blue */
  --gmail-color: #EA4335;     /* Gmail Red */
  --drive-color: #34A853;     /* Drive Green */
  --slack-color: #4A154B;     /* Slack Purple */
  
  /* Status colors */
  --success: #22c55e;
  --warning: #f59e0b;
  --danger: #ef4444;
  
  /* Text */
  --text-primary: #ffffff;
  --text-secondary: #a0a0a0;
}
```

---

## 📱 PAGE & COMPONENT STRUCTURE

### 1. MAIN CHAT PAGE (`/`)

```
┌────────────────────────────────────────────────────────────────┐
│ HEADER                                                         │
│ ┌────────────────────────────────────────────────────────────┐ │
│ │ 🤖 Engage Genius                        [Auth Status] [⚙️] │ │
│ └────────────────────────────────────────────────────────────┘ │
├────────────────────────────────────────────────────────────────┤
│ SIDEBAR (Collapsible)          │  MAIN CHAT AREA              │
│ ┌────────────────────────────┐ │ ┌────────────────────────────┐│
│ │ 📅 Calendar Agent          │ │ │ [Message Bubble]           ││
│ │ 📧 Gmail Agent             │ │ │ User: What meetings...     ││
│ │ 📁 Drive Agent             │ │ │                            ││
│ │ 💬 Slack Search            │ │ │ [Response Bubble]          ││
│ │ ─────────────────────────  │ │ │ AI: You have 2 meetings... ││
│ │ Data Source Scores:        │ │ │ ┌──────────────────────┐   ││
│ │ Calendar: ████████░░ 0.95  │ │ │ │ Agent Contributions  │   ││
│ │ Gmail: ██░░░░░░░░░░ 0.20   │ │ │ │ 📅 Calendar: 0.95    │   ││
│ │ Drive: █░░░░░░░░░░░ 0.10   │ │ │ └──────────────────────┘   ││
│ │ ─────────────────────────  │ │ │                            ││
│ │ Quick Actions:             │ │ │ [Action Confirmation Card] ││
│ │ 🗓️ Schedule meeting        │ │ │ ┌──────────────────────┐   ││
│ │ ✉️ Send email              │ │ │ │ 📅 Create Event      │   ││
│ │ 📄 Create document         │ │ │ │ Title: Team Meeting  │   ││
│ │ 🔍 Search Slack            │ │ │ │ When: Tomorrow 2PM   │   ││
│ └────────────────────────────┘ │ │ │ [Confirm] [Edit] [X] │   ││
│                                │ │ └──────────────────────┘   ││
├────────────────────────────────┼──┴────────────────────────────┤
│ INPUT AREA                                                      │
│ ┌──────────────────────────────────────────────────────────────┐│
│ │ [🎤] Ask me anything... (Ctrl+Enter to send)          [Send]││
│ └──────────────────────────────────────────────────────────────┘│
└────────────────────────────────────────────────────────────────┘
```

### 2. AUTHENTICATION PAGE (`/auth`)

```
┌────────────────────────────────────────────────────────────────┐
│                                                                │
│                    🔐 Connect Your Accounts                    │
│                                                                │
│   ┌────────────────────────────────────────────────────────┐   │
│   │ Google Services                                        │   │
│   │ ┌────────────┐ ┌────────────┐ ┌────────────┐          │   │
│   │ │ 📅 Calendar│ │ 📧 Gmail  │ │ 📁 Drive   │          │   │
│   │ │    ✓      │ │     ✓     │ │    ✓       │          │   │
│   │ └────────────┘ └────────────┘ └────────────┘          │   │
│   │                                                        │   │
│   │ [🔗 Connect Google Account]  Status: ✅ Connected      │   │
│   └────────────────────────────────────────────────────────┘   │
│                                                                │
│   ┌────────────────────────────────────────────────────────┐   │
│   │ Slack                                                  │   │
│   │ ┌────────────┐                                         │   │
│   │ │ 💬 Slack  │  Status: ✅ Connected (via bot token)   │   │
│   │ │    ✓      │                                         │   │
│   │ └────────────┘                                         │   │
│   └────────────────────────────────────────────────────────┘   │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## 🧩 COMPONENT SPECIFICATIONS

### Component 1: MessageBubble

**Types:**
- User message (right-aligned)
- AI response (left-aligned, supports markdown)
- Action plan (special card with confirmation buttons)
- Clarification (special card with input fields)
- Execution result (success/error feedback)

**For AI responses, render:**
```jsx
<div className="message-bubble ai">
  <div className="avatar">🤖</div>
  <div className="content">
    <ReactMarkdown>{response}</ReactMarkdown>
    {agents_triggered && (
      <AgentContributions agents={agents_triggered} />
    )}
    {raw_data && showRawData && (
      <RawDataAccordion data={raw_data} />
    )}
  </div>
  <div className="timestamp">{formatTime(timestamp)}</div>
</div>
```

### Component 2: ActionConfirmationCard

When `response.type === "action_plan"`:

```jsx
<div className="action-card" data-risk={plan.risk_level}>
  <div className="action-header">
    <span className="action-icon">{getActionIcon(plan.steps[0].action)}</span>
    <span className="action-summary">{plan.summary}</span>
    <span className={`risk-badge risk-${plan.risk_level}`}>
      {plan.risk_level}
    </span>
  </div>
  
  <div className="action-preview">
    <ReactMarkdown>{plan.preview}</ReactMarkdown>
  </div>
  
  <div className="action-steps">
    {plan.steps.map(step => (
      <div className="step" key={step.step_number}>
        <span className="step-agent">{getAgentIcon(step.agent)}</span>
        <span className="step-desc">{step.description}</span>
      </div>
    ))}
  </div>
  
  <div className="action-buttons">
    <button className="btn-confirm" onClick={() => executeAction(plan.plan_id)}>
      ✓ Confirm
    </button>
    <button className="btn-edit" onClick={() => editAction(plan)}>
      ✏️ Edit
    </button>
    <button className="btn-cancel" onClick={() => cancelAction(plan.plan_id)}>
      ✕ Cancel
    </button>
  </div>
  
  <div className="action-meta">
    Estimated: {plan.estimated_duration}
  </div>
</div>
```

### Component 3: ClarificationCard

When `response.type === "clarification_needed"`:

```jsx
<div className="clarification-card">
  <div className="clarification-header">
    ⚠️ Need more information
  </div>
  
  <p className="clarification-message">{response.message}</p>
  
  {response.missing_parameters.map(param => (
    <div className="clarification-input" key={param}>
      <label>{formatParamName(param)}</label>
      <input 
        type="text" 
        placeholder={`Enter ${param}...`}
        value={clarificationInputs[param]}
        onChange={e => setClarificationInputs({...clarificationInputs, [param]: e.target.value})}
      />
    </div>
  ))}
  
  <button onClick={() => resubmitWithClarification()}>
    Submit Details
  </button>
</div>
```

### Component 4: AgentRelevanceIndicator (Sidebar)

```jsx
<div className="agent-scores">
  <h4>🎯 Data Source Relevance</h4>
  {agents.map(agent => (
    <div className="agent-score-row" key={agent.name}>
      <span className="agent-icon">{getAgentIcon(agent.name)}</span>
      <span className="agent-name">{agent.name}</span>
      <div className="score-bar">
        <div 
          className="score-fill" 
          style={{
            width: `${agent.score * 100}%`,
            backgroundColor: agent.score >= 0.5 ? 'var(--success)' : 'var(--text-secondary)'
          }}
        />
      </div>
      <span className="score-value">{(agent.score * 100).toFixed(0)}%</span>
      {agent.score >= 0.5 && <span className="active-badge">Active</span>}
    </div>
  ))}
</div>
```

### Component 5: SlackSearchResults

```jsx
<div className="slack-results">
  <div className="slack-header">
    <h4>💬 Slack Search Results</h4>
    <span className="result-count">
      {response.total_message_count} messages across {response.channels_searched} channels
    </span>
  </div>
  
  {/* Channel summaries */}
  <div className="channel-summaries">
    {response.channel_summaries.map(channel => (
      <div className="channel-card" key={channel.channel_id}>
        <div className="channel-header">
          <span className="channel-name">#{channel.channel_name}</span>
          <span className="message-count">{channel.message_count} matches</span>
          <span className="relevance">{(channel.relevance_score * 100).toFixed(0)}% relevant</span>
        </div>
        {channel.top_message_preview && (
          <div className="preview">{channel.top_message_preview}</div>
        )}
      </div>
    ))}
  </div>
  
  {/* Follow-up suggestions */}
  {response.follow_up_suggestions.length > 0 && (
    <div className="suggestions">
      <span>💡 Try:</span>
      {response.follow_up_suggestions.map(suggestion => (
        <button 
          key={suggestion.value} 
          onClick={() => applySuggestion(suggestion)}
          className="suggestion-chip"
        >
          {suggestion.label}
        </button>
      ))}
    </div>
  )}
</div>
```

### Component 6: ExecutionResult

After action execution:

```jsx
<div className={`execution-result ${result.success ? 'success' : 'error'}`}>
  <div className="result-header">
    {result.success ? '✅' : '❌'} {result.summary}
  </div>
  
  {result.links && Object.keys(result.links).length > 0 && (
    <div className="result-links">
      {result.links.html_link && (
        <a href={result.links.html_link} target="_blank" rel="noopener">
          📎 View in Calendar
        </a>
      )}
      {result.links.meeting_link && (
        <a href={result.links.meeting_link} target="_blank" rel="noopener">
          🎥 Join Meeting
        </a>
      )}
      {result.links.web_view_link && (
        <a href={result.links.web_view_link} target="_blank" rel="noopener">
          📄 Open Document
        </a>
      )}
    </div>
  )}
  
  {result.rollback_available && (
    <button className="btn-undo">↩️ Undo</button>
  )}
</div>
```

---

## 🔄 FRONTEND STATE MANAGEMENT

### Global State (React Context or Redux)

```javascript
const AppState = {
  // Authentication
  user: {
    id: "user_123",
    googleConnected: true,
    slackConnected: true
  },
  
  // Conversation history
  messages: [
    {
      id: "msg_1",
      type: "user" | "ai" | "action_plan" | "clarification" | "execution_result",
      content: "...",
      timestamp: Date,
      metadata: {...}
    }
  ],
  
  // Pending action plans
  pendingActions: {
    "plan_id_1": { ...actionPlan }
  },
  
  // Current agent scores (updated after each query)
  agentScores: {
    calendar: { score: 0.95, active: true, justification: "..." },
    gmail: { score: 0.2, active: false, justification: "..." },
    drive: { score: 0.1, active: false, justification: "..." }
  },
  
  // UI state
  ui: {
    isLoading: false,
    sidebarOpen: true,
    showRawData: false
  }
};
```

---

## 🔌 API INTEGRATION HOOKS

### `useQuery` Hook

```javascript
const useQuery = () => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  
  const sendQuery = async (query, userId) => {
    setLoading(true);
    try {
      const response = await fetch('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, user_id: userId })
      });
      
      const data = await response.json();
      
      // Handle different response types
      if (data.type === 'action_plan') {
        return { type: 'action_plan', data };
      } else if (data.type === 'clarification_needed') {
        return { type: 'clarification', data };
      } else {
        return { type: 'response', data };
      }
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  };
  
  return { sendQuery, loading, error };
};
```

### `useAction` Hook

```javascript
const useAction = () => {
  const executeAction = async (planId, userId) => {
    const response = await fetch('/api/action/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan_id: planId, user_id: userId })
    });
    return response.json();
  };
  
  const cancelAction = async (planId) => {
    const response = await fetch('/api/action/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan_id: planId })
    });
    return response.json();
  };
  
  return { executeAction, cancelAction };
};
```

### `useSlackSearch` Hook

```javascript
const useSlackSearch = () => {
  const searchSlack = async (query, options = {}) => {
    const response = await fetch('/api/slack/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query,
        user_id: options.userId,
        channel: options.channel,
        time_range: options.timeRange,
        limit: options.limit || 10
      })
    });
    return response.json();
  };
  
  const getChannels = async () => {
    const response = await fetch('/api/slack/channels');
    return response.json();
  };
  
  return { searchSlack, getChannels };
};
```

### `useAuth` Hook

```javascript
const useAuth = () => {
  const [authStatus, setAuthStatus] = useState(null);
  
  const checkAuthStatus = async (userId) => {
    const response = await fetch(`/api/auth/status?user_id=${userId}`);
    const data = await response.json();
    setAuthStatus(data);
    return data;
  };
  
  const getAuthUrl = async (userId) => {
    const response = await fetch(`/api/auth/google?user_id=${userId}`);
    return response.json();
  };
  
  const disconnect = async (userId) => {
    const response = await fetch(`/api/auth/disconnect?user_id=${userId}`, {
      method: 'DELETE'
    });
    return response.json();
  };
  
  return { authStatus, checkAuthStatus, getAuthUrl, disconnect };
};
```

---

## 📋 USER FLOW DIAGRAMS

### Flow 1: Read Query

```
User types: "What meetings do I have tomorrow?"
     │
     ▼
POST /api/query ──────────────────────────────────┐
     │                                            │
     ▼                                            │
Backend: Intent Classification                    │
     │ → query_type: "read"                       │
     │ → sources_needed: ["calendar"]             │
     │                                            │
     ▼                                            │
Backend: Relevance Scoring (parallel)             │
     │ → calendar: 0.95 ✓                        │
     │ → gmail: 0.20 ✗                           │
     │ → drive: 0.10 ✗                           │
     │                                            │
     ▼                                            │
Calendar Agent: Fetch Events                      │
     │                                            │
     ▼                                            │
LLM: Synthesize Response                          │
     │                                            │
     ▼                                            │
Return QueryResponse ─────────────────────────────┘
     │
     ▼
Frontend: Display markdown response with agent contributions
```

### Flow 2: Action Query with Confirmation

```
User types: "Schedule a meeting with john@company.com tomorrow at 2pm"
     │
     ▼
POST /api/query ─────────────────────────────────────────────┐
     │                                                       │
     ▼                                                       │
Backend: Intent Classification                               │
     │ → query_type: "action"                               │
     │ → action_type: "create_event"                        │
     │                                                       │
     ▼                                                       │
Backend: Create Action Plan                                  │
     │ → Extract parameters (title, time, attendees)        │
     │ → Generate preview                                   │
     │ → Store in pending_plans                             │
     │                                                       │
     ▼                                                       │
Return ActionPlanResponse ───────────────────────────────────┘
     │
     ▼
Frontend: Display ActionConfirmationCard
     │
     │  User clicks [Confirm]
     ▼
POST /api/action/execute ────────────────────────────────────┐
     │                                                       │
     ▼                                                       │
Calendar Agent: create_event()                               │
     │ → Call Google Calendar API                           │
     │ → Return event_id, html_link, meeting_link           │
     │                                                       │
     ▼                                                       │
Return ActionExecutionResponse ──────────────────────────────┘
     │
     ▼
Frontend: Display ExecutionResult with links
```

### Flow 3: Clarification Flow

```
User types: "Send email to Sarah about the deadline"
     │
     ▼
POST /api/query ─────────────────────────────────────────────┐
     │                                                       │
     ▼                                                       │
Backend: Parameter Extraction                                │
     │ → to: ["Sarah"] (missing full email)                 │
     │ → subject: "Regarding the deadline"                  │
     │ → body: "" (missing)                                 │
     │                                                       │
     ▼                                                       │
Return Clarification Response ───────────────────────────────┘
     │
     ▼
Frontend: Display ClarificationCard with input fields
     │
     │  User fills in: sarah@company.com, "The deadline is Friday"
     ▼
POST /api/query (with full query + context)
     │
     ▼
Normal action flow continues...
```

---

## 🎬 AVAILABLE ACTIONS BY AGENT

### 📅 Calendar Agent Actions

| Action Type | Description | Required Params | Optional Params |
|-------------|-------------|-----------------|-----------------|
| `create_event` | Create new event | `title`, `start_time` | `attendees`, `duration_minutes`, `location`, `description`, `add_meet_link` |
| `update_event` | Update existing event | `event_id` | `title`, `start_time`, `end_time`, `attendees_to_add`, `attendees_to_remove`, `location` |
| `delete_event` | Delete event | `event_id` | `send_updates` |
| `check_availability` | Check free/busy | `attendees` | `start_time`, `end_time`, `duration_minutes` |

### 📧 Gmail Agent Actions

| Action Type | Description | Required Params | Optional Params |
|-------------|-------------|-----------------|-----------------|
| `send_email` | Send email immediately | `to`, `subject`, `body` | `cc`, `bcc`, `is_html`, `thread_id` |
| `create_draft` | Create email draft | - | `to`, `subject`, `body`, `cc` |
| `reply_email` | Reply to existing email | `message_id`, `body` | `reply_all` |
| `forward_email` | Forward email | `message_id`, `to` | `additional_message` |

### 📁 Drive Agent Actions

| Action Type | Description | Required Params | Optional Params |
|-------------|-------------|-----------------|-----------------|
| `create_document` | Create Google Doc | `title` | `content`, `folder_id` |
| `create_spreadsheet` | Create Google Sheet | `title` | `folder_id`, `initial_data` |
| `share_file` | Share file with user | `file_id`, `email` | `role` (reader/commenter/writer), `send_notification`, `message` |
| `create_folder` | Create folder | `name` | `parent_id` |

---

## ✅ KEY FEATURES TO IMPLEMENT

### MUST HAVE:
1. ✅ Conversational chat interface with message history
2. ✅ Support for 3 response types (read, action, clarification)
3. ✅ Action confirmation cards with Confirm/Edit/Cancel
4. ✅ Real-time agent relevance score display
5. ✅ Google OAuth authentication flow
6. ✅ Markdown rendering for AI responses
7. ✅ Loading states and error handling
8. ✅ Execution result feedback with links

### NICE TO HAVE:
1. 📌 Keyboard shortcuts (Ctrl+Enter to send)
2. 📌 Message editing/regeneration
3. 📌 Conversation history persistence (localStorage)
4. 📌 Dark/light theme toggle
5. 📌 Slack channel autocomplete
6. 📌 Raw data toggle for debugging
7. 📌 Voice input support
8. 📌 Typing indicator animation

---

## 🔑 IMPORTANT NOTES FOR IMPLEMENTATION

### 1. User ID Management
Generate and store a unique `user_id` on first visit using localStorage:
```javascript
const getUserId = () => {
  let userId = localStorage.getItem('user_id');
  if (!userId) {
    userId = 'user_' + Math.random().toString(36).substr(2, 9);
    localStorage.setItem('user_id', userId);
  }
  return userId;
};
```

### 2. Authentication First
Check `/api/auth/status` on app load. If not connected, show auth prompt:
```javascript
useEffect(() => {
  const checkAuth = async () => {
    const status = await fetch(`/api/auth/status?user_id=${userId}`);
    const data = await status.json();
    if (!data.connected) {
      setShowAuthPrompt(true);
    }
  };
  checkAuth();
}, []);
```

### 3. Response Type Detection
Always check `response.type` field to determine how to render:
```javascript
const handleResponse = (response) => {
  if (response.type === 'action_plan') {
    return <ActionConfirmationCard plan={response} />;
  } else if (response.type === 'clarification_needed') {
    return <ClarificationCard data={response} />;
  } else if (response.type === 'error') {
    return <ErrorMessage error={response.message} />;
  } else {
    // Regular QueryResponse (type is undefined)
    return <AIMessage response={response} />;
  }
};
```

### 4. CORS Configuration
Backend allows all origins (`*`), so no CORS configuration needed on frontend.

### 5. Relevance Scoring Display
The backend HAS data source relevance scoring:
- Located in `orchestrator.py` → `_evaluate_agents()` method
- Uses LLM to score each agent 0.0-1.0
- Threshold is 0.5 (configurable in `config.py`)
- Results appear in `agents_triggered` array in response

### 6. Agent Color Coding
Use consistent colors for each agent:
- 📅 Calendar → Blue (#4285F4)
- 📧 Gmail → Red (#EA4335)
- 📁 Drive → Green (#34A853)
- 💬 Slack → Purple (#4A154B)

### 7. Risk Level Styling
Style action cards based on risk:
```css
.action-card[data-risk="low"] {
  border-left: 4px solid var(--success);
}
.action-card[data-risk="medium"] {
  border-left: 4px solid var(--warning);
}
.action-card[data-risk="high"] {
  border-left: 4px solid var(--danger);
}
```

---

## 📦 RECOMMENDED TECH STACK

### Framework
- **React 18+** with TypeScript (recommended)
- OR **Next.js 14** for SSR support

### Styling
- **Tailwind CSS** for rapid development
- OR **styled-components** / CSS Modules

### State Management
- **React Context + useReducer** for simple state
- OR **Zustand** for more complex state

### Markdown Rendering
- **react-markdown** with **remark-gfm** for GitHub-flavored markdown

### Additional Libraries
- **react-hot-toast** for notifications
- **framer-motion** for animations
- **date-fns** for date formatting
- **lucide-react** for icons

---

## 🚀 QUICK START TEMPLATE

```jsx
// App.jsx
import { useState, useEffect } from 'react';
import ChatInterface from './components/ChatInterface';
import Sidebar from './components/Sidebar';
import AuthPrompt from './components/AuthPrompt';

function App() {
  const [userId] = useState(() => {
    let id = localStorage.getItem('user_id');
    if (!id) {
      id = 'user_' + Math.random().toString(36).substr(2, 9);
      localStorage.setItem('user_id', id);
    }
    return id;
  });
  
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [agentScores, setAgentScores] = useState({});
  
  useEffect(() => {
    // Check auth on mount
    fetch(`/api/auth/status?user_id=${userId}`)
      .then(r => r.json())
      .then(data => setIsAuthenticated(data.connected));
  }, [userId]);
  
  if (!isAuthenticated) {
    return <AuthPrompt userId={userId} onAuth={() => setIsAuthenticated(true)} />;
  }
  
  return (
    <div className="app-container">
      <Sidebar agentScores={agentScores} />
      <ChatInterface 
        userId={userId} 
        onAgentScoresUpdate={setAgentScores}
      />
    </div>
  );
}

export default App;
```

---

## 📝 SAMPLE API CALLS FOR TESTING

### Test Read Query
```bash
curl -X POST http://localhost:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What meetings do I have this week?", "user_id": "test_user"}'
```

### Test Action Query
```bash
curl -X POST http://localhost:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Schedule a meeting with john@example.com tomorrow at 2pm", "user_id": "test_user"}'
```

### Test Execute Action
```bash
curl -X POST http://localhost:8000/api/action/execute \
  -H "Content-Type: application/json" \
  -d '{"plan_id": "abc123", "user_id": "test_user"}'
```

### Test Slack Search
```bash
curl -X POST http://localhost:8000/api/slack/search \
  -H "Content-Type: application/json" \
  -d '{"query": "deployment", "user_id": "test_user"}'
```

### Test Health Check
```bash
curl http://localhost:8000/api/health
```

---

This document contains everything needed to build a complete frontend for the Multi-Agent Data Connector system. Good luck with your Replit implementation! 🚀

