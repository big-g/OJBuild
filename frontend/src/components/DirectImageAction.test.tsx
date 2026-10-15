// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { DirectImageAction } from './DirectImageAction';
const api = vi.hoisted(() => vi.fn());
vi.mock('../pages/WorkflowsPage', () => ({ workflowJson: api }));
let root: Root;
const catalog = { image_generation_allowed: true,
  operations: [{ id: 'image_generate_local', enabled: true }],
  image_servers: [{ id: 'server', enabled: true }],
  workflows: [{ id: 'saved', revision: 3, definition: { name: 'SDXL', steps: [
    { operation: 'image_generate_local', parameters: { instance_id: 'server' } },
  ] } }] };
const prompt = '  Adult fashion portrait — "red dress".\n';
const button = (text: string) => [...document.querySelectorAll('button')].find(b => b.textContent === text)!;
beforeEach(async () => {
  vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  const element = document.createElement('div'); document.body.append(element);
  root = createRoot(element);
  await act(async () => root.render(<MemoryRouter><DirectImageAction prompt={prompt} /></MemoryRouter>));
});
afterEach(async () => { await act(async () => root.unmount()); document.body.replaceChildren(); });
it('submits the exact prompt once to a saved workflow without a chat request', async () => {
  api.mockResolvedValueOnce(catalog).mockResolvedValueOnce({ id: 'run-1', status: 'queued' });
  await act(async () => button('Generate image').click());
  expect(document.querySelector('textarea')?.value).toBe(prompt);
  await act(async () => { button('Submit image job').click(); button('Submit image job').click(); });
  expect(api).toHaveBeenCalledTimes(2);
  expect(api.mock.calls[0]).toEqual(['']);
  const [path, request] = api.mock.calls[1];
  expect(path).toBe('/image-runs');
  expect(JSON.parse(request.body)).toEqual({ workflow_id: 'saved', revision: 3, prompt });
  expect(document.body.textContent).toContain('Run run-1 accepted');
  expect(document.body.textContent).not.toContain('completed');
});
it('explains denied account permission and prevents submission', async () => {
  api.mockResolvedValue({ ...catalog, image_generation_allowed: false });
  await act(async () => button('Generate image').click());
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('Direct image submission is disabled for your account');
  expect(button('Submit image job').disabled).toBe(true);
  expect(api).toHaveBeenCalledTimes(1);
});
it('explains missing enabled workflows', async () => {
  api.mockResolvedValue({ ...catalog, image_servers: [] });
  await act(async () => button('Generate image').click());
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('Save a Text to image workflow');
  expect(button('Submit image job').disabled).toBe(true);
});
