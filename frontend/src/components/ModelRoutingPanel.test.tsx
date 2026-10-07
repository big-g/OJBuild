import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, expect, it, vi } from 'vitest';
import { ModelRoutingPanel } from './ModelRoutingPanel';
const { user } = vi.hoisted(() => ({ user: { is_admin: true } }));
vi.mock('../lib/auth', () => ({ getStoredUser: () => user }));
vi.mock('../lib/api', () => ({ fetchModels: vi.fn() }));
vi.mock('../lib/model-connections-api', () => ({ listModelConnections: vi.fn() }));
beforeEach(() => { user.is_admin = true; });
it('explains limited diagnostics, explicit task selection and no execution', () => {
  const html = renderToStaticMarkup(<ModelRoutingPanel />);
  expect(html).toContain('not a broad quality ranking');
  expect(html).toContain('No generated code or tool call is executed');
  expect(html).toContain('Manual model');
  expect(html).toContain('aria-label="Assignment task"');
  expect(html).toContain('aria-label="Assignment model"');
});
it('hides shared routing controls from ordinary accounts', () => {
  user.is_admin = false;
  expect(renderToStaticMarkup(<ModelRoutingPanel />)).toBe('');
});

it('explains optional preflight-only fallback and representative cases', () => {
  const html = renderToStaticMarkup(<ModelRoutingPanel />);
  expect(html).toContain('aria-label="Assignment fallback"');
  expect(html).toContain('three fixed cases');
  expect(html).toContain('generation/stream failures never switch models');
  expect(html).toContain('defaults to off');
});

it('explains missing model setup and exposes a refresh control', () => {
  const html = renderToStaticMarkup(<ModelRoutingPanel />);
  expect(html).toContain('Installed models need a saved server connection');
  expect(html).toContain('href="#model-server-connections"');
  expect(html).toContain('Read capabilities');
  expect(html).toContain('Enable for chat');
  expect(html).toContain('Refresh models');
});
