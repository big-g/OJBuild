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
