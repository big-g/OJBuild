import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { apiFetch, getStoredUser } = vi.hoisted(() => ({ apiFetch: vi.fn(), getStoredUser: vi.fn() }));
vi.mock('../lib/api', () => ({ apiFetch }));
vi.mock('../lib/auth', () => ({ getStoredUser }));
import { WorkflowsPage, workflowJson } from './WorkflowsPage';

describe('workflow controls', () => {
  beforeEach(() => { apiFetch.mockReset(); getStoredUser.mockReset(); });
  it('does not present administrative approval controls to ordinary users', () => {
    getStoredUser.mockReturnValue({ is_admin: false });
    const html = renderToStaticMarkup(<MemoryRouter><WorkflowsPage /></MemoryRouter>);
    expect(html).toContain('Run saved workflow');
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
});
