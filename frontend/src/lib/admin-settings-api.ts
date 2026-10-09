import { apiFetch } from './api';

export interface AdministratorField {
  key: string;
  editable: boolean;
  type?: 'str' | 'int' | 'float' | 'bool' | 'list';
  value?: string | number | boolean | (string | number | boolean)[];
  active_value?: AdministratorField['value'];
  bootstrap_value?: AdministratorField['value'];
  application?: 'live' | 'restart';
  help?: string;
  reason?: string;
  overridden?: boolean;
  pending_restart?: boolean;
  item_type?: string;
  minimum?: number;
  maximum?: number;
}
export interface AdministratorSettings {
  revision: number;
  fields: AdministratorField[];
  active_default_model: string;
}
async function request(method = 'GET', body?: unknown): Promise<AdministratorSettings> {
  const response = await apiFetch('/v1/admin-settings', {
    method, headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof value?.detail === 'string' ? value.detail : `Settings request failed (${response.status})`);
  return value;
}
export const getAdministratorSettings = () => request();
export const saveAdministratorSettings = (revision: number, changes: Record<string, unknown>) => request('PUT', { revision, changes });
