import { create } from 'zustand';
import { parseISO, isValid } from 'date-fns';
import {
  Message,
  ApiResponse,
  ActionPlanResponse,
  ActionExecutionResponse,
  ClarificationResponse,
  QueryClarificationResponse,
  SessionMessage,
} from '@/services/types';
import {
  sendQuery,
  sendQueryWithSession,
  executeAction,
  cancelAction,
  isActionPlanResponse,
  isClarificationResponse,
  isQueryClarificationResponse,
  isErrorResponse,
  isQueryResponse,
} from '@/services/api';

// Generate simple unique IDs without external dependencies
function generateId(): string {
  return Date.now().toString(36) + Math.random().toString(36).substring(2);
}

/**
 * Parse timestamp from string (ISO format) or return current time.
 * Handles backend UTC timestamps properly.
 */
function parseTimestampFromString(dateStr: string): Date {
  // Try parsing as ISO string
  const parsed = parseISO(dateStr);
  if (isValid(parsed)) {
    return parsed;
  }
  // Fallback to Date constructor
  const fallback = new Date(dateStr);
  if (isValid(fallback)) {
    return fallback;
  }
  // Last resort
  return new Date();
}

interface ChatStore {
  messages: Message[];
  isLoading: boolean;
  pendingAction: ActionPlanResponse | null;
  userId: string;
  sessionId: string | null;
  
  // Actions
  setUserId: (userId: string) => void;
  setSessionId: (sessionId: string | null) => void;
  addMessage: (role: 'user' | 'assistant' | 'system', content: string, metadata?: Message['metadata']) => string;
  updateMessage: (id: string, updates: Partial<Message>) => void;
  clearMessages: () => void;
  loadSessionMessages: (messages: SessionMessage[]) => void;
  
  // Query handling
  sendMessage: (query: string) => Promise<void>;
  
  // Action handling
  confirmAction: (editedParameters?: Record<string, unknown>) => Promise<void>;
  cancelPendingAction: () => Promise<void>;
  setPendingAction: (action: ActionPlanResponse | null) => void;
}

export const useChatStore = create<ChatStore>((set, get) => ({
  messages: [],
  isLoading: false,
  pendingAction: null,
  userId: 'default_user',
  sessionId: null,

  setUserId: (userId: string) => set({ userId }),
  
  setSessionId: (sessionId: string | null) => set({ sessionId }),

  addMessage: (role, content, metadata) => {
    const id = generateId();
    const message: Message = {
      id,
      role,
      content,
      timestamp: new Date(),
      metadata,
    };
    set((state) => ({ messages: [...state.messages, message] }));
    return id;
  },

  updateMessage: (id, updates) => {
    set((state) => ({
      messages: state.messages.map((msg) =>
        msg.id === id ? { ...msg, ...updates } : msg
      ),
    }));
  },

  clearMessages: () => set({ messages: [], pendingAction: null }),

  loadSessionMessages: (sessionMessages: SessionMessage[]) => {
    const messages: Message[] = sessionMessages.map((msg) => ({
      id: msg.message_id,
      role: msg.role,
      content: msg.content,
      timestamp: parseTimestampFromString(msg.created_at),
      metadata: msg.metadata as Message['metadata'],
    }));
    set({ messages, pendingAction: null });
  },

  sendMessage: async (query: string) => {
    const { userId, sessionId, addMessage, updateMessage, setPendingAction } = get();
    
    // Add user message
    addMessage('user', query);
    
    // Add loading message
    const loadingId = addMessage('assistant', '', { isLoading: true });
    set({ isLoading: true });

    try {
      let response: ApiResponse & { session_id?: string };
      
      // Use session-aware endpoint if we have a session
      if (sessionId) {
        response = await sendQueryWithSession({ 
          query, 
          user_id: userId, 
          session_id: sessionId 
        });
      } else {
        // Try to create a new session with the query
        response = await sendQueryWithSession({ 
          query, 
          user_id: userId 
        });
        
        // Update session ID if a new one was created
        if (response.session_id) {
          set({ sessionId: response.session_id });
        }
      }
      
      // Handle different response types
      if (isActionPlanResponse(response)) {
        const actionPlan = response as ActionPlanResponse;
        setPendingAction(actionPlan);
        updateMessage(loadingId, {
          content: actionPlan.preview,
          metadata: {
            action_plan: actionPlan,
            isLoading: false,
          },
        });
      } else if (isClarificationResponse(response)) {
        const clarification = response as ClarificationResponse;
        updateMessage(loadingId, {
          content: clarification.message,
          metadata: {
            clarification,
            isLoading: false,
          },
        });
      } else if (isQueryClarificationResponse(response)) {
        // Handle query clarification (ambiguous read queries)
        const clarification = response as QueryClarificationResponse;
        updateMessage(loadingId, {
          content: clarification.reason || "I need a bit more information to help you.",
          metadata: {
            needs_clarification: true,
            reason: clarification.reason,
            suggested_questions: clarification.suggested_questions,
            likely_sources: clarification.likely_sources,
            isLoading: false,
          },
        });
      } else if (isErrorResponse(response)) {
        updateMessage(loadingId, {
          content: `Error: ${response.message}`,
          metadata: { isLoading: false },
        });
      } else if (isQueryResponse(response)) {
        updateMessage(loadingId, {
          content: response.response,
          metadata: {
            agents_triggered: response.agents_triggered,
            raw_data: response.raw_data,
            isLoading: false,
          },
        });
      } else {
        // Fallback: If response doesn't match any known type, try to extract content
        console.warn('Unknown response type:', response);
        const content = (response as any).response || JSON.stringify(response);
        updateMessage(loadingId, {
          content: typeof content === 'string' ? content : 'Received response but format is unexpected',
          metadata: {
            raw_data: response,
            isLoading: false,
          },
        });
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'An error occurred';
      updateMessage(loadingId, {
        content: `Error: ${errorMessage}`,
        metadata: { isLoading: false },
      });
    } finally {
      set({ isLoading: false });
    }
  },

  confirmAction: async (editedParameters?: Record<string, unknown>) => {
    const { pendingAction, userId, addMessage, updateMessage } = get();
    
    if (!pendingAction) return;
    
    const loadingId = addMessage('assistant', 'Executing action...', { isLoading: true });
    set({ isLoading: true });

    try {
      const result: ActionExecutionResponse = await executeAction({
        plan_id: pendingAction.plan_id,
        user_id: userId,
        edited_parameters: editedParameters,
      });
      
      updateMessage(loadingId, {
        content: result.summary,
        metadata: {
          execution_result: result,
          isLoading: false,
        },
      });
      
      set({ pendingAction: null });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'Action failed';
      updateMessage(loadingId, {
        content: `Action failed: ${errorMessage}`,
        metadata: { isLoading: false },
      });
    } finally {
      set({ isLoading: false });
    }
  },

  cancelPendingAction: async () => {
    const { pendingAction, addMessage } = get();
    
    if (!pendingAction) return;

    try {
      await cancelAction(pendingAction.plan_id);
      addMessage('system', 'Action cancelled.');
    } catch (error) {
      // Action may have already expired, which is fine
      addMessage('system', 'Action cancelled.');
    } finally {
      set({ pendingAction: null });
    }
  },

  setPendingAction: (action) => set({ pendingAction: action }),
}));

// Hook for accessing chat state
export function useChat() {
  const store = useChatStore();
  return store;
}
