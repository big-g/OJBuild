import { apiFetch } from './api';

export interface MCPDefinition {
  name: string;
  url: string;
  allow_without_confirmation: boolean;
  bearer_token?: string;
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
