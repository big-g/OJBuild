import { beforeEach, expect, it, vi } from 'vitest';
import { editableDefinition, runtimeRequest, type RuntimeAdapter, type RuntimeTool } from './runtime-tools-api';
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('./api', () => ({ apiFetch }));
beforeEach(() => apiFetch.mockReset());

it('sends the reviewed revision through the authenticated API wrapper', async () => {
  apiFetch.mockResolvedValue({ ok: true, json: async () => ({ approved: true }) });
  expect(await runtimeRequest('/id/approve', 'POST', { revision: 4 })).toEqual({ approved: true });
  expect(apiFetch).toHaveBeenCalledWith('/v1/runtime-tools/id/approve', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"revision":4}',
  });
});

it('surfaces a stale review without retrying approval', async () => {
  apiFetch.mockResolvedValue({ ok: false, status: 409, json: async () => ({ detail: 'Tool changed; reload before reviewing' }) });
  await expect(runtimeRequest('/id/approve', 'POST', { revision: 4 })).rejects.toThrow('reload before reviewing');
  expect(apiFetch).toHaveBeenCalledTimes(1);
});

it('opens unavailable tools with a usable adapter and detached defaults for repair', () => {
  const adapter: RuntimeAdapter = { adapter_id: 'text_transform', label: 'Text', description: 'Text',
    validator_version: 'transform-v1', fields: [], default_config: { transform: 'upper' } };
  const tool = { name: 'custom_repair', description: 'Needs repair', adapter_id: 'unavailable', config: {} } as RuntimeTool;
  const form = editableDefinition(tool, [adapter]);
  expect(form).toEqual({ name: 'custom_repair', description: 'Needs repair', adapter_id: 'text_transform', config: { transform: 'upper' } });
  form.config!.transform = 'lower';
  expect(adapter.default_config.transform).toBe('upper');
});
