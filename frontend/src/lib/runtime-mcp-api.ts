import { apiFetch } from './api';

export interface MCPDefinition {
  name: string;
  url: string;
  allow_without_confirmation: boolean;
  bearer_token?: string;
  network_access?: 'public' | 'lan';
  lan_addresses?: string;
  tls_trust?: 'system' | 'custom_ca';
  ca_certificate?: string;
}
export interface MCPConnection extends Omit<MCPDefinition, 'bearer_token'> {
  id: string;
  revision: number;
  has_token: boolean;
  discovered: boolean;
  enabled: boolean;
  approved: boolean;
  approved_by: string;
  fingerprint: string;
  validation_error?: string;
  tools: { name: string; remote_name: string; description: string;
    parameters: Record<string, unknown>; annotations: Record<string, unknown>; contract_digest: string }[];
}

export function connectionDefinition(connection: MCPConnection, token = '', clearToken = false): MCPDefinition {
  return { name: connection.name, url: connection.url,
    allow_without_confirmation: connection.allow_without_confirmation,
    ...(connection.network_access === 'lan' ? { network_access: connection.network_access,
      lan_addresses: connection.lan_addresses, tls_trust: connection.tls_trust,
      ca_certificate: connection.ca_certificate } : {}),
    ...(token || clearToken ? { bearer_token: token } : {}) };
}

export async function mcpRequest<T>(path = '', method = 'GET', body?: unknown): Promise<T> {
  const response = await apiFetch(`/v1/runtime-mcp${path}`, {
    method, ...(body === undefined ? {} : {
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : `MCP request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}


export interface LegacyMCPEntry {
  index: number;
  label: string;
  status: 'ready' | 'blocked' | 'already_saved';
  reason: string;
  review_digest: string;
  name?: string;
  url?: string;
  has_token?: boolean;
}
export async function reviewLegacyMCP(): Promise<LegacyMCPEntry[]> {
  return (await mcpRequest<{ entries: LegacyMCPEntry[] }>('/imports/legacy')).entries;
}
export function importLegacyMCP(entry: LegacyMCPEntry): Promise<MCPConnection> {
  return mcpRequest('/imports/legacy', 'POST', {
    index: entry.index, review_digest: entry.review_digest,
  });
}
