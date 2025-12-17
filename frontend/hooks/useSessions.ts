import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { parseISO, isValid } from 'date-fns';
import {
  Session,
  SessionWithMessages,
  SessionMessage,
} from '@/services/types';
import {
  listSessions,
  createSession,
  getSession,
  deleteSession,
  updateSessionTitle,
} from '@/services/api';

/**
 * Ensure session timestamps are properly sorted.
 * Handles ISO string timestamps from the backend.
 */
function parseSessionTimestamp(dateStr: string): number {
  const parsed = parseISO(dateStr);
  if (isValid(parsed)) {
    return parsed.getTime();
  }
  const fallback = new Date(dateStr);
  if (isValid(fallback)) {
    return fallback.getTime();
  }
  return 0;
}

interface SessionsState {
  sessions: Session[];
  currentSessionId: string | null;
  currentSession: SessionWithMessages | null;
  isLoading: boolean;
  error: string | null;
  
  // Actions
  fetchSessions: (userId: string) => Promise<void>;
  selectSession: (sessionId: string) => Promise<void>;
  createNewSession: (userId: string) => Promise<Session>;
  removeSession: (sessionId: string) => Promise<void>;
  updateTitle: (sessionId: string, title: string) => Promise<void>;
  clearCurrentSession: () => void;
  setCurrentSessionId: (sessionId: string | null) => void;
  addMessageToCurrentSession: (message: SessionMessage) => void;
  updateSessionInList: (session: Session) => void;
}

export const useSessionsStore = create<SessionsState>()(
  persist(
    (set, get) => ({
      sessions: [],
      currentSessionId: null,
      currentSession: null,
      isLoading: false,
      error: null,

      fetchSessions: async (userId: string) => {
        set({ isLoading: true, error: null });
        try {
          const response = await listSessions(userId);
          set({ sessions: response.sessions, isLoading: false });
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Failed to fetch sessions';
          set({ error: message, isLoading: false });
        }
      },

      selectSession: async (sessionId: string) => {
        set({ isLoading: true, error: null });
        try {
          const session = await getSession(sessionId);
          set({ 
            currentSessionId: sessionId, 
            currentSession: session,
            isLoading: false 
          });
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Failed to load session';
          set({ error: message, isLoading: false });
        }
      },

      createNewSession: async (userId: string) => {
        set({ isLoading: true, error: null });
        try {
          const session = await createSession({ user_id: userId });
          set((state) => ({ 
            sessions: [session, ...state.sessions],
            currentSessionId: session.session_id,
            currentSession: {
              ...session,
              messages: [],
              summary: undefined,
            },
            isLoading: false 
          }));
          return session;
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Failed to create session';
          set({ error: message, isLoading: false });
          throw error;
        }
      },

      removeSession: async (sessionId: string) => {
        try {
          await deleteSession(sessionId);
          set((state) => {
            const newSessions = state.sessions.filter(s => s.session_id !== sessionId);
            const needsClear = state.currentSessionId === sessionId;
            return {
              sessions: newSessions,
              currentSessionId: needsClear ? null : state.currentSessionId,
              currentSession: needsClear ? null : state.currentSession,
            };
          });
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Failed to delete session';
          set({ error: message });
        }
      },

      updateTitle: async (sessionId: string, title: string) => {
        try {
          await updateSessionTitle(sessionId, title);
          set((state) => ({
            sessions: state.sessions.map(s => 
              s.session_id === sessionId ? { ...s, title } : s
            ),
            currentSession: state.currentSession?.session_id === sessionId
              ? { ...state.currentSession, title }
              : state.currentSession,
          }));
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Failed to update title';
          set({ error: message });
        }
      },

      clearCurrentSession: () => {
        set({ currentSessionId: null, currentSession: null });
      },

      setCurrentSessionId: (sessionId: string | null) => {
        set({ currentSessionId: sessionId });
      },

      addMessageToCurrentSession: (message: SessionMessage) => {
        set((state) => {
          if (!state.currentSession) return state;
          return {
            currentSession: {
              ...state.currentSession,
              messages: [...state.currentSession.messages, message],
            },
          };
        });
      },

      updateSessionInList: (session: Session) => {
        set((state) => ({
          sessions: state.sessions.map(s => 
            s.session_id === session.session_id ? session : s
          ).sort((a, b) => 
            parseSessionTimestamp(b.updated_at) - parseSessionTimestamp(a.updated_at)
          ),
        }));
      },
    }),
    {
      name: 'sessions-storage',
      partialize: (state) => ({ 
        currentSessionId: state.currentSessionId,
      }),
    }
  )
);

export function useSessions() {
  return useSessionsStore();
}

