// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ContentReviewPanel } from './ContentReviewPanel';
const api = vi.hoisted(() => vi.fn());
vi.mock('../lib/api', () => ({ apiFetch: api }));
let root: Root;
const button = (name: string) => [...document.querySelectorAll('button')].find(b => b.textContent === name)!;
beforeEach(() => {
  vi.resetAllMocks(); Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  URL.createObjectURL = vi.fn(() => 'blob:private-review'); URL.revokeObjectURL = vi.fn();
});
afterEach(async () => { await act(async () => root.unmount()); document.body.replaceChildren(); });
it('requires a loaded private preview before approving image delivery', async () => {
  api.mockResolvedValueOnce({ ok: true, json: async () => ({ reviews: [{ id: 'review', owner: 'child', age: 12, request: '<script>prompt</script>', state: 'pending_image' }] }) })
    .mockResolvedValueOnce({ ok: true, blob: async () => new Blob(['image']) });
  await act(async () => root.render(<ContentReviewPanel />));
  expect(document.querySelector('script')).toBeNull();
  expect(button('Approve image for delivery').disabled).toBe(true);
  await act(async () => document.querySelector('img')!.dispatchEvent(new Event('load')));
  expect(button('Approve image for delivery').disabled).toBe(false);
  api.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ ok: true, json: async () => ({ reviews: [] }) });
  await act(async () => button('Approve image for delivery').click());
  const [path, init] = api.mock.calls[2];
  expect(path).toBe('/v1/content-reviews/review');
  expect(JSON.parse(init.body)).toEqual({ stage: 'pending_image', approve: true });
  expect(document.body.textContent).toContain('No pending reviews');
});
it('offers no approvals when reviewer access is denied', async () => {
  api.mockResolvedValue({ ok: false });
  await act(async () => root.render(<ContentReviewPanel />));
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('birthdate');
  expect(document.querySelector('img')).toBeNull();
  expect(document.querySelectorAll('button')).toHaveLength(1);
});
