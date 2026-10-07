import type { ModelInfo } from '../types';
import type { ModelConnection } from './model-connections-api';

export interface RoutingModelOption extends ModelInfo {
  eligible: boolean;
  setup_hint: string;
}

// Match Python quote(..., safe='') for server-bound model identities.
const encodeServingId = (id: string) => encodeURIComponent(id).replace(/[!'()*]/g, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);

export function routingModelOptions(installed: ModelInfo[], connections: ModelConnection[]): RoutingModelOption[] {
  const saved = connections.flatMap(c => c.catalog.map(m => {
    const setup_hint = c.adapter_id !== 'ollama' || c.config_version !== 1 ? 'Unsupported connection adapter/version'
      : c.discovery_state !== 'discovered' ? 'Test catalog'
      : m.capability_state !== 'reported' ? 'Read capabilities'
      : !m.capabilities?.includes('completion') ? 'Not chat-capable'
      : !c.enabled ? 'Enable server for chat' : '';
    return {
      id: `oj/${c.id}/${encodeServingId(m.serving_id)}`, object: 'model', created: 0,
      owned_by: 'configured_ollama', connection_id: c.id, connection_name: c.name,
      serving_id: m.serving_id, display_name: `${m.serving_id} — ${c.name}`,
      capabilities: m.capabilities, capability_state: m.capability_state,
      eligible: !setup_hint, setup_hint,
    };
  }));
  const catalogNames = new Set(saved.map(m => m.serving_id));
  const unsaved = installed.filter(m => m.owned_by !== 'configured_ollama' && !catalogNames.has(m.id)).map(m => ({
    ...m, eligible: false, setup_hint: 'Set up an Ollama server connection',
  }));
  return [...saved, ...unsaved];
}
