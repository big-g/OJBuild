// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MCPConnectionsPanel } from './MCPConnectionsPanel';
import type { MCPConnection } from '../lib/runtime-mcp-api';
const api = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('../lib/runtime-mcp-api', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/runtime-mcp-api')>(), mcpRequest: api.request,
}));
const connection: MCPConnection = { id: 'weather', name: 'weather', revision: 1, url: 'https://example.com/mcp',
  allow_without_confirmation: true, discovered: false, approved: false, enabled: false,
  has_token: false, fingerprint: '', approved_by: '', tools: [] };
let root: Root;
const button = (label: string) => [...document.querySelectorAll('button')].find(item => item.textContent === label)!;
beforeEach(() => {
  vi.resetAllMocks(); Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); document.body.replaceChildren(); });
it.each([true, false])('saves once, discovers, and leaves approval explicit (discovery succeeds: %s)', async success => {
  api.request.mockImplementation(async (path = '', method = 'GET') => {
    if (method === 'GET') return { connections: [connection] };
    if (method === 'PUT') return { ...connection, revision: 2 };
    if (path.endsWith('/discover')) {
      if (!success) throw new Error('offline');
      return { ...connection, revision: 3, discovered: true, tools: [{ name: 'lookup', remote_name: 'lookup', description: 'Get current data.', parameters: {}, annotations: {}, contract_digest: 'digest' }] };
    }
    throw new Error('Unexpected mutation');
  });
  await act(async () => root.render(<MCPConnectionsPanel />));
  await act(async () => button('Edit').click());
  await act(async () => document.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(api.request.mock.calls.filter(([, method]) => method === 'PUT')).toHaveLength(1);
  expect(api.request).toHaveBeenCalledWith('/weather/discover', 'POST', { revision: 2 });
  expect(api.request.mock.calls.some(([path]) => path?.endsWith('/approve'))).toBe(false);
  expect(document.body.textContent).toContain(success ? 'Saved and discovered.' : 'Connection saved disabled, but discovery failed.');
});
