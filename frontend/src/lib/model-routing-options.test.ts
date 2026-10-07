import { expect, it } from 'vitest';
import type { ModelInfo } from '../types';
import type { ModelConnection } from './model-connections-api';
import { routingModelOptions } from './model-routing-options';

const names = ['qwen3.5:9b', 'ornith-1.5:latest', 'llama3:8b', 'gemma3:4b'];
const installed: ModelInfo[] = names.map(id => ({ id, owned_by: 'openjarvis', object: 'model', created: 0 }));
const server = (changes: Partial<ModelConnection> = {}): ModelConnection => ({
  id: 'server-a', name: 'home_gpu', url: 'http://127.0.0.1:11434', adapter_id: 'ollama',
  config_version: 1, revision: 3, enabled: false, discovery_state: 'discovered', tested_at: 1,
  catalog: names.map(serving_id => ({ serving_id, size_bytes: 1, capability_state: 'unknown' })), ...changes,
});

it('shows all four original installed models when no connection has been saved', () => {
  const options = routingModelOptions(installed, []);
  expect(options.map(m => m.id)).toEqual(names);
  expect(options.every(m => !m.eligible && m.setup_hint.includes('server connection'))).toBe(true);
});

it('shows per-model missing-capability and disabled-server reasons without promoting them', () => {
  const connection = server();
  connection.catalog[0] = { ...connection.catalog[0], capability_state: 'reported', capabilities: ['completion'] };
  const options = routingModelOptions(installed, [connection]);
  expect(options).toHaveLength(4);
  expect(options[0].setup_hint).toBe('Enable server for chat');
  expect(options[1].setup_hint).toBe('Read capabilities');
  expect(options.every(m => !m.eligible)).toBe(true);
});

it('makes the four reviewed enabled chat models selectable using exact server identities', () => {
  const connection = server({ enabled: true, catalog: names.map(serving_id => ({ serving_id, size_bytes: 1, capability_state: 'reported', capabilities: ['completion'] })) });
  const options = routingModelOptions(installed, [connection]);
  expect(options).toHaveLength(4);
  expect(options.every(m => m.eligible && m.connection_id === 'server-a')).toBe(true);
  expect(options[0].id).toBe('oj/server-a/qwen3.5%3A9b');
});

it('retains separate identities for identical serving names on different servers', () => {
  const a = server({ enabled: true, catalog: [{ serving_id: "team/model!'():latest", size_bytes: 1, capability_state: 'reported', capabilities: ['completion'] }] });
  const b = { ...a, id: 'server-b', name: 'second_gpu' };
  const options = routingModelOptions([], [a, b]);
  expect(options[0].id).toBe('oj/server-a/team%2Fmodel%21%27%28%29%3Alatest');
  expect(options[1].id).toBe('oj/server-b/team%2Fmodel%21%27%28%29%3Alatest');
});

it('retains saved catalogs when the original inventory is unavailable and blocks unsupported models', () => {
  const connection = server({ enabled: true, catalog: [{ serving_id: 'embedding', size_bytes: 1, capability_state: 'reported', capabilities: ['embedding'] }] });
  const options = routingModelOptions([], [connection]);
  expect(options).toHaveLength(1);
  expect(options[0].setup_hint).toBe('Not chat-capable');
  expect(options[0].eligible).toBe(false);
  expect(routingModelOptions([], [server({ discovery_state: 'error' })])[0].setup_hint).toBe('Test catalog');
  expect(routingModelOptions([], [server({ config_version: 2 })])[0].eligible).toBe(false);
});
