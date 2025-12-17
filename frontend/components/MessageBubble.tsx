import React from 'react';
import ReactMarkdown from 'react-markdown';
import { format, parseISO, isValid } from 'date-fns';
import { User, Bot, Info, Check, X, ExternalLink } from 'lucide-react';
import { Message, ActionExecutionResponse } from '@/services/types';
import { AgentBadges } from './AgentBadge';
import { DataCards } from './DataCards';
import { LoadingIndicator } from './LoadingIndicator';
import { InlineActionConfirm } from './ActionConfirmation';

/**
 * Parse timestamp from various formats (string ISO, Date object)
 * and return a valid Date object for formatting.
 */
function parseTimestamp(timestamp: Date | string): Date {
  if (timestamp instanceof Date && isValid(timestamp)) {
    return timestamp;
  }
  if (typeof timestamp === 'string') {
    // Try parsing as ISO string
    const parsed = parseISO(timestamp);
    if (isValid(parsed)) {
      return parsed;
    }
    // Fallback to Date constructor
    const fallback = new Date(timestamp);
    if (isValid(fallback)) {
      return fallback;
    }
  }
  // Last resort: return current time
  return new Date();
}

interface MessageBubbleProps {
  message: Message;
  showRawData?: boolean;
}

export function MessageBubble({ message, showRawData = false }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const isSystem = message.role === 'system';
  const isLoading = message.metadata?.isLoading;

  return (
    <div 
      className={`flex gap-3 animate-fade-in ${isUser ? 'flex-row-reverse' : ''}`}
    >
      {/* Avatar */}
      <div className={`flex-shrink-0 w-8 h-8 rounded-xl flex items-center justify-center
                       ${isUser ? 'bg-primary-500/20 text-primary-400' : 
                         isSystem ? 'bg-yellow-500/20 text-yellow-400' : 
                         'bg-slate-700 text-slate-300'}`}
      >
        {isUser ? (
          <User className="w-4 h-4" />
        ) : isSystem ? (
          <Info className="w-4 h-4" />
        ) : (
          <Bot className="w-4 h-4" />
        )}
      </div>

      {/* Message Content */}
      <div className={`flex-1 max-w-[85%] ${isUser ? 'text-right' : ''}`}>
        <div
          className={`inline-block rounded-2xl px-4 py-3 
                      ${isUser ? 'message-user' : 
                        isSystem ? 'message-system' : 
                        'message-assistant'}`}
        >
          {isLoading ? (
            <LoadingIndicator />
          ) : (
            <>
              {/* Main content */}
              <div className="markdown-content">
                <ReactMarkdown
                  components={{
                    a: ({ href, children }) => (
                      <a 
                        href={href} 
                        target="_blank" 
                        rel="noopener noreferrer"
                        className="text-primary-400 hover:text-primary-300 underline underline-offset-2"
                      >
                        {children}
                      </a>
                    )
                  }}
                >
                  {message.content}
                </ReactMarkdown>
              </div>

              {/* Action plan confirmation */}
              {message.metadata?.action_plan && !message.metadata?.execution_result && (
                <InlineActionConfirm action={message.metadata.action_plan} />
              )}

              {/* Execution result */}
              {message.metadata?.execution_result && (
                <ExecutionResultDisplay result={message.metadata.execution_result} />
              )}

              {/* Clarification display for actions */}
              {message.metadata?.clarification && message.metadata.clarification.missing_parameters && (
                <ClarificationDisplay 
                  missingParams={message.metadata.clarification.missing_parameters} 
                />
              )}

              {/* Query clarification display (suggested questions) */}
              {message.metadata?.needs_clarification && (
                <QueryClarificationDisplay 
                  reason={message.metadata.reason}
                  contextNote={message.metadata.context_note}
                  suggestedQuestions={message.metadata.suggested_questions || []}
                  likelySources={message.metadata.likely_sources || []}
                  clarityScore={message.metadata.clarity_score}
                />
              )}

              {/* Agent badges */}
              {message.metadata?.agents_triggered && (
                <AgentBadges 
                  agents={message.metadata.agents_triggered} 
                  showDetails={true}
                />
              )}

              {/* Raw data cards */}
              {showRawData && message.metadata?.raw_data && (
                <DataCards rawData={message.metadata.raw_data} />
              )}
            </>
          )}
        </div>

        {/* Timestamp */}
        <div className={`text-xs text-slate-500 mt-1 ${isUser ? 'text-right' : ''}`}>
          {format(parseTimestamp(message.timestamp), 'h:mm a')}
        </div>
      </div>
    </div>
  );
}

