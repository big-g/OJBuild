// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { WorkflowsPage } from './WorkflowsPage';

const api = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock('../lib/api', () => ({ apiFetch: api.fetch }));
vi.mock('../lib/auth', () => ({ getStoredUser: () => ({ is_admin: false }) }));
let root: Root;
const saved = { id: 'saved', revision: 1, definition: { version: 1, name: 'Inspect', steps: [{ id: 'inspect', operation: 'mesh_inspect', input: 'input' }] } };
const catalog = { workflows: [saved], operations: [], templates: [], image_servers: [] };
const response = (value: unknown) => new Response(JSON.stringify(value));
const button = (text: string) => [...document.querySelectorAll('button')].find(item => item.textContent === text)!;
const mount = () => act(async () => { root.render(<MemoryRouter><WorkflowsPage /></MemoryRouter>); });
const choose = () => act(async () => {
  const select = document.querySelector('select')!;
  select.value = 'saved'; select.dispatchEvent(new Event('change', { bubbles: true }));
});

beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  Element.prototype.scrollIntoView = vi.fn();
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  api.fetch.mockImplementation(async (path: string) => response(path === '/v1/workflows' ? catalog : path === '/v1/files' ? { files: [] } : { runs: [] }));
});
afterEach(async () => {
  await act(async () => root.unmount()); document.body.replaceChildren(); vi.useRealTimers();
});

it('locks the editor until the saved revision is acknowledged', async () => {
  await mount(); await choose();
  let resolve!: (value: Response) => void;
  api.fetch.mockImplementation((path: string, options?: RequestInit) => {
    if (options?.method === 'POST') return new Promise<Response>(done => { resolve = done; });
    return Promise.resolve(response(path === '/v1/workflows' ? catalog : path === '/v1/files' ? { files: [] } : { runs: [] }));
  });
  await act(async () => button('Save workflow').click());
  expect((document.querySelector('fieldset[aria-label="Workflow editor"]') as HTMLFieldSetElement).disabled).toBe(true);
  expect(button('Run saved workflow').disabled).toBe(true);
  await act(async () => resolve(response({ ...saved, revision: 2 })));
  expect((document.querySelector('fieldset[aria-label="Workflow editor"]') as HTMLFieldSetElement).disabled).toBe(false);
});

it('keeps an accepted run successful when the files refresh fails', async () => {
  await mount(); await choose();
  const run = { id: 'run-1', status: 'queued', definition: saved.definition, steps: [] };
  api.fetch.mockImplementation(async (path: string, options?: RequestInit) => {
    if (options?.method === 'POST') return response(run);
    if (path === '/v1/files') throw new Error('offline');
    return response(path === '/v1/workflows' ? catalog : { runs: [run] });
  });
  await act(async () => button('Run saved workflow').click());
  expect(document.body.textContent).toContain('Run run-1 accepted');
  expect(document.body.textContent).toContain('Some sections could not refresh');
  expect(document.querySelector('[role="alert"]')).toBeNull();
  expect(document.querySelector('#workflow-run-run-1')).not.toBeNull();
  expect(api.fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});

it('does not overlap slow polling and aborts requests on unmount', async () => {
  const signals: AbortSignal[] = [];
  api.fetch.mockImplementation((_path: string, options: RequestInit) => {
    signals.push(options.signal as AbortSignal);
    return new Promise(() => {});
  });
  await mount();
  await act(async () => vi.advanceTimersByTimeAsync(60000));
  expect(api.fetch).toHaveBeenCalledTimes(3);
  await act(async () => root.unmount());
  expect(signals.every(signal => signal.aborted)).toBe(true);
});

it('loads reports on demand and invalidates detail when steps advance', async () => {
  let state = 'running';
  const compact = () => ({ id: 'detail-run', compact: true, status: 'running', definition: saved.definition, steps: [{ id: 'inspect', status: state }] });
  api.fetch.mockImplementation(async (path: string) => {
    if (path === '/v1/workflows') return response(catalog);
    if (path === '/v1/files') return response({ files: [] });
    if (path === '/v1/workflows/runs/detail-run') return response({ ...compact(), compact: false, steps: [{ id: 'inspect', status: state, output: { report: { note: 'loaded report' } } }] });
    return response({ runs: [compact()] });
  });
  await mount();
  expect(document.body.textContent).not.toContain('loaded report');
  await act(async () => button('Load step reports and previews').click());
  expect(document.body.textContent).toContain('loaded report');
  state = 'completed';
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(document.body.textContent).not.toContain('loaded report');
  expect(button('Load step reports and previews')).toBeTruthy();
});

it('uploads the original file as binary instead of base64 JSON', async () => {
  await mount();
  const file = new File(['mesh'], 'original.glb', { type: 'model/gltf-binary' });
  api.fetch.mockImplementation(async (path: string, options?: RequestInit) => {
    if (options?.method === 'POST') return response({ id: 'artifact', filename: file.name, size: file.size });
    return response(path === '/v1/workflows' ? catalog : path === '/v1/files' ? { files: [] } : { runs: [] });
  });
  const input = document.querySelector('input[type=file]')!;
  Object.defineProperty(input, 'files', { value: [file] });
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })));
  const upload = api.fetch.mock.calls.find(([, options]) => options?.method === 'POST')!;
  expect(upload[0]).toBe('/v1/files/upload?filename=original.glb');
  expect(upload[1].body).toBe(file);
});
