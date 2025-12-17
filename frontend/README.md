# Multi-Agent Data Connector - Frontend

A modern React/Next.js frontend for the Multi-Agent Data Connector system. This conversational interface allows users to interact with Google Calendar, Gmail, Google Drive, and Slack through natural language.

## Features

- **Conversational Interface**: Chat-based UI for natural language queries
- **Smart Agent Routing**: Backend automatically routes queries to relevant data sources
- **Action Confirmation**: Safe execution of actions with preview and confirmation
- **Real-time Feedback**: Loading indicators, agent badges, and execution results
- **Slack Search**: Dedicated search interface for Slack messages
- **Google OAuth**: Seamless authentication with Google services
- **Dark Mode**: Modern dark theme with glass-morphism design

## Tech Stack

- **Next.js 14** - React framework with App Router
- **TypeScript** - Type safety
- **Tailwind CSS** - Utility-first styling
- **Zustand** - State management
- **Lucide React** - Icons
- **React Markdown** - Markdown rendering
- **date-fns** - Date formatting

## Getting Started

### Prerequisites

- Node.js 18+ 
- npm or yarn
- Backend API running on `localhost:8000`

### Installation

```bash
# Navigate to frontend directory
cd frontend

# Install dependencies
npm install

# Start development server
npm run dev
```

The app will be available at `http://localhost:3000`

### Backend Connection

The frontend proxies API requests to the backend via Next.js rewrites configured in `next.config.js`. Ensure the backend is running:

```bash
# From project root
cd ..
python app.py
```

## Project Structure

```
frontend/
├── components/           # React components
│   ├── ActionConfirmation.tsx   # Action plan modal
│   ├── AgentBadge.tsx          # Agent indicators
│   ├── AuthButton.tsx          # Google OAuth button
│   ├── ChatInterface.tsx       # Main chat UI
│   ├── DataCards.tsx           # Calendar/Email/File cards
│   ├── LoadingIndicator.tsx    # Loading states
│   ├── MessageBubble.tsx       # Chat message rendering
│   └── SlackSearch.tsx         # Slack search modal
├── hooks/               # React hooks
│   ├── useAuth.ts              # Authentication state
│   ├── useChat.ts              # Chat state (Zustand)
│   └── useActions.ts           # Action handling
├── services/            # API layer
│   ├── api.ts                  # API client functions
│   └── types.ts                # TypeScript interfaces
├── pages/               # Next.js pages
│   ├── _app.tsx               # App wrapper
│   └── index.tsx              # Main page
├── styles/              # Global styles
│   └── globals.css            # Tailwind + custom CSS
└── public/              # Static assets
```

## API Integration

The frontend integrates with these backend endpoints:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/query` | POST | Main conversational query |
| `/api/action/execute` | POST | Execute confirmed action |
| `/api/action/cancel` | POST | Cancel pending action |
| `/api/auth/google` | GET | Get OAuth URL |
| `/api/auth/status` | GET | Check auth status |
| `/api/slack/search` | POST | Search Slack |
| `/api/slack/channels` | GET | List channels |

## Response Types

The frontend handles these response types from `/api/query`:

1. **QueryResponse** - Normal read query with synthesized answer
2. **ActionPlanResponse** - Action requiring confirmation
3. **ClarificationResponse** - Missing information needed
4. **ErrorResponse** - Error handling

## Customization

### Styling

Global styles are in `styles/globals.css`. The design uses:
- Custom CSS properties for theming
- Tailwind utilities with custom components
- Glass-morphism effects with backdrop blur
- Agent-specific color coding

### Adding New Agents

1. Add agent type to `services/types.ts`
2. Add badge config in `components/AgentBadge.tsx`
3. Add data card in `components/DataCards.tsx` if needed

## Environment Variables

Create `.env.local` for environment-specific settings:

```env
# Override API base URL if needed
NEXT_PUBLIC_API_URL=http://localhost:8000
```

## Scripts

```bash
npm run dev      # Start development server
npm run build    # Build for production
npm run start    # Start production server
npm run lint     # Run ESLint
```

## Browser Support

- Chrome/Edge 90+
- Firefox 88+
- Safari 14+

## License

MIT

