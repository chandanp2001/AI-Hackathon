import React from 'react';
import { useAuth } from '@/hooks/useAuth';
import { 
  LogIn, 
  LogOut, 
  Check, 
  AlertCircle, 
  Loader2,
  ChevronDown 
} from 'lucide-react';

interface AuthButtonProps {
  userId: string;
}

export function AuthButton({ userId }: AuthButtonProps) {
  const { 
    isAuthenticated, 
    isLoading, 
    error, 
    scopes, 
    connect, 
    disconnect 
  } = useAuth({ userId });
  
  const [showDropdown, setShowDropdown] = React.useState(false);

  if (isLoading) {
    return (
      <button 
        disabled 
        className="flex items-center gap-2 px-4 py-2 bg-slate-700/50 text-slate-400 rounded-xl"
      >
        <Loader2 className="w-4 h-4 animate-spin" />
        <span>Checking...</span>
      </button>
    );
  }

  if (isAuthenticated) {
    return (
      <div className="relative">
        <button
          onClick={() => setShowDropdown(!showDropdown)}
          className="flex items-center gap-2 px-4 py-2 bg-green-500/20 text-green-400 
                     border border-green-500/30 rounded-xl hover:bg-green-500/30 transition-all"
        >
          <Check className="w-4 h-4" />
          <span>Connected</span>
          <ChevronDown className="w-4 h-4" />
        </button>
        
        {showDropdown && (
          <>
            <div 
              className="fixed inset-0 z-10" 
              onClick={() => setShowDropdown(false)}
            />
            <div className="absolute right-0 mt-2 w-64 glass-card p-3 z-20 animate-fade-in">
              <div className="text-sm text-slate-400 mb-3">
                <p className="font-medium text-white mb-1">Google Account Connected</p>
                <p className="text-xs">
                  {scopes.length} scopes granted
                </p>
              </div>
              
              <div className="space-y-2 mb-3">
                {scopes.slice(0, 3).map((scope, idx) => (
                  <div 
                    key={idx}
                    className="text-xs text-slate-500 truncate"
                  >
                    {scope.split('/').pop()}
                  </div>
                ))}
                {scopes.length > 3 && (
                  <div className="text-xs text-slate-500">
                    +{scopes.length - 3} more
                  </div>
                )}
              </div>
              
              <button
                onClick={() => {
                  disconnect();
                  setShowDropdown(false);
                }}
                className="w-full flex items-center justify-center gap-2 px-3 py-2 
                           bg-red-500/20 text-red-400 border border-red-500/30 rounded-lg
                           hover:bg-red-500/30 transition-all text-sm"
              >
                <LogOut className="w-4 h-4" />
                Disconnect
              </button>
            </div>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col items-end gap-2">
      <button
        onClick={connect}
        className="flex items-center gap-2 px-4 py-2 bg-primary-500 text-white 
                   rounded-xl hover:bg-primary-600 transition-all shadow-lg shadow-primary-500/25"
      >
        <LogIn className="w-4 h-4" />
        <span>Connect Google</span>
      </button>
      
      {error && (
        <div className="flex items-center gap-1 text-red-400 text-xs">
          <AlertCircle className="w-3 h-3" />
          <span>{error}</span>
        </div>
      )}
    </div>
  );
}

// Google icon component
function GoogleIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
    </svg>
  );
}

