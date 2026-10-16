import { expect, it, vi } from 'vitest';
import { setAccountBirthdate, createAccount } from './accounts-api';
const api = vi.hoisted(() => vi.fn());
vi.mock('./api', () => ({ apiFetch: api }));
it('sends birthdate and timezone without calculating age in the browser', async () => {
  api.mockResolvedValue({ ok: true, json: async () => ({ ok: true }) });
  await setAccountBirthdate('child/id', '2012-02-29', 'America/New_York');
  const [path, init] = api.mock.lastCall!;
  expect(path).toBe('/v1/auth/users/child%2Fid/birthdate');
  expect(JSON.parse(init.body)).toEqual({ birthdate: '2012-02-29', birthdate_timezone: 'America/New_York' });
  await setAccountBirthdate('child', null, 'UTC');
  expect(JSON.parse(api.mock.lastCall![1].body).birthdate).toBeNull();
  await createAccount('child', 'Child', 'password123', 'user', '2012-02-29', 'America/New_York');
  expect(JSON.parse(api.mock.lastCall![1].body).birthdate).toBe('2012-02-29');
});
