// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { CloudProvidersPanel } from './CloudProvidersPanel';
const { user, get, save, remove } = vi.hoisted(() => ({
  user: { is_admin: true }, get: vi.fn(), save: vi.fn(), remove: vi.fn(),
}));
vi.mock('../lib/auth', () => ({ getStoredUser: () => user }));
vi.mock('../lib/cloud-providers-api', () => ({ getCloudProviders: get, saveCloudProvider: save, removeCloudProvider: remove }));
const settings = { revision: 3, providers: [{ id: 'openai', name: 'OpenAI', key_name: 'OPENAI_API_KEY', configured: false, source: 'none' }], audit: [] };
let root: Root;
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  user.is_admin = true; vi.clearAllMocks(); get.mockResolvedValue(settings);
});
afterEach(async () => { await act(async () => root.unmount()); document.body.replaceChildren(); });
const button = (label: string) => [...document.querySelectorAll('button')].find(b => b.textContent === label)!;
async function enterKey(value: string) {
  const input = document.querySelector<HTMLInputElement>('input[type="password"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  return input;
}
it('saves to the server with current revision and clears the draft', async () => {
  save.mockResolvedValue({ ...settings, revision: 4, providers: [{ ...settings.providers[0], configured: true, source: 'managed' }] });
  await act(async () => root.render(<CloudProvidersPanel />));
  const input = await enterKey('private-key');
  expect(input.type).toBe('password');
  await act(async () => button('Save provider key').click());
  expect(save).toHaveBeenCalledWith('openai', 3, 'private-key');
  expect(input.value).toBe('');
  expect(document.body.textContent).toContain('Configured on server');
  expect(document.body.textContent).not.toContain('private-key');
});
it('removes the server key using its revision', async () => {
  get.mockResolvedValue({ ...settings, providers: [{ ...settings.providers[0], configured: true }] });
  remove.mockResolvedValue({ ...settings, revision: 4 });
  await act(async () => root.render(<CloudProvidersPanel />));
  await act(async () => button('Remove key').click());
  expect(remove).toHaveBeenCalledWith('openai', 3);
  expect(document.body.textContent).toContain('Legacy keys stay overridden');
});
it('does not fetch credential settings for ordinary users', async () => {
  user.is_admin = false;
  await act(async () => root.render(<CloudProvidersPanel />));
  expect(get).not.toHaveBeenCalled(); expect(document.body.textContent).toBe('');
});
it('clears the draft and explains stale revisions without echoing it', async () => {
  save.mockRejectedValue(new Error('Provider settings changed; reload before saving'));
  await act(async () => root.render(<CloudProvidersPanel />));
  const input = await enterKey('private-key');
  await act(async () => button('Save provider key').click());
  expect(input.value).toBe(''); expect(document.querySelector('[role="alert"]')?.textContent).toContain('reload');
});
