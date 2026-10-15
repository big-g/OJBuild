// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ThreeDPage } from './ThreeDPage';
const api = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock('../lib/api', () => ({ apiFetch: api.fetch }));
let root: Root;
beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount()); document.body.replaceChildren(); vi.useRealTimers();
});
it('allows only one pending history refresh and aborts it on unmount', async () => {
  let signal: AbortSignal | undefined;
  api.fetch.mockImplementation((path: string, options?: RequestInit) => {
    if (path.startsWith('/v1/3d/jobs')) { signal = options?.signal as AbortSignal; return new Promise(() => {}); }
    return Promise.resolve(new Response(JSON.stringify({ workflows: [], triggers: [] })));
  });
  await act(async () => root.render(<MemoryRouter><ThreeDPage /></MemoryRouter>));
  await act(async () => vi.advanceTimersByTimeAsync(30000));
  expect(api.fetch.mock.calls.filter(([path]) => path.startsWith('/v1/3d/jobs'))).toHaveLength(1);
  expect(api.fetch.mock.calls.some(([path]) => path === '/v1/3d/jobs?limit=25&offset=0')).toBe(true);
  await act(async () => root.unmount());
  expect(signal?.aborted).toBe(true);
});
