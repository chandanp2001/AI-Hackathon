import React, { useEffect, useState } from 'react';
import { 
  MessageSquare, 
  Plus, 
  Trash2, 
  Edit2, 
  Check, 
  X,
  Clock,
  ChevronRight
} from 'lucide-react';
import { parseISO, isValid } from 'date-fns';
import { useSessions } from '@/hooks/useSessions';
import { Session } from '@/services/types';

interface SessionSidebarProps {
  userId: string;
  isAuthenticated: boolean;
  onSessionChange?: (sessionId: string | null) => void;
}

export function SessionSidebar({ 
  userId, 
  isAuthenticated,
  onSessionChange 
}: SessionSidebarProps) {
  const {
    sessions,
    currentSessionId,
    isLoading,
    error,
    fetchSessions,
    selectSession,
    createNewSession,
    removeSession,
    updateTitle,
    clearCurrentSession,
  } = useSessions();

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState('');

  // Fetch sessions on mount
  useEffect(() => {
    if (isAuthenticated && userId) {
      fetchSessions(userId);
    }
  }, [isAuthenticated, userId, fetchSessions]);

  // Notify parent of session changes
  useEffect(() => {
    onSessionChange?.(currentSessionId);
  }, [currentSessionId, onSessionChange]);

  const handleNewSession = async () => {
    try {
      await createNewSession(userId);
    } catch (err) {
      console.error('Failed to create session:', err);
    }
  };

  const handleSelectSession = async (sessionId: string) => {
    if (sessionId !== currentSessionId) {
      await selectSession(sessionId);
    }
  };

  const handleDeleteSession = async (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    if (confirm('Delete this conversation?')) {
      await removeSession(sessionId);
    }
  };

  const handleStartEdit = (e: React.MouseEvent, session: Session) => {
    e.stopPropagation();
    setEditingId(session.session_id);
    setEditTitle(session.title || '');
  };

  const handleSaveEdit = async (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    if (editTitle.trim()) {
      await updateTitle(sessionId, editTitle.trim());
    }
    setEditingId(null);
    setEditTitle('');
  };

  const handleCancelEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingId(null);
    setEditTitle('');
  };

  const formatDate = (dateStr: string) => {
    // Parse ISO timestamp properly - handle both formats
    let date: Date;
    
    // Try parsing as ISO string first
    const parsed = parseISO(dateStr);
    if (isValid(parsed)) {
      date = parsed;
    } else {
      // Fallback to Date constructor
      date = new Date(dateStr);
    }
    
    // Ensure we have a valid date
    if (!isValid(date)) {
      return 'Unknown';
    }
    
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 0) return 'Just now'; // Handle future dates gracefully
    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
  };

  if (!isAuthenticated) {
    return (
      <div className="p-4 text-center text-slate-500 text-sm">
        Connect your account to view sessions
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-slate-700/50">
        <button
          onClick={handleNewSession}
          disabled={isLoading}
          className="w-full flex items-center justify-center gap-2 px-4 py-2.5
                     bg-primary-500 hover:bg-primary-600 disabled:opacity-50
                     text-white rounded-xl font-medium transition-all
                     shadow-lg shadow-primary-500/25"
        >
          <Plus className="w-4 h-4" />
          New Chat
        </button>
      </div>

      {/* Sessions List */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {error && (
          <div className="p-3 text-xs text-red-400 bg-red-500/10 rounded-lg mb-2">
            {error}
          </div>
        )}

        {isLoading && sessions.length === 0 ? (
          <div className="flex items-center justify-center py-8">
            <div className="w-6 h-6 border-2 border-primary-500 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : sessions.length === 0 ? (
          <div className="text-center py-8 text-slate-500 text-sm">
            <MessageSquare className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p>No conversations yet</p>
            <p className="text-xs mt-1">Start a new chat above</p>
          </div>
        ) : (
          sessions.map((session) => (
            <div
              key={session.session_id}
              onClick={() => handleSelectSession(session.session_id)}
              className={`group relative p-3 rounded-xl cursor-pointer transition-all
                         ${currentSessionId === session.session_id
                           ? 'bg-primary-500/20 border border-primary-500/30'
                           : 'hover:bg-slate-700/50 border border-transparent'
                         }`}
            >
              {editingId === session.session_id ? (
                <div className="flex items-center gap-2" onClick={e => e.stopPropagation()}>
                  <input
                    type="text"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    className="flex-1 px-2 py-1 text-sm bg-slate-800 border border-slate-600 
                               rounded text-white focus:outline-none focus:border-primary-500"
                    autoFocus
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') handleSaveEdit(e as any, session.session_id);
                      if (e.key === 'Escape') handleCancelEdit(e as any);
                    }}
                  />
                  <button
                    onClick={(e) => handleSaveEdit(e, session.session_id)}
                    className="p-1 text-green-400 hover:bg-green-500/20 rounded"
                  >
                    <Check className="w-4 h-4" />
                  </button>
                  <button
                    onClick={handleCancelEdit}
                    className="p-1 text-red-400 hover:bg-red-500/20 rounded"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              ) : (
                <>
                  <div className="flex items-start gap-2">
                    <MessageSquare className={`w-4 h-4 mt-0.5 flex-shrink-0
                                              ${currentSessionId === session.session_id
                                                ? 'text-primary-400'
                                                : 'text-slate-500'
                                              }`} />
                    <div className="flex-1 min-w-0">
                      <p className={`text-sm font-medium truncate
                                    ${currentSessionId === session.session_id
                                      ? 'text-white'
                                      : 'text-slate-300'
                                    }`}>
                        {session.title || 'New Conversation'}
                      </p>
                      <p className="text-xs text-slate-500 flex items-center gap-1 mt-0.5">
                        <Clock className="w-3 h-3" />
                        {formatDate(session.updated_at)}
                      </p>
                    </div>
                    <ChevronRight className={`w-4 h-4 text-slate-500 opacity-0 group-hover:opacity-100
                                             transition-opacity ${currentSessionId === session.session_id ? 'opacity-100' : ''}`} />
                  </div>

                  {/* Action buttons - show on hover */}
                  <div className="absolute right-2 top-2 flex items-center gap-1 opacity-0 
                                  group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={(e) => handleStartEdit(e, session)}
                      className="p-1.5 text-slate-400 hover:text-white hover:bg-slate-600 
                                 rounded-lg transition-colors"
                      title="Rename"
                    >
                      <Edit2 className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={(e) => handleDeleteSession(e, session.session_id)}
                      className="p-1.5 text-slate-400 hover:text-red-400 hover:bg-red-500/20 
                                 rounded-lg transition-colors"
                      title="Delete"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </>
              )}
            </div>
          ))
        )}
      </div>

      {/* Footer */}
      {currentSessionId && (
        <div className="p-3 border-t border-slate-700/50">
          <button
            onClick={clearCurrentSession}
            className="w-full text-xs text-slate-500 hover:text-slate-300 
                       py-2 rounded-lg hover:bg-slate-700/50 transition-colors"
          >
            Clear selection
          </button>
        </div>
      )}
    </div>
  );
}

