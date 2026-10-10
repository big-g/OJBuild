import { useEffect, useRef, useState } from 'react';
import { apiFetch } from '../lib/api';
import { read3DJson, checked3DDownload } from '../lib/hunyuan-response';

interface Job {
  job_id: string; status: string; model: string; watertight?: boolean;
  vertices?: number; faces?: number; elapsed_seconds?: number; error?: string; background_model?: string | null; chat_mode?: string;
}

export function ThreeDPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState('');
  const [model, setModel] = useState('turbo');
  const modelChosen = useRef(false);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => {
    if (!file) { setPreview(''); return; }
    const url = URL.createObjectURL(file); setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const body = await read3DJson<{ jobs: Job[]; default_model?: string }>(await apiFetch('/v1/3d/jobs'));
        if (!Array.isArray(body.jobs)) throw new Error('3D API returned an invalid job list.');
        if (!cancelled) { setJobs(body.jobs); if (!modelChosen.current && body.default_model) { setModel(body.default_model); modelChosen.current = true; } }
      } catch (e) { if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load jobs'); }
    };
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);
  const submit = async () => {
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) { setError('Choose an image below 20 MiB.'); return; }
    setSubmitting(true); setError('');
    try {
      const job = await read3DJson<Job>(await apiFetch(`/v1/3d/jobs?model=${model}`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
      }));
      setJobs(rows => [job, ...rows.filter(row => row.job_id !== job.job_id)]);
    } catch (e) { setError(e instanceof Error ? e.message : 'Generation could not start'); }
    finally { setSubmitting(false); }
  };
  const download = async (job: Job) => {
    try {
      const response = await checked3DDownload(await apiFetch(`/v1/3d/jobs/${job.job_id}/download`));
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = `${job.job_id}.glb`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setError(e instanceof Error ? e.message : 'Download failed'); }
  };
  const running = jobs.some(job => job.status === 'running');
  return <main className="p-6 max-w-4xl mx-auto space-y-5">
    <h1 className="text-2xl font-semibold">3D Generation</h1>
    <p>Turn a reference image into a local 3D mesh. Use a single object on a transparent background for best results.</p>
    <p className="text-sm text-muted-foreground">During generation, local chat uses the alternate model configured in administrator settings when GPU memory permits. The normal model returns afterward. Textures are not included.</p>
    {error && <p role="alert" className="text-red-500">{error}</p>}
    <div className="rounded-lg border p-4 space-y-4">
      <label className="block">Reference image (PNG or JPEG, up to 20 MiB)
        <input className="block mt-2" type="file" accept="image/png,image/jpeg" onChange={e => setFile(e.target.files?.[0] || null)} />
      </label>
      {preview && <img src={preview} alt="Reference for generation" className="max-h-64 rounded border object-contain" />}
      <label className="block">Model
        <select className="block mt-2 border rounded p-2 bg-background" value={model} onChange={e => { modelChosen.current = true; setModel(e.target.value); }}>
          <option value="standard">Standard — higher quality</option>
          <option value="turbo">Turbo — faster, lower GPU memory</option>
        </select>
      </label>
      <button className="rounded bg-primary text-primary-foreground px-4 py-2 disabled:opacity-50" disabled={!file || submitting || running} onClick={() => void submit()}>
        {submitting ? 'Starting…' : running ? 'Generation running…' : 'Generate mesh'}
      </button>
    </div>
    <h2 className="text-lg font-semibold">Your jobs</h2>
    {!jobs.length && <p>No jobs yet.</p>}
    {jobs.map(job => <article key={job.job_id} className="rounded-lg border p-4 space-y-2">
      <p className="font-medium">{job.model === 'turbo' ? 'Turbo' : 'Standard'} · {job.status}</p>
      <p className="text-xs text-muted-foreground break-all">{job.job_id}</p>
      {job.status === 'running' && <p>Generating your mesh. Status updates automatically.</p>}
      {job.status === 'running' && <p>{job.background_model ? `Chat continues using ${job.background_model} with a reduced context window.` : 'Local chat is paused because there is insufficient GPU memory for a second model.'}</p>}
      {job.error && <p role="alert">{job.error}</p>}
      {job.status === 'completed' && <>
        <p>{job.vertices?.toLocaleString()} vertices · {job.faces?.toLocaleString()} faces · {Math.round(job.elapsed_seconds || 0)} seconds</p>
        <p>{job.watertight ? 'Watertight mesh. Check dimensions and printability in your slicer.' : 'Mesh is not watertight. Repair may be needed before printing.'}</p>
        <button className="rounded border px-3 py-2" onClick={() => void download(job)}>Download GLB</button>
      </>}
    </article>)}
  </main>;
}
