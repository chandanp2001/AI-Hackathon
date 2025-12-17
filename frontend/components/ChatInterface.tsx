import React, { useState, useRef, useEffect } from 'react';
import { Send, Sparkles, Trash2, Eye, EyeOff, Settings } from 'lucide-react';
import { useChatStore } from '@/hooks/useChat';
import { MessageBubble } from './MessageBubble';
import { ActionConfirmationModal } from './ActionConfirmation';
import { useActions } from '@/hooks/useActions';

interface ChatInterfaceProps {
  userId: string;
  isAuthenticated: boolean;
}

export function ChatInterface({ userId, isAuthenticated }: ChatInterfaceProps) {
  const [input, setInput] = useState('');
  const [showRawData, setShowRawData] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  
  const { 
    messages, 
    isLoading, 
    pendingAction,
    sendMessage, 
    clearMessages,
    setUserId 
  } = useChatStore();
  
  const { confirm, cancel, isExecuting } = useActions();

  // Set user ID on mount
  useEffect(() => {
    setUserId(userId);
  }, [userId, setUserId]);

  // Scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Auto-resize textarea
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = 'auto';
      inputRef.current.style.height = `${Math.min(inputRef.current.scrollHeight, 200)}px`;
    }
  }, [input]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading || !isAuthenticated) return;

    const query = input.trim();
    setInput('');
    await sendMessage(query);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  // Example queries
  const exampleQueries = [
    "What meetings do I have tomorrow?",
    "Find emails about the project proposal",
    "Search my Drive for budget reports",
    "Schedule a meeting with team@company.com tomorrow at 2pm",
  ];

  return (
    <div className="flex flex-col h-full">
      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto px-4 py-6 space-y-6">
        {messages.length === 0 ? (
          <WelcomeScreen 
            onSelectQuery={(q) => {
              setInput(q);
              inputRef.current?.focus();
            }}
            exampleQueries={exampleQueries}
            isAuthenticated={isAuthenticated}
          />
        ) : (
          <>
            {messages.map((message) => (
              <MessageBubble 
                key={message.id} 
                message={message} 
                showRawData={showRawData}
              />
            ))}
            <div ref={messagesEndRef} />
          </>
        )}
      </div>

      {/* Input Area */}
      <div className="border-t border-slate-700/50 p-4 bg-slate-900/50 backdrop-blur-xl">
        {/* Toolbar */}
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowRawData(!showRawData)}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg
                         transition-all ${showRawData 
                           ? 'bg-primary-500/20 text-primary-400 border border-primary-500/30' 
                           : 'text-slate-400 hover:text-slate-300 hover:bg-slate-700/50'}`}
              title={showRawData ? 'Hide raw data' : 'Show raw data'}
            >
              {showRawData ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
              Raw Data
            </button>
          </div>
          
          {messages.length > 0 && (
            <button
              onClick={clearMessages}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-slate-400 
                         hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-all"
            >
              <Trash2 className="w-3.5 h-3.5" />
              Clear
            </button>
          )}
        </div>

        {/* Input Form */}
        <form onSubmit={handleSubmit} className="relative">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              isAuthenticated 
                ? "Ask me anything about your calendar, emails, or files..." 
                : "Connect your Google account to get started"
            }
            disabled={!isAuthenticated || isLoading}
            className="input-field pr-14 min-h-[52px] max-h-[200px] resize-none"
            rows={1}
          />
          <button
            type="submit"
            disabled={!input.trim() || isLoading || !isAuthenticated}
            className="absolute right-2 bottom-2 p-2.5 bg-primary-500 text-white rounded-xl
                       hover:bg-primary-600 disabled:opacity-50 disabled:cursor-not-allowed
                       transition-all shadow-lg shadow-primary-500/25"
          >
            <Send className="w-5 h-5" />
          </button>
        </form>

        {/* Keyboard hint */}
        <p className="text-xs text-slate-500 mt-2 text-center">
          Press <kbd className="px-1.5 py-0.5 bg-slate-700/50 rounded text-slate-400">Enter</kbd> to send, 
          <kbd className="px-1.5 py-0.5 bg-slate-700/50 rounded text-slate-400 ml-1">Shift+Enter</kbd> for new line
        </p>
      </div>

      {/* Action Confirmation Modal */}
      {pendingAction && (
        <ActionConfirmationModal
          action={pendingAction}
          onConfirm={confirm}
          onCancel={cancel}
          isExecuting={isExecuting}
        />
      )}
    </div>
  );
}

// Welcome Screen Component
interface WelcomeScreenProps {
  onSelectQuery: (query: string) => void;
  exampleQueries: string[];
  isAuthenticated: boolean;
}

function WelcomeScreen({ onSelectQuery, exampleQueries, isAuthenticated }: WelcomeScreenProps) {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center px-4 py-12">
      <div className="mb-8">
        <div className="w-16 h-16 bg-gradient-to-br from-primary-500 to-accent-purple 
                        rounded-2xl flex items-center justify-center mx-auto mb-4 shadow-xl 
                        shadow-primary-500/25">
          <Sparkles className="w-8 h-8 text-white" />
        </div>
        <h2 className="text-2xl font-bold text-white mb-2">
          Multi-Agent Data Connector
        </h2>
        <p className="text-slate-400 max-w-md">
          Ask questions about your Google Calendar, Gmail, and Drive. 
          I can also help you schedule meetings, send emails, and more.
        </p>
      </div>

      {!isAuthenticated ? (
        <div className="glass-card p-6 max-w-sm">
          <p className="text-slate-300 mb-4">
            Connect your Google account to get started
          </p>
          <p className="text-sm text-slate-500">
            Click "Connect Google" above to authorize access
          </p>
        </div>
      ) : (
        <div className="w-full max-w-lg">
          <p className="text-sm text-slate-500 mb-4">Try asking:</p>
          <div className="grid gap-3">
            {exampleQueries.map((query, idx) => (
              <button
                key={idx}
                onClick={() => onSelectQuery(query)}
                className="glass-card-hover p-4 text-left text-sm text-slate-300 
                           hover:text-white transition-all group"
              >
                <span className="opacity-60 group-hover:opacity-100 transition-opacity">
                  "{query}"
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Feature badges */}
      <div className="flex flex-wrap items-center justify-center gap-3 mt-8">
        <FeatureBadge icon="📅" label="Calendar" />
        <FeatureBadge icon="📧" label="Gmail" />
        <FeatureBadge icon="📁" label="Drive" />
        <FeatureBadge icon="💬" label="Slack" />
      </div>
    </div>
  );
}

function FeatureBadge({ icon, label }: { icon: string; label: string }) {
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 bg-slate-800/50 
                    border border-slate-700/50 rounded-full text-sm text-slate-400">
      <span>{icon}</span>
      <span>{label}</span>
    </div>
  );
}

