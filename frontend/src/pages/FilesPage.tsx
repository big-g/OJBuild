import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router';
import { FilePreview } from '../components/Files/FilePreview';
import { deleteFile, downloadFile, listFiles, previewFile } from '../lib/files-api';
import type { GeneratedFile, FilePreview as Preview } from '../lib/files-api';

export function FilesPage() {
  const [files, setFiles] = useState<GeneratedFile[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [params, setParams] = useSearchParams();
  const selected = params.get('file');
  const refresh = async () => setFiles(await listFiles());
  useEffect(() => {
    let cancelled = false;
    listFiles().then(rows => { if (!cancelled) setFiles(rows); })
      .catch(err => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    setError('');
    if (selected) previewFile(selected).then(row => { if (!cancelled) setPreview(row); })
      .catch(err => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, [selected]);
  const action = async (callback: () => Promise<void>) => {
    setBusy(true); setError('');
    try { await callback(); } catch (err: any) { setError(err.message); }
    finally { setBusy(false); }
  };
  return <div className="p-6 max-w-6xl mx-auto" style={{ color: 'var(--color-text-primary)' }}>
    <h1 className="text-2xl font-semibold mb-3">My files</h1>
    <p className="text-sm mb-4">Files are stored on your OpenJarvis server and belong to your account. Ask Jarvis to save a file, or use Save file on a chat code block. Limit: 20 MiB per file, 100 files and 200 MiB per account.</p>
    <button disabled={busy || loading} onClick={() => void action(refresh)} className="border rounded px-3 py-1 mb-4">Refresh files</button>
    {error && <p role="alert" className="mb-3">{error}</p>}
    {loading ? <p>Loading files…</p> : files.length === 0 && <p>No saved files yet.</p>}
    <div className="grid gap-5 md:grid-cols-[240px_1fr]">
      <ul className="space-y-2">
        {files.map(file => <li key={file.id}>
          <button onClick={() => setParams({ file: file.id })}
            className="text-left border rounded p-3 w-full break-all"
            aria-pressed={file.id === selected}>
            {file.filename}<span className="block text-xs">{file.size.toLocaleString()} bytes · {new Date(file.created_at).toLocaleString()}</span>
          </button>
        </li>)}
      </ul>
      {preview && <section className="min-w-0">
        <h2 className="font-semibold break-all mb-2">{preview.filename}</h2>
        <p className="text-xs break-all mb-4">SHA-256: {preview.sha256}</p>
        <FilePreview preview={preview} />
        <div className="flex gap-3 mt-4">
          <button disabled={busy} onClick={() => void action(() => downloadFile(preview))}
            className="border rounded px-3 py-2">Download file</button>
          <button disabled={busy} onClick={() => {
            if (window.confirm(`Delete ${preview.filename}?`)) void action(async () => {
              await deleteFile(preview.id); setParams({}); await refresh();
            });
          }} className="border rounded px-3 py-2">Delete file</button>
        </div>
      </section>}
    </div>
  </div>;
}
