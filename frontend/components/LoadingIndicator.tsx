import React from 'react';

interface LoadingIndicatorProps {
  text?: string;
}

export function LoadingIndicator({ text = 'Thinking' }: LoadingIndicatorProps) {
  return (
    <div className="flex items-center gap-3">
      <div className="typing-indicator">
        <span></span>
        <span></span>
        <span></span>
      </div>
      <span className="text-sm text-slate-400">{text}</span>
    </div>
  );
}

export function FullPageLoader() {
  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="text-center space-y-4">
        <div className="typing-indicator mx-auto w-fit">
          <span></span>
          <span></span>
          <span></span>
        </div>
        <p className="text-slate-400">Loading...</p>
      </div>
    </div>
  );
}

export function InlineLoader({ className = '' }: { className?: string }) {
  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <div className="w-4 h-4 border-2 border-primary-500/30 border-t-primary-500 
                      rounded-full animate-spin" />
    </div>
  );
}

