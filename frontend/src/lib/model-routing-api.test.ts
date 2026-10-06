import { beforeEach, expect, it, vi } from 'vitest';
import { getModelRouting, runModelDiagnostic, saveTaskRule } from './model-routing-api';
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('./api', () => ({ apiFetch }));
beforeEach(() => { apiFetch.mockReset(); apiFetch.mockImplementation(async () => new Response('{}')); });
it('runs explicitly requested diagnostics through the authenticated API', async () => {
  await runModelDiagnostic('oj/server/model', 7, 'coding');
  expect(apiFetch).toHaveBeenCalledWith('/v1/model-routing/benchmarks', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ model_id: 'oj/server/model', connection_revision: 7, task: 'coding' }),
  }));
});
it('binds assignments to the reviewed rule and benchmark', async () => {
  await saveTaskRule({ task: 'vision', revision: 3, enabled: true, model_id: 'oj/server/model', benchmark_id: 'measured' });
  expect(apiFetch).toHaveBeenCalledWith('/v1/model-routing/tasks/vision', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ revision: 3, enabled: true, model_id: 'oj/server/model', benchmark_id: 'measured' }),
  }));
});
it('shows a safe stale-rule error', async () => {
  apiFetch.mockResolvedValue(new Response('{"detail":"Assignment changed"}', { status: 409 }));
  await expect(getModelRouting()).rejects.toThrow('Assignment changed');
});
