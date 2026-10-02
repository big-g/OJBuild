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
  credential_origin?: string;
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
  connection_auth?: 'token' | 'oauth' | null;
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
  configuration_state?: 'current' | 'migration_available' | 'unsupported';
  adapter_config_version?: number;
  enabled: boolean;
  state: 'idle' | 'queued' | 'syncing' | 'cancelled' | 'error';
  schedule?: SourceSchedule;
  latest_job?: SourceJob | null;
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

export interface SourceSchedule {
  source_id?: string; revision: number; enabled: boolean; interval_seconds: number;
  next_run_at?: string | null;
}
export interface SourceJob {
  id: string; source_id: string; source_revision: number; trigger: 'manual' | 'scheduled';
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
  phase: string; created_at: string; started_at: string | null; finished_at: string | null;
  cancel_requested: boolean; documents_seen: number; documents_total: number | null;
  chunks_written: number; pages_read: number; error: string | null;
}
export const setSourceSchedule = (id: string, schedule: Pick<SourceSchedule, 'revision' | 'enabled' | 'interval_seconds'>) =>
  request<SourceSchedule>(`/${encodeURIComponent(id)}/schedule`, 'PUT', schedule);
export const listSourceJobs = (id: string) => request<{ jobs: SourceJob[] }>(`/${encodeURIComponent(id)}/jobs`);
export const cancelSourceSync = (id: string) => request<SourceJob>(`/${encodeURIComponent(id)}/cancel`, 'POST');

export interface SourceMigrationPlan {
  source_id: string; revision: number; from_version: number; to_version: number;
  config: SourceConfig; index_reset: boolean; plan_token: string;
}
export interface SourceAuditEvent {
  id: number; source_id: string; adapter_id: string; action: string; actor: string;
  created_at: string; revision: number; config_version: number;
  previous_version: number | null; schedule_revision: number | null;
  changed_fields: string[]; index_reset: boolean;
}
export const previewSourceMigration = (source: SourceInstance) =>
  request<SourceMigrationPlan>(`/${encodeURIComponent(source.id)}/migration/preview`, 'POST', { revision: source.revision });
export const applySourceMigration = (plan: SourceMigrationPlan) =>
  request<SourceInstance>(`/${encodeURIComponent(plan.source_id)}/migration`, 'POST', { revision: plan.revision, plan_token: plan.plan_token });
export const listSourceAudit = (id?: string, beforeId?: number) =>
  request<{ events: SourceAuditEvent[] }>(`${id ? `/${encodeURIComponent(id)}` : ''}/audit${beforeId === undefined ? '' : `?before_id=${beforeId}`}`);


export interface LegacySourceImport {
  import_id: string; display_name: string; adapter_id: string;
  state: 'available' | 'unavailable' | 'imported' | 'removed'; source_id: string | null;
}
export interface LegacySourceImportPlan {
  import_id: string; adapter_id: string; name: string; credential_origin: string;
  fresh_index: boolean; legacy_connection_kept: boolean; expires_in_seconds: number;
  config_version: number; config: SourceConfig;
  settings: { label: string; value: string | number | boolean }[];
  plan_token: string;
}
export const listLegacySourceImports = () => request<{ imports: LegacySourceImport[] }>('/imports');
export const previewLegacySourceImport = (id: string, name: string) =>
  request<LegacySourceImportPlan>(`/imports/${encodeURIComponent(id)}/preview`, 'POST', { name });
export const applyLegacySourceImport = (plan: LegacySourceImportPlan) =>
  request<SourceInstance>(`/imports/${encodeURIComponent(plan.import_id)}`, 'POST', { plan_token: plan.plan_token });


export const getSourceConnection = (id: string) =>
  request<{ connected: boolean; client_configured: boolean; auth_type: string }>(`/${encodeURIComponent(id)}/connection`);
export const setSourceAccountToken = (source: SourceInstance, token: string) =>
  request(`/${encodeURIComponent(source.id)}/connection/token`, 'PUT', { revision: source.revision, token });
export const setSourceAccountClient = (source: SourceInstance, client_id: string, client_secret: string) =>
  request(`/${encodeURIComponent(source.id)}/connection/client`, 'PUT', { revision: source.revision, client_id, client_secret });
export const disconnectSourceAccount = (source: SourceInstance) =>
  request(`/${encodeURIComponent(source.id)}/connection/disconnect`, 'POST', { revision: source.revision });
