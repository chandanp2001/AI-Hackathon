// ============================================================================
// API Request/Response Types
// ============================================================================

export interface QueryRequest {
  query: string;
  user_id: string;
  threshold_override?: number;
}

export interface AgentContribution {
  agent_name: 'calendar' | 'gmail' | 'drive' | 'slack';
  relevance_score: number;
  justification: string;
  data_count: number;
  execution_time_ms: number;
}

// READ Query Response
export interface QueryResponse {
  query: string;
  response: string;
  agents_triggered: AgentContribution[];
  raw_data?: Record<string, unknown>;
  total_execution_time_ms: number;
  timestamp: string;
}

// ACTION Query Response
export interface ActionStep {
  step_number: number;
  action: string;
  description: string;
  agent: string;
}

export interface ActionPlanResponse {
  type: 'action_plan';
  plan_id: string;
  query: string;
  summary: string;
  preview: string;
  steps: ActionStep[];
  risk_level: 'low' | 'medium' | 'high';
  requires_confirmation: boolean;
  estimated_duration: string;
  execution_time_ms: number;
}

// Clarification Response (for actions)
export interface ClarificationResponse {
  type: 'clarification_needed';
  message: string;
  missing_parameters: string[];
  partial_plan?: Record<string, unknown>;
  execution_time_ms: number;
}

// Query Clarification Response (for ambiguous read queries)
export interface QueryClarificationResponse {
  type: 'clarification';
  query: string;
  needs_clarification: boolean;
  reason: string;
  suggested_questions: string[];
  likely_sources: string[];
  agent_scores?: Record<string, number>;
}

// Error Response
export interface ErrorResponse {
  type: 'error';
  message: string;
  execution_time_ms?: number;
}

// Union type for all API responses
export type ApiResponse = QueryResponse | ActionPlanResponse | ClarificationResponse | QueryClarificationResponse | ErrorResponse;

// Action Execution
export interface ActionExecuteRequest {
  plan_id: string;
  user_id: string;
}

export interface StepResult {
  step_number: number;
  success: boolean;
  result_data?: {
    event_id?: string;
    html_link?: string;
    meeting_link?: string;
    web_view_link?: string;
    message_id?: string;
    draft_id?: string;
    file_id?: string;
    [key: string]: unknown;
  };
  error_message?: string;
  execution_time_ms?: number;
}

export interface ActionExecutionResponse {
  plan_id: string;
  success: boolean;
  summary: string;
  step_results: StepResult[];
  links: Record<string, string>;
  rollback_available: boolean;
}

// ============================================================================
// Authentication Types
// ============================================================================

export interface AuthUrlResponse {
  authorization_url: string;
  state: string;
}

export interface AuthStatusResponse {
  connected: boolean;
  scopes?: string[];
  expires_at?: string;
  error?: string;
}

// ============================================================================
// Slack Types
// ============================================================================

export interface SlackSearchRequest {
  query: string;
  user_id: string;
  channel?: string;
  channels?: string[];
  time_range?: string;
  limit?: number;
  max_channels?: number;
  include_dms?: boolean;
  deep_search?: boolean;
}

export interface SlackChannelSummary {
  channel_id: string;
  channel_name: string;
  message_count: number;
  relevance_score: number;
  top_message_preview?: string;
}

export interface SlackFollowUpSuggestion {
  type: string;
  value: string;
  label: string;
  description?: string;
}

export interface SlackSearchResponse {
  query: string;
  results: Record<string, unknown>;
  channel_summaries: SlackChannelSummary[];
  total_message_count: number;
  channels_searched: number;
  follow_up_suggestions: SlackFollowUpSuggestion[];
  summary: string;
  is_global_search: boolean;
  processing_time_ms: number;
}

export interface SlackChannel {
  name: string;
  id: string;
  is_private: boolean;
}

// ============================================================================
// Data Types (from raw_data)
// ============================================================================

export interface CalendarEvent {
  event_id: string;
  title: string;
  start_time: string;
  end_time: string;
  location?: string;
  description?: string;
  attendees: string[];
  organizer?: string;
  meeting_link?: string;
  status: string;
  is_all_day: boolean;
}

export interface EmailMessage {
  message_id: string;
  thread_id: string;
  subject: string;
  sender: string;
  recipients: string[];
  date: string;
  snippet: string;
  body_preview?: string;
  labels: string[];
  has_attachments: boolean;
  is_unread: boolean;
}

export interface DriveFile {
  file_id: string;
  name: string;
  mime_type: string;
  created_time?: string;
  modified_time?: string;
  size_bytes?: number;
  web_view_link?: string;
  owners: string[];
  shared: boolean;
  parent_folders: string[];
  content_preview?: string;
}

// ============================================================================
// Chat/Message Types
// ============================================================================

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: Date;
  metadata?: {
    agents_triggered?: AgentContribution[];
    action_plan?: ActionPlanResponse;
    execution_result?: ActionExecutionResponse;
    clarification?: ClarificationResponse;
    raw_data?: Record<string, unknown>;
    isLoading?: boolean;
    // Query clarification fields
    needs_clarification?: boolean;
    reason?: string;
    suggested_questions?: string[];
    likely_sources?: string[];
  };
}

export interface ChatState {
  messages: Message[];
  isLoading: boolean;
  pendingAction: ActionPlanResponse | null;
  userId: string;
  isAuthenticated: boolean;
}

// ============================================================================
// Session Types
// ============================================================================

export interface Session {
  session_id: string;
  user_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface SessionMessage {
  message_id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  metadata?: Record<string, unknown>;
  created_at: string;
}

export interface SessionWithMessages extends Session {
  messages: SessionMessage[];
  summary?: string;
}

export interface SessionListResponse {
  sessions: Session[];
  total: number;
}

export interface SessionCreateRequest {
  user_id: string;
  title?: string;
}

export interface QueryRequestWithSession extends QueryRequest {
  session_id?: string;
}

// ============================================================================
// Health/Metrics Types
// ============================================================================

export interface HealthResponse {
  status: string;
  orchestrator: Record<string, unknown>;
  auth_service: boolean;
  agents: Record<string, boolean>;
}

