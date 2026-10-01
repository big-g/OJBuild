import { apiFetch } from './api';

export interface SourceFieldCondition {
  field: string;
  equals?: string | number | boolean;
  one_of?: (string | number | boolean)[];
}

export interface SourceField {
  name: string;
  label: string;
  type: 'text' | 'number' | 'checkbox' | 'select' | 'credential';
  credential_kinds?: string[];
  required?: boolean;
  placeholder?: string;
  default_value?: string | number | boolean;
  description?: string;
  min?: number;
  max?: number;
  visible_when?: SourceFieldCondition | SourceFieldCondition[];
  value_updates?: Record<string, SourceConfig>;
  options?: { value: string; label: string }[];
}

export interface SourceAdapter {
  adapter_id: string;
  display_name: string;
  description: string;
  config_version: number;
  fields: SourceField[];
  required_capabilities: string[];
  operations: string[];
}

export type SourceConfig = Record<string, string | number | boolean>;
export interface SourceInstance {
  id: string;
  adapter_id: string;
  name: string;
  config: SourceConfig;
  config_version: number;
  revision: number;
  enabled: boolean;
  state: 'idle' | 'syncing' | 'error';
  error: string | null;
  chunks?: number;
  checkpoint?: { last_sync: string | null; items_synced: number; error: string | null } | null;
}

async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await apiFetch(`/v1/sources${path}`, {
    method,
    ...(body === undefined ? {} : {
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(typeof error.detail === 'string' ? error.detail : `Source request failed (${response.status})`);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export const listSourceAdapters = () => request<{ adapters: SourceAdapter[] }>('/adapters');
export const listSourceInstances = () => request<{ sources: SourceInstance[] }>('');
export const testSourceConfiguration = (adapter_id: string, config: SourceConfig) =>
  request<{ ok: boolean; config: SourceConfig; documents?: number; sample_titles?: string[]; final_url?: string }>('/test', 'POST', { adapter_id, config });
export const createSourceInstance = (adapter_id: string, name: string, config: SourceConfig) =>
  request<SourceInstance>('', 'POST', { adapter_id, name, config });
export const updateSourceInstance = (source: SourceInstance) =>
  request<SourceInstance>(`/${encodeURIComponent(source.id)}`, 'PUT', {
    revision: source.revision, name: source.name, config: source.config, enabled: source.enabled,
  });
export const removeSourceInstance = (source: SourceInstance) =>
  request<void>(`/${encodeURIComponent(source.id)}?revision=${source.revision}`, 'DELETE');
export const syncSourceInstance = (id: string) =>
  request<{ status: string }>(`/${encodeURIComponent(id)}/sync`, 'POST');

export interface SourceCredential {
  id: string; name: string; kind: 'bearer' | 'api_key'; origin: string;
  header_name: string; revision: number; created_at: string; updated_at: string;
}
export const listSourceCredentials = () => request<{ credentials: SourceCredential[] }>('/credentials');
export const createSourceCredential = (input: { name: string; kind: string; origin: string; header_name: string; secret: string }) =>
  request<SourceCredential>('/credentials', 'POST', input);
export const rotateSourceCredential = (credential: SourceCredential, secret: string) =>
  request<SourceCredential>(`/credentials/${encodeURIComponent(credential.id)}`, 'PUT', { revision: credential.revision, secret });
export const removeSourceCredential = (credential: SourceCredential) =>
  request<void>(`/credentials/${encodeURIComponent(credential.id)}?revision=${credential.revision}`, 'DELETE');
