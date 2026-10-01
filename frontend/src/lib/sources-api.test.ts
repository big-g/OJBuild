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
