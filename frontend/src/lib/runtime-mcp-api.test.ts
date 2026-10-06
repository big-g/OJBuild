import { beforeEach, expect, it, vi } from 'vitest';
import { connectionDefinition, mcpRequest, type MCPConnection } from './runtime-mcp-api';
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('./api', () => ({ apiFetch }));
beforeEach(() => apiFetch.mockReset());

it('retains API key header settings without returning or resubmitting saved secrets', () => {
  const saved = { name: 'example', url: 'https://example.com/mcp', allow_without_confirmation: false,
    auth_type: 'api_key', api_key_header: 'x-api-key', has_token: true } as MCPConnection;
  expect(connectionDefinition(saved)).toEqual({ name: saved.name, url: saved.url, allow_without_confirmation: false,
    auth_type: 'api_key', api_key_header: 'x-api-key' });
  expect(connectionDefinition(saved, 'new-key').credential_secret).toBe('new-key');
  expect(connectionDefinition(saved, '', true).credential_secret).toBe('');
  expect(connectionDefinition(saved)).not.toHaveProperty('credential_secret');
  expect(connectionDefinition(saved, 'new-key')).not.toHaveProperty('bearer_token');
});

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

it('imports only the reviewed server entry without sending credentials or an owner', async () => {
  const { reviewLegacyMCP, importLegacyMCP } = await import('./runtime-mcp-api');
  const entry = { index: 2, label: 'Legacy entry 3', status: 'ready' as const, reason: '',
    review_digest: 'a'.repeat(64), name: 'example', url: 'https://example.com/mcp', has_token: true };
  apiFetch.mockResolvedValue({ ok: true, json: async () => ({ entries: [entry] }) });
  expect(await reviewLegacyMCP()).toEqual([entry]);
  expect(apiFetch).toHaveBeenLastCalledWith('/v1/runtime-mcp/imports/legacy', { method: 'GET' });
  apiFetch.mockResolvedValue({ ok: true, json: async () => ({ enabled: false }) });
  await importLegacyMCP(entry);
  expect(JSON.parse(apiFetch.mock.calls[apiFetch.mock.calls.length - 1][1].body)).toEqual({ index: 2, review_digest: 'a'.repeat(64) });
});

it('retains explicit LAN endpoint and certificate policy when editing a connection', () => {
  const saved = { name: 'lan', url: 'https://mcp.internal:8443/mcp', allow_without_confirmation: false,
    network_access: 'lan', lan_addresses: '192.168.1.20', tls_trust: 'custom_ca', ca_certificate: 'PEM' } as MCPConnection;
  expect(connectionDefinition(saved)).toEqual(saved);
  expect(connectionDefinition(saved, 'replacement').bearer_token).toBe('replacement');
});
