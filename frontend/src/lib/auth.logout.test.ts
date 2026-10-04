import { afterEach, expect, it, vi } from 'vitest';
vi.mock('./api', () => ({ getBase: () => 'http://local.test' }));
import { logout } from './auth';

afterEach(() => vi.unstubAllGlobals());

it('revokes the human session and clears local auth', async () => {
  const removeItem = vi.fn();
  vi.stubGlobal('localStorage', { getItem: () => JSON.stringify({ sessionToken: 'session' }), removeItem });
  const fetchMock = vi.fn().mockResolvedValue({ ok: true });
  vi.stubGlobal('fetch', fetchMock);
  await logout();
  expect(fetchMock).toHaveBeenCalledWith('http://local.test/v1/auth/logout', expect.objectContaining({
    method: 'POST', headers: { 'X-OpenJarvis-Session': 'session' }, signal: expect.any(AbortSignal),
  }));
  expect(removeItem).toHaveBeenCalledWith('openjarvis-auth');
});

it('clears local auth when server revocation fails', async () => {
  const removeItem = vi.fn();
  vi.stubGlobal('localStorage', { getItem: () => JSON.stringify({ sessionToken: 'session' }), removeItem });
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
  await expect(logout()).rejects.toThrow('offline');
  expect(removeItem).toHaveBeenCalledWith('openjarvis-auth');
});
