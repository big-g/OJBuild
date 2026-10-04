import { afterEach, expect, it, vi } from 'vitest';
vi.mock('./api', () => ({ getBase: () => 'http://local.test' }));
import { accountRequest } from './auth';

afterEach(() => vi.unstubAllGlobals());

it('recovery uses only proof in the request body, never a saved login or URL', async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ username: 'gary' }) });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('localStorage', { getItem: () => { throw new Error('Must not read stored auth'); } });
  expect(await accountRequest('recover', { recovery_code: 'private-code' })).toEqual({ username: 'gary' });
  const [url, options] = fetchMock.mock.calls[0];
  expect(url).toBe('http://local.test/v1/auth/recover');
  expect(options.headers).toEqual({ 'Content-Type': 'application/json' });
  expect(JSON.parse(options.body)).toEqual({ recovery_code: 'private-code' });
});

it('account changes send a human session and never save passwords locally', async () => {
  const setItem = vi.fn();
  vi.stubGlobal('localStorage', { getItem: () => JSON.stringify({ sessionToken: 'session' }), setItem });
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) });
  vi.stubGlobal('fetch', fetchMock);
  await accountRequest('password', { current_password: 'old', new_password: 'new-password' });
  expect(fetchMock.mock.calls[0][1].headers['X-OpenJarvis-Session']).toBe('session');
  expect(setItem).not.toHaveBeenCalled();
});

it('validation and server failures do not surface echoed secret values', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 422, json: async () => ({ detail: 'secret' }) }));
  await expect(accountRequest('recover', { recovery_code: 'private-code' })).rejects.toThrow('password length');
});
