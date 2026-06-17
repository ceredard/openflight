import { useEffect, useCallback } from 'react';
import { socketService } from '../services/socketService';

export function useSocket() {
  useEffect(() => {
    socketService.connect();

    return () => {
      socketService.disconnect();
    };
  }, []);

  const shutdown = useCallback(() => {
    fetch('/api/shutdown', { method: 'POST' }).catch(() => {});
  }, []);

  return {
    socketService,
    shutdown,
  };
}
