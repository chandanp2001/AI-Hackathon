import {
  QueryRequest,
  ApiResponse,
  ActionExecuteRequest,
  ActionExecutionResponse,
  AuthUrlResponse,
  AuthStatusResponse,
  SlackSearchRequest,
  SlackSearchResponse,
  SlackChannel,
  HealthResponse,
  ActionPlanResponse,
  Session,
  SessionWithMessages,
  SessionListResponse,
  SessionCreateRequest,
  QueryRequestWithSession,
} from './types';

// API base URL - using Next.js rewrite proxy
const API_BASE = '/api';

// Default timeout for long-running requests (2 minutes)
const LONG_TIMEOUT = 120000;

// Helper to create a fetch with timeout
async function fetchWithTimeout(
  url: string, 
  options: RequestInit = {}, 
  timeout: number = LONG_TIMEOUT
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  
  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    return response;
  } catch (error) {
    clearTimeout(timeoutId);
    if (error instanceof Error && error.name === 'AbortError') {
      throw new Error('Request timed out. The server is taking too long to respond.');
    }
    throw error;
  }
}

// ============================================================================
// Query API
// ============================================================================

export async function sendQuery(request: QueryRequest): Promise<ApiResponse> {
  const response = await fetchWithTimeout(`${API_BASE}/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

// ============================================================================
// Action API
// ============================================================================

export async function executeAction(request: ActionExecuteRequest): Promise<ActionExecutionResponse> {
  const response = await fetch(`${API_BASE}/action/execute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function cancelAction(planId: string): Promise<{ status: string; message: string }> {
  const response = await fetch(`${API_BASE}/action/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId }),
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function getActionPlan(planId: string): Promise<ActionPlanResponse> {
  const response = await fetch(`${API_BASE}/action/${planId}`);
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

// ============================================================================
// Authentication API
// ============================================================================

export async function getAuthUrl(userId: string): Promise<AuthUrlResponse> {
  const response = await fetch(`${API_BASE}/auth/google?user_id=${encodeURIComponent(userId)}`);
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function checkAuthStatus(userId: string): Promise<AuthStatusResponse> {
  const response = await fetch(`${API_BASE}/auth/status?user_id=${encodeURIComponent(userId)}`);
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function disconnectAuth(userId: string): Promise<{ status: string; message: string }> {
  const response = await fetch(`${API_BASE}/auth/disconnect?user_id=${encodeURIComponent(userId)}`, {
    method: 'DELETE',
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

// ============================================================================
// Slack API
// ============================================================================

export async function searchSlack(request: SlackSearchRequest): Promise<SlackSearchResponse> {
  const response = await fetch(`${API_BASE}/slack/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function getSlackChannels(
  limit: number = 50,
  includePrivate: boolean = true
): Promise<{ channels: SlackChannel[]; total: number; cache_age_seconds?: number }> {
  const params = new URLSearchParams({
    limit: limit.toString(),
    include_private: includePrivate.toString(),
  });
  
  const response = await fetch(`${API_BASE}/slack/channels?${params}`);
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

// ============================================================================
// System API
// ============================================================================

export async function checkHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE}/health`);
  
  if (!response.ok) {
    throw new Error(`Health check failed: HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function getMetrics(): Promise<Record<string, unknown>> {
  const response = await fetch(`${API_BASE}/metrics`);
  
  if (!response.ok) {
    throw new Error(`Metrics fetch failed: HTTP ${response.status}`);
  }
  
  return response.json();
}

// ============================================================================
// Session API
// ============================================================================

export async function listSessions(
  userId: string,
  limit: number = 50,
  offset: number = 0
): Promise<SessionListResponse> {
  const params = new URLSearchParams({
    user_id: userId,
    limit: limit.toString(),
    offset: offset.toString(),
  });
  
  const response = await fetch(`${API_BASE}/sessions?${params}`);
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function createSession(request: SessionCreateRequest): Promise<Session> {
  const response = await fetch(`${API_BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function getSession(sessionId: string): Promise<SessionWithMessages> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}`);
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function deleteSession(sessionId: string): Promise<{ status: string; message: string }> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}`, {
    method: 'DELETE',
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function updateSessionTitle(
  sessionId: string,
  title: string
): Promise<{ status: string; message: string }> {
  const params = new URLSearchParams({ title });
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/title?${params}`, {
    method: 'PATCH',
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

export async function sendQueryWithSession(
  request: QueryRequestWithSession
): Promise<ApiResponse & { session_id?: string }> {
  const response = await fetchWithTimeout(`${API_BASE}/query/session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }
  
  return response.json();
}

// ============================================================================
// Helper Functions
// ============================================================================

export function isActionPlanResponse(response: ApiResponse): response is ApiResponse & { type: 'action_plan' } {
  return 'type' in response && response.type === 'action_plan';
}

export function isClarificationResponse(response: ApiResponse): response is ApiResponse & { type: 'clarification_needed' } {
  return 'type' in response && response.type === 'clarification_needed';
}

export function isQueryClarificationResponse(response: ApiResponse): response is ApiResponse & { type: 'clarification' } {
  return 'type' in response && response.type === 'clarification';
}

export function isErrorResponse(response: ApiResponse): response is ApiResponse & { type: 'error' } {
  return 'type' in response && response.type === 'error';
}

export function isQueryResponse(response: ApiResponse): response is ApiResponse & { response: string } {
  return 'response' in response && typeof response.response === 'string' && !('type' in response);
}

