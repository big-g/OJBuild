import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.fn<typeof fetch>();
vi.mock('./api', () => ({ apiFetch: (...args: Parameters<typeof fetch>) => apiFetch(...args) }));
import { createSourceInstance, listSourceAdapters, listSourceInstances, removeSourceInstance, syncSourceInstance, testSourceConfiguration, updateSourceInstance } from './sources-api';
import type { SourceInstance } from './sources-api';

const source: SourceInstance = {
  id: 'instance-1', adapter_id: 'future_api', name: 'Policies', config: { endpoint: 'https://example.test' },
  config_version: 1, revision: 7, enabled: true, state: 'idle', error: null,
};

beforeEach(() => {
  apiFetch.mockReset();
  apiFetch.mockImplementation(async () => new Response('{}', { status: 200 }));
});
afterEach(() => vi.clearAllMocks());

describe('source instance API', () => {
  it('routes every operation through the authenticated transport', async () => {
    await listSourceAdapters();
    await listSourceInstances();
    await createSourceInstance(source.adapter_id, source.name, source.config);
    await testSourceConfiguration(source.adapter_id, source.config);
    await updateSourceInstance(source);
    await syncSourceInstance(source.id);
    await removeSourceInstance(source);
    expect(apiFetch.mock.calls.map(([url]) => url)).toEqual([
      '/v1/sources/adapters', '/v1/sources', '/v1/sources', '/v1/sources/test',
      '/v1/sources/instance-1', '/v1/sources/instance-1/sync', '/v1/sources/instance-1?revision=7',
    ]);
    expect(JSON.parse(String(apiFetch.mock.calls[4][1]?.body))).toEqual({
      revision: 7, name: source.name, config: source.config, enabled: true,
    });
  });

  it('surfaces busy and stale-edit errors without retrying a mutation', async () => {
    apiFetch.mockResolvedValue(new Response(JSON.stringify({ detail: 'Source changed; refresh before saving' }), { status: 409 }));
    await expect(updateSourceInstance(source)).rejects.toThrow('Source changed; refresh before saving');
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it('accepts an empty successful removal response', async () => {
    apiFetch.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(removeSourceInstance(source)).resolves.toBeUndefined();
  });
});

it('uses the authenticated transport for credential lifecycle and sends revisions', async () => {
  const { listSourceCredentials, createSourceCredential, rotateSourceCredential, removeSourceCredential } = await import('./sources-api');
  const credential = { id: 'credential-one', name: 'API', kind: 'bearer' as const, origin: 'https://api.example.com', header_name: 'Authorization', revision: 2, created_at: '', updated_at: '' };
  await listSourceCredentials();
  await createSourceCredential({ name: 'API', kind: 'bearer', origin: credential.origin, header_name: '', secret: 'new-token' });
  await rotateSourceCredential(credential, 'replacement-token');
  await removeSourceCredential(credential);
  expect(apiFetch.mock.calls.map(([url]) => url)).toEqual([
    '/v1/sources/credentials', '/v1/sources/credentials', '/v1/sources/credentials/credential-one', '/v1/sources/credentials/credential-one?revision=2',
  ]);
  expect(JSON.parse(String(apiFetch.mock.calls[2][1]?.body))).toEqual({ revision: 2, secret: 'replacement-token' });
});

it('routes schedule, job history and cancellation through authenticated transport', async () => {
  const { setSourceSchedule, listSourceJobs, cancelSourceSync } = await import('./sources-api');
  await setSourceSchedule('source/one', { revision: 2, enabled: true, interval_seconds: 300 });
  await listSourceJobs('source/one');
  await cancelSourceSync('source/one');
  expect(apiFetch.mock.calls.map(([url]) => url)).toEqual(['/v1/sources/source%2Fone/schedule', '/v1/sources/source%2Fone/jobs', '/v1/sources/source%2Fone/cancel']);
  expect(JSON.parse(String(apiFetch.mock.calls[0][1]?.body))).toEqual({ revision: 2, enabled: true, interval_seconds: 300 });
});

it('uses authenticated migration preview/apply and cursor audit routes', async () => {
  const { previewSourceMigration, applySourceMigration, listSourceAudit } = await import('./sources-api');
  await previewSourceMigration(source);
  await applySourceMigration({ source_id: source.id, revision: 7, from_version: 1, to_version: 2, config: {}, index_reset: true, plan_token: 'a'.repeat(64) });
  await listSourceAudit(source.id, 20);
  await listSourceAudit();
  expect(apiFetch.mock.calls.map(([url]) => url)).toEqual(['/v1/sources/instance-1/migration/preview', '/v1/sources/instance-1/migration', '/v1/sources/instance-1/audit?before_id=20', '/v1/sources/audit']);
  expect(JSON.parse(String(apiFetch.mock.calls[1][1]?.body))).toEqual({ revision: 7, plan_token: 'a'.repeat(64) });
});

it('keeps legacy import tickets in authenticated request bodies', async () => {
  const { listLegacySourceImports, previewLegacySourceImport, applyLegacySourceImport } = await import('./sources-api');
  await listLegacySourceImports();
  await previewLegacySourceImport('provider/name', 'Work');
  await applyLegacySourceImport({
    import_id: 'provider/name', adapter_id: 'notion_pages', name: 'Work',
    credential_origin: 'https://api.notion.com', config_version: 1, config: {}, settings: [],
    fresh_index: true, legacy_connection_kept: true, expires_in_seconds: 600,
    plan_token: 'private-import-ticket',
  });
  expect(apiFetch.mock.calls.map(([url]) => url)).toEqual([
    '/v1/sources/imports', '/v1/sources/imports/provider%2Fname/preview', '/v1/sources/imports/provider%2Fname',
  ]);
  expect(JSON.parse(String(apiFetch.mock.calls[2][1]?.body))).toEqual({ plan_token: 'private-import-ticket' });
  expect(apiFetch.mock.calls.map(([url]) => String(url)).join(' ')).not.toContain('private-import-ticket');
});

it('keeps named account credentials in authenticated bodies and disconnects one instance', async () => {
  const { getSourceConnection, setSourceAccountToken, setSourceAccountClient, disconnectSourceAccount } = await import('./sources-api');
  await getSourceConnection(source.id);
  await setSourceAccountToken(source, 'private-token');
  await setSourceAccountClient(source, 'application-id', 'private-secret');
  await disconnectSourceAccount(source);
  expect(apiFetch.mock.calls.map(([url]) => url)).toEqual([
    '/v1/sources/instance-1/connection', '/v1/sources/instance-1/connection/token',
    '/v1/sources/instance-1/connection/client', '/v1/sources/instance-1/connection/disconnect',
  ]);
  expect(JSON.parse(String(apiFetch.mock.calls[1][1]?.body))).toEqual({ revision: 7, token: 'private-token' });
  expect(JSON.parse(String(apiFetch.mock.calls[2][1]?.body))).toEqual({ revision: 7, client_id: 'application-id', client_secret: 'private-secret' });
  expect(apiFetch.mock.calls.map(([url]) => String(url)).join(' ')).not.toContain('private-');
});

it('sends IMAP credentials in an authenticated, revision-bound body', async () => {
  const { setSourceAccountPassword } = await import('./sources-api');
  await setSourceAccountPassword(source, 'mail@example.com', 'protected password');
  const [url, options] = apiFetch.mock.calls[0];
  expect(url).toBe('/v1/sources/instance-1/connection/password');
  expect(options?.method).toBe('PUT');
  expect(JSON.parse(String(options?.body))).toEqual({ revision: 7, username: 'mail@example.com', password: 'protected password' });
});
