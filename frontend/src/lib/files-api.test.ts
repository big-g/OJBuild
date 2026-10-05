import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { deleteFile, downloadFile, listFiles, previewFile, saveFile, suggestedFilename } from './files-api';
import { apiFetch } from './api';

vi.mock('./api', () => ({ apiFetch: vi.fn() }));
const call = vi.mocked(apiFetch);
beforeEach(() => call.mockReset());
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const file = { id: 'id', filename: 'script.html', size: 10, sha256: 'hash', created_at: 'date' };
describe('generated file API', () => {
  it('uses authenticated API requests and never accepts an owner parameter', async () => {
    call.mockResolvedValueOnce(Response.json(file));
    expect(await saveFile('script.html', '<script>bad()</script>')).toEqual(file);
    const [path, options] = call.mock.calls[0];
    expect(path).toBe('/v1/files');
    expect(JSON.parse(String(options?.body))).toEqual({ filename: 'script.html', content: '<script>bad()</script>', encoding: 'utf8' });
    call.mockResolvedValueOnce(Response.json({ files: [file] }));
    expect(await listFiles()).toEqual([file]);
    call.mockResolvedValueOnce(Response.json({ ...file, kind: 'text' }));
    await previewFile('a/b');
    expect(call).toHaveBeenLastCalledWith('/v1/files/a%2Fb/preview');
    call.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await deleteFile('id');
    expect(call).toHaveBeenLastCalledWith('/v1/files/id', { method: 'DELETE' });
  });
  it('surfaces server quota and ownership failures', async () => {
    call.mockResolvedValue(Response.json({ detail: 'File not found' }, { status: 404 }));
    await expect(previewFile('other-user')).rejects.toThrow('File not found');
  });
  it('downloads a fetched blob with attachment semantics without rendering it', async () => {
    const link = { href: '', download: '', click: vi.fn(), remove: vi.fn() };
    const append = vi.fn();
    vi.stubGlobal('document', { createElement: vi.fn(() => link), body: { appendChild: append } });
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:private-download');
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    vi.useFakeTimers();
    try {
      call.mockResolvedValue(new Response('<script>bad()</script>'));
      await downloadFile(file);
      expect(call).toHaveBeenCalledWith('/v1/files/id/download');
      expect(link.href).toBe('blob:private-download');
      expect(link.download).toBe('script.html');
      expect(link.click).toHaveBeenCalledOnce();
      expect(link.remove).toHaveBeenCalledOnce();
      vi.runAllTimers();
      expect(revoke).toHaveBeenCalledWith('blob:private-download');
    } finally { vi.useRealTimers(); }
  });
  it('suggests useful filenames and safely defaults unknown languages', () => {
    expect(suggestedFilename('python')).toBe('generated.py');
    expect(suggestedFilename('STL')).toBe('generated.stl');
    expect(suggestedFilename('../evil')).toBe('generated.txt');
  });
});
