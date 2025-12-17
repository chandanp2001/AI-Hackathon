import React from 'react';
import ReactMarkdown from 'react-markdown';
import { 
  X, 
  Check, 
  AlertTriangle, 
  Clock, 
  ChevronRight,
  Shield,
  Loader2 
} from 'lucide-react';
import { ActionPlanResponse } from '@/services/types';
import { useActions, getRiskLevelColor } from '@/hooks/useActions';
import { AgentIndicator } from './AgentBadge';

interface ActionConfirmationProps {
  action: ActionPlanResponse;
  onConfirm: () => void;
  onCancel: () => void;
  isExecuting?: boolean;
}

export function ActionConfirmationModal({
  action,
  onConfirm,
  onCancel,
  isExecuting = false,
}: ActionConfirmationProps) {
  const riskColorClass = getRiskLevelColor(action.risk_level);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div 
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onCancel}
      />
      
      {/* Modal */}
      <div className="relative w-full max-w-lg glass-card p-6 animate-slide-up">
        {/* Header */}
        <div className="flex items-start justify-between mb-4">
          <div>
            <h3 className="text-lg font-semibold text-white mb-1">
              Confirm Action
            </h3>
            <p className="text-sm text-slate-400">{action.summary}</p>
          </div>
          <button
            onClick={onCancel}
            disabled={isExecuting}
            className="p-2 text-slate-400 hover:text-white hover:bg-slate-700/50 
                       rounded-lg transition-all disabled:opacity-50"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Preview */}
        <div className="bg-slate-800/50 rounded-xl p-4 mb-4 border border-slate-700/50">
          <div className="markdown-content text-sm">
            <ReactMarkdown
              components={{
                a: ({ node, ...props }) => (
                  <a
                    {...props}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-primary-400 hover:text-primary-300 underline"
                  />
                ),
              }}
            >
              {action.preview}
            </ReactMarkdown>
          </div>
        </div>

        {/* Steps */}
        {action.steps.length > 0 && (
          <div className="mb-4">
            <h4 className="text-sm font-medium text-slate-300 mb-2">Steps:</h4>
            <div className="space-y-2">
              {action.steps.map((step) => (
                <div 
                  key={step.step_number}
                  className="flex items-center gap-3 text-sm"
                >
                  <div className="flex items-center justify-center w-6 h-6 
                                  bg-primary-500/20 text-primary-400 rounded-full text-xs font-medium">
                    {step.step_number}
                  </div>
                  <span className="text-slate-300 flex-1">{step.description}</span>
                  <AgentIndicator agentName={step.agent} />
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Metadata Row */}
        <div className="flex items-center gap-4 mb-6 text-sm">
          {/* Risk Level */}
          <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border ${riskColorClass}`}>
            <Shield className="w-3.5 h-3.5" />
            <span className="capitalize">{action.risk_level} risk</span>
          </div>

          {/* Duration */}
          <div className="flex items-center gap-1.5 text-slate-400">
            <Clock className="w-3.5 h-3.5" />
            <span>{action.estimated_duration}</span>
          </div>
        </div>

        {/* Warning for high risk */}
        {action.risk_level === 'high' && (
          <div className="flex items-start gap-3 p-3 bg-red-500/10 border border-red-500/30 
                          rounded-xl mb-4 text-sm">
            <AlertTriangle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
            <p className="text-red-200">
              This action cannot be undone. Please review carefully before confirming.
            </p>
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center gap-3">
          <button
            onClick={onCancel}
            disabled={isExecuting}
            className="flex-1 btn-secondary disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={isExecuting}
            className="flex-1 btn-primary flex items-center justify-center gap-2"
          >
            {isExecuting ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Executing...
              </>
            ) : (
              <>
                <Check className="w-4 h-4" />
                Confirm
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// Inline action confirmation (shown in message)
interface InlineActionConfirmProps {
  action: ActionPlanResponse;
}

export function InlineActionConfirm({ action }: InlineActionConfirmProps) {
  const { confirm, cancel, isExecuting } = useActions();
  const riskColorClass = getRiskLevelColor(action.risk_level);

  return (
    <div className="mt-4 space-y-4">
      {/* Metadata */}
      <div className="flex items-center gap-3 text-sm">
        <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border ${riskColorClass}`}>
          <Shield className="w-3.5 h-3.5" />
          <span className="capitalize">{action.risk_level} risk</span>
        </div>
        <div className="flex items-center gap-1.5 text-slate-400">
          <Clock className="w-3.5 h-3.5" />
          <span>{action.estimated_duration}</span>
        </div>
      </div>

      {/* Steps preview */}
      {action.steps.length > 0 && (
        <div className="flex items-center gap-2 text-xs text-slate-400">
          {action.steps.map((step, idx) => (
            <React.Fragment key={step.step_number}>
              <span className="flex items-center gap-1">
                <AgentIndicator agentName={step.agent} />
                {step.action}
              </span>
              {idx < action.steps.length - 1 && (
                <ChevronRight className="w-3 h-3" />
              )}
            </React.Fragment>
          ))}
        </div>
      )}

      {/* Action buttons */}
      <div className="flex items-center gap-3">
        <button
          onClick={cancel}
          disabled={isExecuting}
          className="px-4 py-2 text-sm btn-secondary disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={confirm}
          disabled={isExecuting}
          className="px-4 py-2 text-sm btn-primary flex items-center gap-2 disabled:opacity-50"
        >
          {isExecuting ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Executing...
            </>
          ) : (
            <>
              <Check className="w-4 h-4" />
              Execute
            </>
          )}
        </button>
      </div>
    </div>
  );
}

