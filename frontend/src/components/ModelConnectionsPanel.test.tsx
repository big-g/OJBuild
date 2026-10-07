import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, expect, it, vi } from 'vitest';
import { ModelConnectionsPanel } from './ModelConnectionsPanel';
const { user } = vi.hoisted(() => ({ user: { is_admin: true } }));
vi.mock('../lib/auth', () => ({ getStoredUser: () => user }));
vi.mock('../lib/model-connections-api', () => ({}));
vi.mock('../lib/api', () => ({ fetchModels: vi.fn() }));
vi.mock('../lib/store', () => ({ useAppStore: { getState: () => ({ setModels: vi.fn() }) } }));
beforeEach(() => { user.is_admin = true; });
it('explains the actual scope and backend-local address meaning', () => {
  const html = renderToStaticMarkup(<ModelConnectionsPanel />);
  expect(html).toContain('does not change the current chat model');
  expect(html).toContain('id="model-server-connections"');
  expect(html).toContain('Model task assignments and diagnostics below');
  expect(html).toContain('Localhost refers to the backend server');
  expect(html).toContain('does not pull, load or run models');
  expect(html).toContain('HTTP LAN traffic is unencrypted');
});
it('associates visible name and URL rules with the fields', () => {
  const html = renderToStaticMarkup(<ModelConnectionsPanel />);
  expect(html).toContain('aria-describedby="model-name-hint"');
  expect(html).toContain('aria-describedby="model-url-hint"');
  expect(html).toContain('1–24 characters');
  expect(html).toContain('http://192.168.1.20:11434');
  expect(html).toContain('omit credentials and /api paths');
});
it('hides shared-server administration from ordinary users', () => {
  user.is_admin = false;
  expect(renderToStaticMarkup(<ModelConnectionsPanel />)).toBe('');
});
