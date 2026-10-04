import { apiFetch } from './api';

export interface RuntimeDefinition {
  name: string;
  description: string;
  transform: string;
}
export interface RuntimeTool extends RuntimeDefinition {
  id: string;
  revision: number;
  enabled: boolean;
  approved: boolean;
  approved_by: string;
  status: string;
  fingerprint: string;
  required_capabilities: string[];
  validator_version: string;
}
export interface ToolAudit {
  seq: number;
  revision: number;
  event: string;
  actor: string;
  timestamp: number;
}

export async function runtimeRequest<T>(path = '', method = 'GET', body?: unknown): Promise<T> {
  const response = await apiFetch(`/v1/runtime-tools${path}`, {
    method,
    ...(body === undefined ? {} : {
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : `Tool request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}
