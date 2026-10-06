import { apiFetch } from './api';

export interface ModelConnection {
  id: string;
  name: string;
  url: string;
  adapter_id: 'ollama';
  config_version: number;
  revision: number;
  discovery_state: 'untested' | 'discovered' | 'error';
  tested_at: number;
  catalog: { serving_id: string; size_bytes: number; capability_state: 'unknown' }[];
}
export interface ModelConnectionEvent { revision: number; event: string; actor: string; timestamp: number }

async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await apiFetch(`/v1/model-connections${path}`, {
    method,
    ...(body === undefined ? {} : {
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(typeof error?.detail === 'string' ? error.detail : `Model connection request failed (${response.status})`);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export const listModelConnections = () => request<{ connections: ModelConnection[] }>('');
export const createModelConnection = (name: string, url: string) => request<ModelConnection>('', 'POST', { name, url });
export const updateModelConnection = (connection: ModelConnection, name: string, url: string) =>
  request<ModelConnection>(`/${encodeURIComponent(connection.id)}`, 'PUT', { name, url, revision: connection.revision });
export const testModelConnection = (connection: ModelConnection) =>
  request<{ ok: boolean; connection: ModelConnection; message: string }>(`/${encodeURIComponent(connection.id)}/test`, 'POST', { revision: connection.revision });
export const removeModelConnection = (connection: ModelConnection) =>
  request<void>(`/${encodeURIComponent(connection.id)}?revision=${connection.revision}`, 'DELETE');
export const getModelConnectionAudit = (id: string) =>
  request<{ events: ModelConnectionEvent[] }>(`/${encodeURIComponent(id)}/audit`);
