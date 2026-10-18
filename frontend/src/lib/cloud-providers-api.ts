import { apiFetch } from './api';

export interface CloudProvider {
  id: string;
  name: string;
  key_name: string;
  configured: boolean;
  source: 'managed' | 'legacy' | 'none';
}
export interface CloudProviderSettings {
  revision: number;
  providers: CloudProvider[];
  audit: Array<{ actor: string; provider: string; action: string; at: string }>;
}
async function result(response: Response): Promise<CloudProviderSettings> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || 'Cannot update cloud providers');
  }
  return response.json();
}
export async function getCloudProviders() {
  return result(await apiFetch('/v1/cloud/providers'));
}
export async function saveCloudProvider(id: string, revision: number, key: string) {
  return result(await apiFetch(`/v1/cloud/providers/${encodeURIComponent(id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision, key }),
  }));
}
export async function removeCloudProvider(id: string, revision: number) {
  return result(await apiFetch(`/v1/cloud/providers/${encodeURIComponent(id)}`, {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision }),
  }));
}

export async function getCloudAvailability(): Promise<Record<string, boolean>> {
  const response = await apiFetch('/v1/cloud/availability');
  if (!response.ok) throw new Error('Cannot load server cloud availability');
  const data: { providers: Array<{ key_name: string; configured: boolean }> } = await response.json();
  return Object.fromEntries(data.providers.map(p => [p.key_name, p.configured]));
}
