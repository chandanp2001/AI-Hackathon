import React, { useState, useEffect, useCallback } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { useSessions } from '@/hooks/useSessions';
import { useChatStore } from '@/hooks/useChat';
import { AuthButton } from '@/components/AuthButton';
import { ChatInterface } from '@/components/ChatInterface';
import { SessionSidebar } from '@/components/SessionSidebar';
import { FullPageLoader } from '@/components/LoadingIndicator';
import { 
  Sparkles, 
  Menu, 
  X, 
  Sun, 
  Moon,
  ExternalLink,
  Activity,
  PanelLeftClose,
  PanelLeft
} from 'lucide-react';
import { checkHealth } from '@/services/api';

// Default user ID - in production, this would come from authentication
const DEFAULT_USER_ID = 'chandan.poonacha';

export default function Home() {
  const [userId] = useState(DEFAULT_USER_ID);
  const [showSidebar, setShowSidebar] = useState(true);
  const [isBackendHealthy, setIsBackendHealthy] = useState<boolean | null>(null);
  const [darkMode, setDarkMode] = useState(true);
  
  const { isAuthenticated, isLoading: authLoading } = useAuth({ userId });
  const { currentSession, selectSession, fetchSessions } = useSessions();
  const { setSessionId, loadSessionMessages, clearMessages } = useChatStore();

  // Check backend health on mount
  useEffect(() => {
    const checkBackendHealth = async () => {
      try {
        await checkHealth();
        setIsBackendHealthy(true);
      } catch {
        setIsBackendHealthy(false);
      }
    };
    checkBackendHealth();
  }, []);

  // Handle dark mode toggle
  useEffect(() => {
    if (darkMode) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  }, [darkMode]);

  // Handle session changes - load messages when session is selected
  const handleSessionChange = useCallback((sessionId: string | null) => {
    setSessionId(sessionId);
    if (sessionId && currentSession?.session_id === sessionId) {
      loadSessionMessages(currentSession.messages);
    } else if (!sessionId) {
      clearMessages();
    }
  }, [setSessionId, loadSessionMessages, clearMessages, currentSession]);

  // Load messages when currentSession changes
  useEffect(() => {
    if (currentSession) {
      loadSessionMessages(currentSession.messages);
    }
  }, [currentSession, loadSessionMessages]);

  // Refresh sessions when auth status changes
  useEffect(() => {
    if (isAuthenticated) {
      fetchSessions(userId);
    }
  }, [isAuthenticated, userId, fetchSessions]);

  if (authLoading) {
    return <FullPageLoader />;
  }

  return (
    <div className="min-h-screen flex flex-col bg-slate-950">
      {/* Header */}
      <header className="border-b border-slate-700/50 bg-slate-900/80 backdrop-blur-xl sticky top-0 z-40">
        <div className="px-4 h-16 flex items-center justify-between">
          {/* Left side - Logo & Title */}
          <div className="flex items-center gap-4">
            <button
              onClick={() => setShowSidebar(!showSidebar)}
              className="p-2 text-slate-400 hover:text-white hover:bg-slate-700/50 
                         rounded-lg transition-all"
              title={showSidebar ? 'Hide sidebar' : 'Show sidebar'}
            >
              {showSidebar ? <PanelLeftClose className="w-5 h-5" /> : <PanelLeft className="w-5 h-5" />}
            </button>
            
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 bg-gradient-to-br from-primary-500 to-accent-purple 
                              rounded-xl flex items-center justify-center shadow-lg shadow-primary-500/25">
                <Sparkles className="w-5 h-5 text-white" />
              </div>
              <div className="hidden sm:block">
                <h1 className="font-semibold text-white">Multi-Agent Connector</h1>
                <p className="text-xs text-slate-400">AI-powered data assistant</p>
              </div>
            </div>
          </div>

          {/* Right side - Actions */}
          <div className="flex items-center gap-3">
            {/* Backend Status Indicator */}
            <div 
              className={`hidden sm:flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs
                         ${isBackendHealthy === true 
                           ? 'bg-green-500/20 text-green-400' 
                           : isBackendHealthy === false 
                           ? 'bg-red-500/20 text-red-400'
                           : 'bg-slate-700/50 text-slate-400'}`}
              title={isBackendHealthy ? 'Backend connected' : 'Backend unavailable'}
            >
              <Activity className="w-3 h-3" />
              <span>{isBackendHealthy ? 'Online' : isBackendHealthy === false ? 'Offline' : '...'}</span>
            </div>

            {/* Auth Button */}
            <AuthButton userId={userId} />

            {/* Settings */}
            <button
              onClick={() => setDarkMode(!darkMode)}
              className="p-2 text-slate-400 hover:text-white hover:bg-slate-700/50 
                         rounded-lg transition-all"
              title="Toggle theme"
            >
              {darkMode ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
            </button>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 flex overflow-hidden">
        {/* Session Sidebar */}
        <aside 
          className={`bg-slate-900/95 border-r border-slate-700/50 
                      transition-all duration-300 flex-shrink-0 overflow-hidden
                      ${showSidebar ? 'w-72' : 'w-0'}`}
        >
          <div className={`w-72 h-full ${showSidebar ? 'opacity-100' : 'opacity-0'} transition-opacity`}>
            <SessionSidebar 
              userId={userId}
              isAuthenticated={isAuthenticated}
              onSessionChange={handleSessionChange}
            />
          </div>
        </aside>

        {/* Chat Area */}
        <div className="flex-1 flex flex-col min-w-0">
          {!isBackendHealthy && isBackendHealthy !== null && (
            <div className="bg-red-500/10 border-b border-red-500/30 px-4 py-3 text-center flex-shrink-0">
              <p className="text-sm text-red-400">
                Backend server is not responding. Please ensure the API is running at{' '}
                <code className="px-1.5 py-0.5 bg-red-500/20 rounded">localhost:8000</code>
              </p>
            </div>
          )}
          
          <div className="flex-1 overflow-hidden">
            <ChatInterface 
              userId={userId} 
              isAuthenticated={isAuthenticated} 
            />
          </div>
        </div>

        {/* Right Sidebar - Quick Links (collapsible on smaller screens) */}
        <aside className="hidden xl:block w-64 border-l border-slate-700/50 bg-slate-900/50 overflow-y-auto flex-shrink-0">
          <div className="p-4 space-y-6">
            {/* Quick Links */}
            <div>
              <h3 className="text-xs uppercase tracking-wider text-slate-500 font-medium mb-3">
                Quick Links
              </h3>
              <div className="space-y-1">
                <SidebarLink 
                  href="https://calendar.google.com" 
                  icon="📅" 
                  label="Google Calendar" 
                />
                <SidebarLink 
                  href="https://mail.google.com" 
                  icon="📧" 
                  label="Gmail" 
                />
                <SidebarLink 
                  href="https://drive.google.com" 
                  icon="📁" 
                  label="Google Drive" 
                />
                <SidebarLink 
                  href="https://slack.com" 
                  icon="💬" 
                  label="Slack" 
                />
              </div>
            </div>

            {/* Capabilities */}
            <div>
              <h3 className="text-xs uppercase tracking-wider text-slate-500 font-medium mb-3">
                Capabilities
              </h3>
              <div className="space-y-2 text-sm text-slate-400">
                <CapabilityItem 
                  title="Read Data"
                  description="Search calendar, emails, and files"
                />
                <CapabilityItem 
                  title="Take Actions"
                  description="Schedule meetings, send emails, create docs"
                />
                <CapabilityItem 
                  title="Smart Routing"
                  description="AI determines relevant data sources"
                />
              </div>
            </div>

            {/* Help */}
            <div className="pt-4 border-t border-slate-700/50">
              <h3 className="text-xs uppercase tracking-wider text-slate-500 font-medium mb-3">
                Example Queries
              </h3>
              <div className="space-y-2 text-xs text-slate-500">
                <p>• "What meetings do I have tomorrow?"</p>
                <p>• "Find emails about the budget report"</p>
                <p>• "Schedule a meeting with John at 2pm"</p>
                <p>• "Search my drive for project docs"</p>
                <p>• "What did the team discuss in #general?"</p>
              </div>
            </div>
          </div>
        </aside>
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-700/50 bg-slate-900/50 py-3 px-4 flex-shrink-0">
        <div className="flex items-center justify-between text-xs text-slate-500">
          <span>Multi-Agent Data Connector v1.0</span>
          <div className="flex items-center gap-4">
            <a 
              href="/api/health" 
              target="_blank"
              className="hover:text-slate-300 transition-colors"
            >
              API Status
            </a>
            <a 
              href="/api" 
              target="_blank"
              className="hover:text-slate-300 transition-colors flex items-center gap-1"
            >
              API Docs
              <ExternalLink className="w-3 h-3" />
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}

// Sidebar Components
function SidebarLink({ href, icon, label }: { href: string; icon: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-3 px-3 py-2 text-sm text-slate-400 
                 hover:text-white hover:bg-slate-700/50 rounded-lg transition-all group"
    >
      <span>{icon}</span>
      <span>{label}</span>
      <ExternalLink className="w-3 h-3 opacity-0 group-hover:opacity-100 ml-auto transition-opacity" />
    </a>
  );
}

function CapabilityItem({ title, description }: { title: string; description: string }) {
  return (
    <div className="glass-card p-3">
      <h4 className="text-white font-medium text-sm mb-1">{title}</h4>
      <p className="text-xs">{description}</p>
    </div>
  );
}
