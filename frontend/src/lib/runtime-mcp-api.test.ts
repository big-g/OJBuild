import { beforeEach, expect, it, vi } from 'vitest';
import { connectionDefinition, mcpRequest, type MCPConnection } from './runtime-mcp-api';
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('./api', () => ({ apiFetch }));
beforeEach(() => apiFetch.mockReset());

it('sends the exact reviewed revision through authenticated requests', async () => {
  apiFetch.mockResolvedValue({ ok: true, json: async () => ({ approved: true }) });
  expect(await mcpRequest('/id/approve', 'POST', { revision: 7 })).toEqual({ approved: true });
  expect(apiFetch).toHaveBeenCalledWith('/v1/runtime-mcp/id/approve', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"revision":7}',
  });
});

it('does not retry or silently approve a stale catalog', async () => {
  apiFetch.mockResolvedValue({ ok: false, status: 409, json: async () => ({ detail: 'Connection changed; reload before reviewing' }) });
  await expect(mcpRequest('/id/approve', 'POST', { revision: 7 })).rejects.toThrow('reload before reviewing');
  expect(apiFetch).toHaveBeenCalledTimes(1);
});

it('keeps saved tokens by omission and requires explicit replacement or clearing', () => {
  const saved = { name: 'example', url: 'https://example.com/mcp', allow_without_confirmation: false,
    has_token: true, revision: 4, fingerprint: 'digest' } as MCPConnection;
  expect(connectionDefinition(saved)).toEqual({ name: saved.name, url: saved.url, allow_without_confirmation: false });
  expect(connectionDefinition(saved, 'new-token').bearer_token).toBe('new-token');
  expect(connectionDefinition(saved, '', true).bearer_token).toBe('');
  expect(saved).not.toHaveProperty('bearer_token');
});
