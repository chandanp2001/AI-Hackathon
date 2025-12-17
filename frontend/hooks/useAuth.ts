import { useState, useEffect, useCallback } from 'react';
import { checkAuthStatus, getAuthUrl, disconnectAuth } from '@/services/api';
import { AuthStatusResponse } from '@/services/types';

interface UseAuthOptions {
  userId: string;
  checkOnMount?: boolean;
}

interface UseAuthReturn {
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  scopes: string[];
  expiresAt: string | null;
  connect: () => Promise<void>;
  disconnect: () => Promise<void>;
  refresh: () => Promise<void>;
}

export function useAuth({ userId, checkOnMount = true }: UseAuthOptions): UseAuthReturn {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scopes, setScopes] = useState<string[]>([]);
  const [expiresAt, setExpiresAt] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!userId) {
      setIsLoading(false);
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      const status: AuthStatusResponse = await checkAuthStatus(userId);
      setIsAuthenticated(status.connected);
      setScopes(status.scopes || []);
      setExpiresAt(status.expires_at || null);
      
      if (status.error) {
        setError(status.error);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to check auth status');
      setIsAuthenticated(false);
    } finally {
      setIsLoading(false);
    }
  }, [userId]);

  const connect = useCallback(async () => {
    if (!userId) {
      setError('User ID is required');
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      const { authorization_url } = await getAuthUrl(userId);
      // Redirect to Google OAuth
      window.location.href = authorization_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to get auth URL');
      setIsLoading(false);
    }
  }, [userId]);

  const disconnect = useCallback(async () => {
    if (!userId) {
      setError('User ID is required');
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      await disconnectAuth(userId);
      setIsAuthenticated(false);
      setScopes([]);
      setExpiresAt(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to disconnect');
    } finally {
      setIsLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    if (checkOnMount) {
      refresh();
    }
  }, [checkOnMount, refresh]);

  return {
    isAuthenticated,
    isLoading,
    error,
    scopes,
    expiresAt,
    connect,
    disconnect,
    refresh,
  };
}

