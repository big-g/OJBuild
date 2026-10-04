import { apiFetch } from './api';

export interface RuntimeDefinition {
  name: string;
  description: string;
  transform?: string;
  adapter_id?: string;
  config?: RuntimeConfig;
}
export type RuntimeConfig = Record<string, string | string[]>;
export interface RuntimeField {
  name: string;
  label: string;
  type: 'text' | 'select' | 'string_list';
  required?: boolean;
  max_length?: number;
  placeholder?: string;
  description?: string;
  options?: { value: string; label: string }[];
}
export interface RuntimeAdapter {
  adapter_id: string;
  label: string;
  description: string;
  validator_version: string;
  fields: RuntimeField[];
  default_config: RuntimeConfig;
}
export interface RuntimeTool extends RuntimeDefinition {
  adapter_id: string;
  config: RuntimeConfig;
  id: string;
  revision: number;
  enabled: boolean;
  approved: boolean;
  approved_by: string;
  status: string;
  fingerprint: string;
  required_capabilities: string[];
  validator_version: string;
  validation_error?: string;
}
export interface ToolAudit {
  seq: number;
  revision: number;
  event: string;
  actor: string;
  timestamp: number;
}

export function editableDefinition(tool: RuntimeTool, adapters: RuntimeAdapter[]): RuntimeDefinition {
  const adapter = adapters.find(a => a.adapter_id === tool.adapter_id) || adapters[0];
  return {
    name: tool.name, description: tool.description,
    adapter_id: adapter?.adapter_id || tool.adapter_id,
    config: structuredClone(adapter?.adapter_id === tool.adapter_id ? tool.config : adapter?.default_config || {}),
  };
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
