import { useCallback } from 'react';
import { useChatStore } from './useChat';
import { ActionPlanResponse, ActionExecutionResponse } from '@/services/types';

interface UseActionsReturn {
  pendingAction: ActionPlanResponse | null;
  isExecuting: boolean;
  confirm: () => Promise<void>;
  cancel: () => Promise<void>;
  hasPendingAction: boolean;
}

export function useActions(): UseActionsReturn {
  const { 
    pendingAction, 
    isLoading: isExecuting, 
    confirmAction, 
    cancelPendingAction 
  } = useChatStore();

  const confirm = useCallback(async () => {
    await confirmAction();
  }, [confirmAction]);

  const cancel = useCallback(async () => {
    await cancelPendingAction();
  }, [cancelPendingAction]);

  return {
    pendingAction,
    isExecuting,
    confirm,
    cancel,
    hasPendingAction: pendingAction !== null,
  };
}

// Risk level utilities
export function getRiskLevelColor(level: 'low' | 'medium' | 'high'): string {
  switch (level) {
    case 'low':
      return 'text-green-400 bg-green-400/10 border-green-400/30';
    case 'medium':
      return 'text-yellow-400 bg-yellow-400/10 border-yellow-400/30';
    case 'high':
      return 'text-red-400 bg-red-400/10 border-red-400/30';
    default:
      return 'text-gray-400 bg-gray-400/10 border-gray-400/30';
  }
}

export function getRiskLevelIcon(level: 'low' | 'medium' | 'high'): string {
  switch (level) {
    case 'low':
      return '✓';
    case 'medium':
      return '⚠';
    case 'high':
      return '⚠️';
    default:
      return '•';
  }
}

