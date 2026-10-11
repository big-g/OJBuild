import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { apiFetch, getStoredUser } = vi.hoisted(() => ({ apiFetch: vi.fn(), getStoredUser: vi.fn() }));
vi.mock('../lib/api', () => ({ apiFetch }));
vi.mock('../lib/auth', () => ({ getStoredUser }));
import { WorkflowsPage, submitSavedWorkflow, workflowJson } from './WorkflowsPage';

describe('workflow controls', () => {
  beforeEach(() => { apiFetch.mockReset(); getStoredUser.mockReset(); });
  it('does not present administrative approval controls to ordinary users', () => {
    getStoredUser.mockReturnValue({ is_admin: false });
    const html = renderToStaticMarkup(<MemoryRouter><WorkflowsPage /></MemoryRouter>);
    expect(html).toContain('Run saved workflow');
    expect(html).toContain('Editing step settings requires saving again');
    expect(html).toContain('up to 20 MiB');
    expect(html).toContain('target dimension in millimetres');
    expect(html).not.toContain('Administrator: operations');
  });
  it('explains local server formats and permissions to administrators', () => {
    getStoredUser.mockReturnValue({ is_admin: true });
    const html = renderToStaticMarkup(<MemoryRouter><WorkflowsPage /></MemoryRouter>);
    expect(html).toContain('Configuration alone does not enable a server');
    expect(html).toContain('http://127.0.0.1:8188');
    expect(html).toContain('SD/SDXL only');
  });
  it('reports HTTP failures even if their bodies are valid JSON', async () => {
    apiFetch.mockResolvedValue(new Response(JSON.stringify({ detail: 'Workflow changed; reload' }), { status: 409 }));
    await expect(workflowJson('/runs')).rejects.toThrow('Workflow changed; reload');
  });
  it('reports non-JSON proxy errors instead of accepting an empty job', async () => {
    apiFetch.mockResolvedValue(new Response('<html>Error</html>', { status: 502 }));
    await expect(workflowJson('/runs')).rejects.toThrow('invalid response');
  });
  it('returns distinct accepted runs when rerunning the same saved workflow', async () => {
    const saved = { id: 'saved-image', revision: 2, definition: { version: 1 as const, name: 'Image test', steps: [] } };
    const first = { id: 'run-one', status: 'queued', definition: saved.definition, steps: [] };
    const second = { ...first, id: 'run-two' };
    apiFetch.mockResolvedValueOnce(new Response(JSON.stringify(first), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(second), { status: 202 }));
    expect(await submitSavedWorkflow(saved, { prompt: 'red mug' })).toEqual(first);
    expect(await submitSavedWorkflow(saved, { prompt: 'blue mug' })).toEqual(second);
    expect(apiFetch).toHaveBeenCalledTimes(2);
    expect(JSON.parse(apiFetch.mock.calls[1][1].body)).toEqual({ workflow_id: 'saved-image', revision: 2, input: { prompt: 'blue mug' } });
  });
  it('does not silently retry an incomplete submission acknowledgement', async () => {
    apiFetch.mockResolvedValue(new Response('{}', { status: 202 }));
    await expect(submitSavedWorkflow({ id: 'saved', revision: 1, definition: { version: 1, name: 'Test', steps: [] } }, {})).rejects.toThrow('Reload your run history before retrying');
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });
});
