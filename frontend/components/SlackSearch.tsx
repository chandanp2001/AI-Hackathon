import React, { useState, useEffect } from 'react';
import { Search, MessageSquare, Hash, Clock, ChevronRight, Loader2, X } from 'lucide-react';
import { searchSlack, getSlackChannels } from '@/services/api';
import { 
  SlackSearchResponse, 
  SlackChannel, 
  SlackChannelSummary,
  SlackFollowUpSuggestion 
} from '@/services/types';

interface SlackSearchProps {
  userId: string;
  isOpen: boolean;
  onClose: () => void;
}

export function SlackSearch({ userId, isOpen, onClose }: SlackSearchProps) {
  const [query, setQuery] = useState('');
  const [selectedChannel, setSelectedChannel] = useState<string | null>(null);
  const [channels, setChannels] = useState<SlackChannel[]>([]);
  const [results, setResults] = useState<SlackSearchResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingChannels, setIsLoadingChannels] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load channels on mount
  useEffect(() => {
    if (isOpen) {
      loadChannels();
    }
  }, [isOpen]);

  const loadChannels = async () => {
    setIsLoadingChannels(true);
    try {
      const response = await getSlackChannels(50, true);
      setChannels(response.channels);
    } catch (err) {
      console.error('Failed to load channels:', err);
    } finally {
      setIsLoadingChannels(false);
    }
  };

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;

    setIsLoading(true);
    setError(null);

    try {
      const response = await searchSlack({
        query: query.trim(),
        user_id: userId,
        channel: selectedChannel || undefined,
        limit: 20,
        max_channels: 10,
      });
      setResults(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Search failed');
    } finally {
      setIsLoading(false);
    }
  };

  const handleSuggestionClick = (suggestion: SlackFollowUpSuggestion) => {
    if (suggestion.type === 'channel') {
      setSelectedChannel(suggestion.value);
    } else if (suggestion.type === 'keyword') {
      setQuery((prev) => `${prev} ${suggestion.value}`.trim());
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-20 px-4">
      {/* Backdrop */}
      <div 
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />
      
      {/* Modal */}
      <div className="relative w-full max-w-2xl glass-card overflow-hidden animate-slide-up">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-slate-700/50">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-purple-500/20 rounded-lg">
              <MessageSquare className="w-5 h-5 text-purple-400" />
            </div>
            <div>
              <h3 className="font-semibold text-white">Search Slack</h3>
              <p className="text-xs text-slate-400">Search across channels and messages</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-2 text-slate-400 hover:text-white hover:bg-slate-700/50 
                       rounded-lg transition-all"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Search Form */}
        <form onSubmit={handleSearch} className="p-4 border-b border-slate-700/50">
          <div className="flex gap-3">
            {/* Channel selector */}
            <div className="relative">
              <select
                value={selectedChannel || ''}
                onChange={(e) => setSelectedChannel(e.target.value || null)}
                className="h-full px-3 py-2 bg-slate-800/50 border border-slate-700 rounded-lg
                           text-sm text-slate-300 focus:outline-none focus:border-primary-500
                           appearance-none pr-8 min-w-[140px]"
              >
                <option value="">All channels</option>
                {channels.map((channel) => (
                  <option key={channel.id} value={channel.name}>
                    #{channel.name}
                  </option>
                ))}
              </select>
              <Hash className="absolute right-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-500 pointer-events-none" />
            </div>

            {/* Search input */}
            <div className="flex-1 relative">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search messages..."
                className="input-field pl-10"
              />
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-500" />
            </div>

            <button
              type="submit"
              disabled={isLoading || !query.trim()}
              className="btn-primary disabled:opacity-50"
            >
              {isLoading ? (
                <Loader2 className="w-5 h-5 animate-spin" />
              ) : (
                'Search'
              )}
            </button>
          </div>
        </form>

        {/* Results */}
        <div className="max-h-[60vh] overflow-y-auto">
          {error && (
            <div className="p-4 text-red-400 text-sm">
              {error}
            </div>
          )}

          {results && (
            <div className="p-4 space-y-4">
              {/* Summary */}
              <div className="flex items-center justify-between text-sm">
                <span className="text-slate-400">
                  Found {results.total_message_count} messages in {results.channels_searched} channels
                </span>
                <span className="text-slate-500">
                  {results.processing_time_ms.toFixed(0)}ms
                </span>
              </div>

              {/* Follow-up suggestions */}
              {results.follow_up_suggestions.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {results.follow_up_suggestions.map((suggestion, idx) => (
                    <button
                      key={idx}
                      onClick={() => handleSuggestionClick(suggestion)}
                      className="flex items-center gap-1.5 px-3 py-1.5 text-xs
                                 bg-slate-700/50 text-slate-300 rounded-lg
                                 hover:bg-slate-700 transition-all"
                    >
                      <ChevronRight className="w-3 h-3" />
                      {suggestion.label}
                    </button>
                  ))}
                </div>
              )}

              {/* Channel summaries */}
              {results.channel_summaries.length > 0 && (
                <div className="space-y-3">
                  {results.channel_summaries.map((channel) => (
                    <ChannelResultCard 
                      key={channel.channel_id} 
                      channel={channel}
                      onSelect={() => setSelectedChannel(channel.channel_name)}
                    />
                  ))}
                </div>
              )}

              {/* Summary text */}
              {results.summary && (
                <div className="p-3 bg-slate-800/50 rounded-lg text-sm text-slate-300">
                  {results.summary}
                </div>
              )}
            </div>
          )}

          {/* Empty state */}
          {!results && !isLoading && !error && (
            <div className="p-8 text-center text-slate-500">
              <MessageSquare className="w-12 h-12 mx-auto mb-3 opacity-30" />
              <p>Enter a search query to find Slack messages</p>
            </div>
          )}

          {/* Loading state */}
          {isLoading && (
            <div className="p-8 text-center">
              <Loader2 className="w-8 h-8 mx-auto mb-3 text-primary-500 animate-spin" />
              <p className="text-slate-400">Searching...</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Channel Result Card
interface ChannelResultCardProps {
  channel: SlackChannelSummary;
  onSelect: () => void;
}

function ChannelResultCard({ channel, onSelect }: ChannelResultCardProps) {
  return (
    <button
      onClick={onSelect}
      className="w-full text-left glass-card-hover p-4 space-y-2"
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Hash className="w-4 h-4 text-purple-400" />
          <span className="font-medium text-white">{channel.channel_name}</span>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <span className="text-slate-400">{channel.message_count} messages</span>
          <div 
            className="w-16 h-1.5 bg-slate-700 rounded-full overflow-hidden"
            title={`${Math.round(channel.relevance_score * 100)}% relevance`}
          >
            <div 
              className="h-full bg-purple-500 rounded-full"
              style={{ width: `${channel.relevance_score * 100}%` }}
            />
          </div>
        </div>
      </div>
      {channel.top_message_preview && (
        <p className="text-sm text-slate-400 line-clamp-2">
          {channel.top_message_preview}
        </p>
      )}
    </button>
  );
}

// Slack Search Button (to open the modal)
interface SlackSearchButtonProps {
  onClick: () => void;
}

export function SlackSearchButton({ onClick }: SlackSearchButtonProps) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-2 px-4 py-2 bg-purple-500/20 text-purple-400 
                 border border-purple-500/30 rounded-xl hover:bg-purple-500/30 transition-all"
    >
      <MessageSquare className="w-4 h-4" />
      <span>Slack Search</span>
    </button>
  );
}

