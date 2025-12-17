import React from 'react';
import { Calendar, Mail, FileText, MessageSquare } from 'lucide-react';
import { AgentContribution } from '@/services/types';

interface AgentBadgeProps {
  agent: AgentContribution;
  showDetails?: boolean;
}

const agentConfig = {
  calendar: {
    icon: Calendar,
    label: 'Calendar',
    className: 'agent-calendar',
  },
  gmail: {
    icon: Mail,
    label: 'Gmail',
    className: 'agent-gmail',
  },
  drive: {
    icon: FileText,
    label: 'Drive',
    className: 'agent-drive',
  },
  slack: {
    icon: MessageSquare,
    label: 'Slack',
    className: 'agent-slack',
  },
};

export function AgentBadge({ agent, showDetails = false }: AgentBadgeProps) {
  const config = agentConfig[agent.agent_name as keyof typeof agentConfig] || {
    icon: FileText,
    label: agent.agent_name,
    className: 'bg-slate-500/20 text-slate-400 border-slate-500/30',
  };
  
  const Icon = config.icon;
  const scorePercent = Math.round(agent.relevance_score * 100);

  return (
    <div 
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs
                  font-medium transition-all ${config.className}`}
      title={agent.justification}
    >
      <Icon className="w-3.5 h-3.5" />
      <span>{config.label}</span>
      {showDetails && (
        <>
          <span className="opacity-60">•</span>
          <span className="opacity-80">{scorePercent}%</span>
          {agent.data_count > 0 && (
            <>
              <span className="opacity-60">•</span>
              <span className="opacity-80">{agent.data_count} items</span>
            </>
          )}
        </>
      )}
    </div>
  );
}

interface AgentBadgesProps {
  agents: AgentContribution[];
  showDetails?: boolean;
}

export function AgentBadges({ agents, showDetails = false }: AgentBadgesProps) {
  if (!agents || agents.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2 mt-3">
      {agents.map((agent) => (
        <AgentBadge 
          key={agent.agent_name} 
          agent={agent} 
          showDetails={showDetails}
        />
      ))}
    </div>
  );
}

// Simple inline agent indicator
export function AgentIndicator({ agentName }: { agentName: string }) {
  const config = agentConfig[agentName as keyof typeof agentConfig];
  if (!config) return null;
  
  const Icon = config.icon;
  
  return (
    <span 
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs ${config.className}`}
    >
      <Icon className="w-3 h-3" />
    </span>
  );
}

