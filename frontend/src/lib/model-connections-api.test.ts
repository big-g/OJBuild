import { beforeEach, expect, it, vi } from 'vitest';
import {
  createModelConnection, listModelConnections, removeModelConnection,
  testModelConnection, updateModelConnection, type ModelConnection,
  readModelCapabilities, enableModelConnection,
} from './model-connections-api';
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('./api', () => ({ apiFetch }));
const connection = { id: 'server/one', name: 'gpu', url: 'http://127.0.0.1:11434', revision: 3 } as ModelConnection;
beforeEach(() => { apiFetch.mockReset(); apiFetch.mockImplementation(async () => new Response('{}', { status: 200 })); });
it('binds capability reads and chat activation to the reviewed revision', async () => {
  await readModelCapabilities(connection, 'qwen3.5:9b');
  expect(apiFetch).toHaveBeenLastCalledWith('/v1/model-connections/server%2Fone/capabilities', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ revision: 3, serving_id: 'qwen3.5:9b' }),
  }));
  await enableModelConnection(connection, true);
  expect(apiFetch).toHaveBeenLastCalledWith('/v1/model-connections/server%2Fone/enabled', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ revision: 3, enabled: true }),
  }));
});
it('uses the authenticated API wrapper and binds edits/tests to their revision', async () => {
  await updateModelConnection(connection, 'second', 'http://192.168.1.20:11434');
  expect(apiFetch).toHaveBeenLastCalledWith('/v1/model-connections/server%2Fone', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ name: 'second', url: 'http://192.168.1.20:11434', revision: 3 }),
  }));
  await testModelConnection(connection);
  expect(apiFetch).toHaveBeenLastCalledWith('/v1/model-connections/server%2Fone/test', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ revision: 3 }),
  }));
});
it('sends configuration to the backend rather than connecting from the browser', async () => {
  await createModelConnection('gpu', connection.url);
  expect(apiFetch).toHaveBeenLastCalledWith('/v1/model-connections', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ name: 'gpu', url: connection.url }),
  }));
  await listModelConnections();
  expect(apiFetch).toHaveBeenLastCalledWith('/v1/model-connections', { method: 'GET' });
});
it('handles empty deletion responses and exposes actionable conflict guidance', async () => {
  apiFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
  await expect(removeModelConnection(connection)).resolves.toBeUndefined();
  expect(apiFetch).toHaveBeenLastCalledWith('/v1/model-connections/server%2Fone?revision=3', { method: 'DELETE' });
  apiFetch.mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Connection changed. Reload and try again.' }), { status: 409 }));
  await expect(testModelConnection(connection)).rejects.toThrow('Connection changed. Reload and try again.');
});
