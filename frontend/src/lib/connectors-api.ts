import { apiFetch, getBase } from './api';
import type { ConnectorInfo, SyncStatus, ConnectRequest, ConnectResponse } from '../types/connectors';

// ---------------------------------------------------------------------------
// Connectors API
// ---------------------------------------------------------------------------
//
// Every call here must go through apiFetch() (not a bare fetch()) so the
// Bearer auth header is attached when OPENJARVIS_API_KEY is set -- direct
// fetch() calls silently 401 against an authenticated server, exactly the
// bug apiFetch was introduced to prevent elsewhere (#266). This file was
// missed when that fix landed.

export async function listConnectors(): Promise<ConnectorInfo[]> {
  const res = await apiFetch('/v1/connectors');
  if (!res.ok) throw new Error(`Failed to list connectors: ${res.status}`);
  const data = await res.json();
  return data.connectors || [];
}

export async function getConnector(id: string): Promise<ConnectorInfo> {
  const res = await apiFetch(`/v1/connectors/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error(`Failed to get connector ${id}: ${res.status}`);
  return res.json();
}

export async function connectSource(id: string, req: ConnectRequest): Promise<ConnectResponse> {
  const res = await apiFetch(`/v1/connectors/${encodeURIComponent(id)}/connect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    // Surface the backend's actionable detail (e.g. malformed Client ID /
    // Secret) instead of a bare status code so the UI can render it.
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Failed to connect ${id}: ${res.status}`);
  }
  return res.json();
}

/** Open the server-side OAuth consent flow in a popup and resolve once the
 *  connector reports connected (or reject on timeout). Reused for any OAuth
 *  connector whose /connect returned `oauth_required` (issue #512). */
export async function startServerOAuth(id: string, oauthStartPath?: string): Promise<void> {
  const prefix = `/v1/connectors/${encodeURIComponent(id)}/oauth`;
  const path = `${prefix}/start`;
  if (oauthStartPath && oauthStartPath !== path) throw new Error('Invalid authorization start address.');
  const popup = window.open('about:blank', '_blank', 'width=600,height=700');
  if (!popup) throw new Error('Allow popups to connect this account, then try again.');
  popup.opener = null;
  let attemptId: string;
  try {
    const res = await apiFetch(path, { method: 'POST' });
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      throw new Error(typeof error.detail === 'string' ? error.detail : 'Could not start authorization.');
    }
    const attempt = await res.json();
    const ticket = typeof attempt.launch_path === 'string' ? attempt.launch_path.slice(`${prefix}/launch?ticket=`.length) : '';
    if (!attempt.launch_path?.startsWith(`${prefix}/launch?ticket=`) || !/^[A-Za-z0-9_-]{43}$/.test(ticket) || !/^[a-f0-9-]{36}$/.test(attempt.attempt_id)) {
      throw new Error('Invalid authorization handoff.');
    }
    attemptId = attempt.attempt_id;
    popup.location.href = `${getBase()}${attempt.launch_path}`;
  } catch (error) {
    popup.close();
    throw error;
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    let polling = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      clearInterval(interval); clearTimeout(timer);
      popup.close();
      if (error) reject(error); else resolve();
    };
    const interval = setInterval(async () => {
      if (settled || polling) return;
      polling = true;
      try {
        const res = await apiFetch(`${prefix}/status?attempt_id=${encodeURIComponent(attemptId)}`);
        if (!res.ok) { finish(new Error('Authorization expired or was cancelled. Please start again.')); return; }
        const result = await res.json();
        if (result.status === 'completed') finish();
        else if (result.status === 'failed') finish(new Error('Authorization failed. Please start again.'));
      } catch {
        // Retry transient transport errors until the bounded deadline.
      } finally { polling = false; }
    }, 2000);
    const timer = setTimeout(() => finish(new Error('Authorization timed out. Please start again.')), 600000);
  });
}

export class ConnectorApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ConnectorApiError';
  }
}

export async function disconnectSource(
  id: string,
  signal?: AbortSignal,
): Promise<void> {
  const res = await apiFetch(`/v1/connectors/${encodeURIComponent(id)}/disconnect`, {
    method: 'POST',
    signal,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ConnectorApiError(
      err.detail || `Failed to disconnect ${id}: ${res.status}`,
      res.status,
    );
  }
}

export interface DisconnectUntilCompleteOptions {
  signal?: AbortSignal;
  retryDelayMs?: number;
  onPending?: (message: string) => void;
}

function waitForDisconnectRetry(delayMs: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Disconnect cancelled', 'AbortError'));
      return;
    }

    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', handleAbort);
      resolve();
    }, delayMs);
    const handleAbort = () => {
      clearTimeout(timer);
      reject(new DOMException('Disconnect cancelled', 'AbortError'));
    };
    signal?.addEventListener('abort', handleAbort, { once: true });
  });
}

/**
 * Finish a disconnect even when the backend first returns 409 while an active
 * sync is stopping. The server deliberately keeps credentials and indexed
 * data intact until that worker exits; retrying is what completes cleanup.
 */
export async function disconnectSourceUntilComplete(
  id: string,
  {
    signal,
    retryDelayMs = 1500,
    onPending,
  }: DisconnectUntilCompleteOptions = {},
): Promise<void> {
  while (true) {
    try {
      await disconnectSource(id, signal);
      return;
    } catch (err) {
      if (!(err instanceof ConnectorApiError) || err.status !== 409) {
        throw err;
      }
      onPending?.(err.message);
      await waitForDisconnectRetry(retryDelayMs, signal);
    }
  }
}

export async function getSyncStatus(id: string): Promise<SyncStatus> {
  const res = await apiFetch(`/v1/connectors/${encodeURIComponent(id)}/sync`);
  if (!res.ok) throw new Error(`Failed to get sync status for ${id}: ${res.status}`);
  return res.json();
}

export async function triggerSync(id: string): Promise<{ connector_id: string; chunks_indexed: number; status: string }> {
  const res = await apiFetch(`/v1/connectors/${encodeURIComponent(id)}/sync`, {
    method: 'POST',
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Sync failed: ${res.status}`);
  }
  return res.json();
}
