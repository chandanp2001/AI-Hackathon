import React, { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { 
  X, 
  Check, 
  AlertTriangle, 
  Clock, 
  ChevronRight,
  Shield,
  Loader2,
  MapPin,
  FileText,
  Timer
} from 'lucide-react';
import { ActionPlanResponse, EditableField } from '@/services/types';
import { useActions, getRiskLevelColor } from '@/hooks/useActions';
import { AgentIndicator } from './AgentBadge';

interface ActionConfirmationProps {
  action: ActionPlanResponse;
  onConfirm: (editedParams?: Record<string, unknown>) => void;
  onCancel: () => void;
  isExecuting?: boolean;
}

// Editable field component
function EditableFieldInput({
  name,
  field,
  value,
  onChange,
}: {
  name: string;
  field: EditableField;
  value: string | number;
  onChange: (name: string, value: string | number) => void;
}) {
  const iconMap: Record<string, React.ReactNode> = {
    location: <MapPin className="w-4 h-4" />,
    description: <FileText className="w-4 h-4" />,
    duration_minutes: <Timer className="w-4 h-4" />,
  };

  const icon = iconMap[name] || null;

  if (field.type === 'textarea') {
    return (
      <div className="space-y-1.5">
        <label className="flex items-center gap-2 text-sm font-medium text-slate-300">
          {icon}
          {field.label}
          {field.required && <span className="text-red-400">*</span>}
        </label>
        <textarea
          value={value as string}
          onChange={(e) => onChange(name, e.target.value)}
          placeholder={field.placeholder}
          rows={3}
          className="w-full px-3 py-2 bg-slate-800/50 border border-slate-600/50 
                     rounded-lg text-sm text-white placeholder-slate-500
                     focus:border-primary-500/50 focus:ring-1 focus:ring-primary-500/20
                     transition-all resize-none"
        />
      </div>
    );
  }

  if (field.type === 'number') {
    return (
      <div className="space-y-1.5">
        <label className="flex items-center gap-2 text-sm font-medium text-slate-300">
          {icon}
          {field.label}
        </label>
        <input
          type="number"
          value={value as number}
          onChange={(e) => onChange(name, parseInt(e.target.value) || 0)}
          min={15}
          max={480}
          step={15}
          className="w-full px-3 py-2 bg-slate-800/50 border border-slate-600/50 
                     rounded-lg text-sm text-white
                     focus:border-primary-500/50 focus:ring-1 focus:ring-primary-500/20
                     transition-all"
        />
      </div>
    );
  }

  // Default: text input
  return (
    <div className="space-y-1.5">
      <label className="flex items-center gap-2 text-sm font-medium text-slate-300">
        {icon}
        {field.label}
        {field.required && <span className="text-red-400">*</span>}
      </label>
      <input
        type="text"
        value={value as string}
        onChange={(e) => onChange(name, e.target.value)}
        placeholder={field.placeholder}
        className="w-full px-3 py-2 bg-slate-800/50 border border-slate-600/50 
                   rounded-lg text-sm text-white placeholder-slate-500
                   focus:border-primary-500/50 focus:ring-1 focus:ring-primary-500/20
                   transition-all"
      />
    </div>
  );
}

export function ActionConfirmationModal({
  action,
  onConfirm,
  onCancel,
  isExecuting = false,
}: ActionConfirmationProps) {
  const riskColorClass = getRiskLevelColor(action.risk_level);
  
  // State for editable fields
  const [editedValues, setEditedValues] = useState<Record<string, string | number>>({});
  
  // Initialize edited values from action.editable_fields
  useEffect(() => {
    if (action.editable_fields) {
      const initial: Record<string, string | number> = {};
      Object.entries(action.editable_fields).forEach(([key, field]) => {
        initial[key] = field.value ?? '';
      });
      setEditedValues(initial);
    }
  }, [action.editable_fields]);
  
  const handleFieldChange = (name: string, value: string | number) => {
    setEditedValues((prev) => ({ ...prev, [name]: value }));
  };
  
  const handleConfirm = () => {
    // Only include non-empty edited values
    const params: Record<string, unknown> = {};
    Object.entries(editedValues).forEach(([key, value]) => {
      if (value !== '' && value !== null && value !== undefined) {
        params[key] = value;
      }
    });
    onConfirm(Object.keys(params).length > 0 ? params : undefined);
  };

  // Filter editable fields to show (exclude title and non-editable)
  const editableFieldEntries = Object.entries(action.editable_fields || {})
    .filter(([key, field]) => field.editable && key !== 'title');

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div 
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onCancel}
      />
      
      {/* Modal */}
      <div className="relative w-full max-w-lg glass-card p-6 animate-slide-up max-h-[90vh] overflow-y-auto">
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
            <ReactMarkdown>{action.preview}</ReactMarkdown>
          </div>
        </div>

        {/* Editable Fields */}
        {editableFieldEntries.length > 0 && (
          <div className="mb-4 space-y-4">
            <h4 className="text-sm font-medium text-slate-300 flex items-center gap-2">
              <FileText className="w-4 h-4" />
              Additional Details (Optional)
            </h4>
            <div className="space-y-3 bg-slate-800/30 rounded-xl p-4 border border-slate-700/30">
              {editableFieldEntries.map(([key, field]) => (
                <EditableFieldInput
                  key={key}
                  name={key}
                  field={field}
                  value={editedValues[key] ?? field.value ?? ''}
                  onChange={handleFieldChange}
                />
              ))}
            </div>
          </div>
        )}

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

        {/* Google Meet Note */}
        <div className="flex items-center gap-2 p-3 bg-blue-500/10 border border-blue-500/30 
                        rounded-xl mb-4 text-sm text-blue-200">
          <span>📹</span>
          <span>A Google Meet link will be automatically added to this meeting.</span>
        </div>

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
            onClick={handleConfirm}
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
  
  // State for editable fields
  const [editedValues, setEditedValues] = useState<Record<string, string | number>>({});
  const [showEditFields, setShowEditFields] = useState(false);
  
  // Initialize edited values from action.editable_fields
  useEffect(() => {
    if (action.editable_fields) {
      const initial: Record<string, string | number> = {};
      Object.entries(action.editable_fields).forEach(([key, field]) => {
        initial[key] = field.value ?? '';
      });
      setEditedValues(initial);
    }
  }, [action.editable_fields]);
  
  const handleFieldChange = (name: string, value: string | number) => {
    setEditedValues((prev) => ({ ...prev, [name]: value }));
  };
  
  const handleConfirm = () => {
    // Only include non-empty edited values
    const params: Record<string, unknown> = {};
    Object.entries(editedValues).forEach(([key, value]) => {
      if (value !== '' && value !== null && value !== undefined) {
        params[key] = value;
      }
    });
    confirm(Object.keys(params).length > 0 ? params : undefined);
  };

  // Filter editable fields to show (exclude title and non-editable)
  const editableFieldEntries = Object.entries(action.editable_fields || {})
    .filter(([key, field]) => field.editable && key !== 'title');

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

      {/* Editable Fields Toggle */}
      {editableFieldEntries.length > 0 && (
        <div className="space-y-3">
          <button
            onClick={() => setShowEditFields(!showEditFields)}
            className="text-sm text-primary-400 hover:text-primary-300 flex items-center gap-1"
          >
            <FileText className="w-4 h-4" />
            {showEditFields ? 'Hide' : 'Add'} details (location, description, duration)
          </button>
          
          {showEditFields && (
            <div className="space-y-3 bg-slate-800/30 rounded-xl p-4 border border-slate-700/30">
              {editableFieldEntries.map(([key, field]) => (
                <EditableFieldInput
                  key={key}
                  name={key}
                  field={field}
                  value={editedValues[key] ?? field.value ?? ''}
                  onChange={handleFieldChange}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Google Meet Note */}
      <div className="flex items-center gap-2 text-xs text-blue-300">
        <span>📹</span>
        <span>Google Meet link will be added automatically</span>
      </div>

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
          onClick={handleConfirm}
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