// Execution Result Display
interface ExecutionResultDisplayProps {
  result: ActionExecutionResponse;
}

function ExecutionResultDisplay({ result }: ExecutionResultDisplayProps) {
  return (
    <div className="mt-4 space-y-3">
      {/* Status */}
      <div className={`flex items-center gap-2 text-sm font-medium
                       ${result.success ? 'text-green-400' : 'text-red-400'}`}>
        {result.success ? (
          <Check className="w-4 h-4" />
        ) : (
          <X className="w-4 h-4" />
        )}
        <span>{result.success ? 'Completed successfully' : 'Action failed'}</span>
      </div>

      {/* Links */}
      {Object.keys(result.links).length > 0 && (
        <div className="flex flex-wrap gap-2">
          {Object.entries(result.links).map(([key, url]) => (
            <a
              key={key}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm
                         bg-primary-500/20 text-primary-400 rounded-lg
                         hover:bg-primary-500/30 transition-all"
            >
              <ExternalLink className="w-3.5 h-3.5" />
              {formatLinkLabel(key)}
            </a>
          ))}
        </div>
      )}

      {/* Step results */}
      {result.step_results.length > 1 && (
        <div className="space-y-1 text-xs text-slate-400">
          {result.step_results.map((step) => (
            <div key={step.step_number} className="flex items-center gap-2">
              {step.success ? (
                <Check className="w-3 h-3 text-green-400" />
              ) : (
                <X className="w-3 h-3 text-red-400" />
              )}
              <span>Step {step.step_number}</span>
              {step.error_message && (
                <span className="text-red-400">- {step.error_message}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Rollback option */}
      {result.rollback_available && (
        <p className="text-xs text-slate-500">
          This action can be undone if needed.
        </p>
      )}
    </div>
  );
}

function formatLinkLabel(key: string): string {
  const labels: Record<string, string> = {
    html_link: 'Open',
    meeting_link: 'Join Meeting',
    web_view_link: 'View File',
  };
  return labels[key] || key.replace(/_/g, ' ');
}

// Clarification Display
interface ClarificationDisplayProps {
  missingParams: string[];
}

function ClarificationDisplay({ missingParams }: ClarificationDisplayProps) {
  if (!missingParams.length) return null;

  return (
    <div className="mt-3 text-sm">
      <p className="text-slate-400 mb-2">Missing information:</p>
      <ul className="list-disc list-inside text-slate-300 space-y-1">
        {missingParams.map((param, idx) => (
          <li key={idx}>{param}</li>
        ))}
      </ul>
    </div>
  );
}

// Query Clarification Display (for ambiguous queries)
interface QueryClarificationDisplayProps {
  reason?: string;
  contextNote?: string;
  suggestedQuestions: string[];
  likelySources?: string[];
  clarityScore?: number;
}

function QueryClarificationDisplay({ 
  reason, 
  contextNote,
  suggestedQuestions, 
  likelySources,
  clarityScore 
}: QueryClarificationDisplayProps) {
  if (!suggestedQuestions.length) return null;

  const sourceLabels: Record<string, string> = {
    calendar: '📅 Calendar',
    gmail: '📧 Gmail',
    drive: '📁 Drive',
    slack: '💬 Slack',
    devrev: '🎫 DevRev',
  };

  return (
    <div className="mt-4 p-4 bg-yellow-500/10 border border-yellow-500/30 rounded-xl space-y-3">
      {reason && (
        <p className="text-sm text-yellow-400/90">{reason}</p>
      )}
      
      <div>
        <p className="text-sm text-slate-300 mb-2">
          {contextNote || "To help you better, could you clarify:"}
        </p>
        <ul className="space-y-2">
          {suggestedQuestions.map((question, idx) => (
            <li 
              key={idx} 
              className="text-sm text-slate-400 pl-4 border-l-2 border-yellow-500/50"
            >
              {question}
            </li>
          ))}
        </ul>
      </div>

      {likelySources && likelySources.length > 0 && (
        <div className="flex flex-wrap gap-2 pt-2">
          <span className="text-xs text-slate-500">Possible sources:</span>
          {likelySources.map((source) => (
            <span 
              key={source}
              className="text-xs px-2 py-0.5 bg-slate-700/50 text-slate-400 rounded-full"
            >
              {sourceLabels[source] || source}
            </span>
          ))}
        </div>
      )}
      
      {clarityScore !== undefined && clarityScore < 0.5 && (
        <p className="text-xs text-slate-500 pt-1">
          💡 Tip: Try being more specific about what you're looking for or which data source to search.
        </p>
      )}
    </div>
  );
}

