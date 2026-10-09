import { beforeEach, expect, it, vi } from 'vitest';
import { apiFetch } from './api';
import { getAdministratorSettings, saveAdministratorSettings } from './admin-settings-api';
vi.mock('./api', () => ({ apiFetch: vi.fn() }));
beforeEach(() => { vi.clearAllMocks(); });
it('sends versioned changes through the authenticated API', async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ revision: 2, fields: [] })));
  await saveAdministratorSettings(1, { 'intelligence.default_model': 'ornith-1.5:35b' });
  expect(apiFetch).toHaveBeenCalledWith('/v1/admin-settings', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ revision: 1, changes: { 'intelligence.default_model': 'ornith-1.5:35b' } }),
  }));
});
it('surfaces stale revision conflicts instead of claiming a successful save', async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ detail: 'Reload before saving' }), { status: 409 }));
  await expect(saveAdministratorSettings(1, {})).rejects.toThrow('Reload before saving');
});
it('rejects denied administrator reads', async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response('', { status: 403 }));
  await expect(getAdministratorSettings()).rejects.toThrow('Settings request failed (403)');
});
