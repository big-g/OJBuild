import { useEffect, useState } from 'react';
import { cancelSourceSync, listSourceJobs, setSourceSchedule } from '../../lib/sources-api';
import type { SourceInstance, SourceJob } from '../../lib/sources-api';

export function SourceSyncControls({ source, refresh }: { source: SourceInstance; refresh: () => Promise<void> }) {
  const schedule = source.schedule ?? { revision: 0, enabled: false, interval_seconds: 3600, next_run_at: null };
  const [enabled, setEnabled] = useState(schedule.enabled);
  const [minutes, setMinutes] = useState(schedule.interval_seconds / 60);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [history, setHistory] = useState<SourceJob[] | null>(null);
  useEffect(() => {
    setEnabled(schedule.enabled); setMinutes(schedule.interval_seconds / 60);
  }, [schedule.revision, schedule.enabled, schedule.interval_seconds]);
  const perform = async (operation: () => Promise<unknown>, message: string) => {
    setBusy(true); setError(''); setNotice('');
    try { await operation(); await refresh(); setNotice(message); }
    catch (err) { setError(err instanceof Error ? err.message : 'Sync operation failed'); }
    finally { setBusy(false); }
  };
  const job = source.latest_job;
  const active = job?.state === 'queued' || job?.state === 'running';
  return <div className="flex flex-col gap-2" aria-label={`Sync controls for ${source.name}`}>
    {job && <div role="status">
      Latest run: {job.cancel_requested && active ? 'Cancellation requested' : job.state} · {job.phase}
      {` · ${job.documents_seen} documents${job.documents_total == null ? '' : ` of ${job.documents_total}`} · ${job.chunks_written} chunks written · ${job.pages_read} pages`}
      {job.error && <p>{job.error}</p>}
    </div>}
    {active && <button type="button" disabled={busy || job.cancel_requested || job.phase === 'committing'} onClick={() => void perform(() => cancelSourceSync(source.id), 'Cancellation requested. The current operation will finish at a safe boundary.')}>Cancel sync</button>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <form onSubmit={(event) => {
      event.preventDefault();
      void perform(() => setSourceSchedule(source.id, { revision: schedule.revision, enabled, interval_seconds: Math.round(minutes * 60) }), 'Schedule saved.');
    }}>
      <fieldset disabled={busy} className="flex flex-wrap gap-3">
        <label><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /> Scheduled sync</label>
        <label>Every (minutes) <input type="number" min={5} max={10080} step={1} required value={minutes} onChange={(event) => setMinutes(Number(event.target.value))} /></label>
        <button type="submit">Save schedule</button>
      </fieldset>
    </form>
    {schedule.enabled && <p>{source.enabled ? `Next scheduled run: ${schedule.next_run_at ? new Date(schedule.next_run_at).toLocaleString() : 'Pending'}` : 'Schedule paused while source is disabled.'}</p>}
    <button type="button" disabled={busy} onClick={() => {
      if (history) setHistory(null);
      else void perform(async () => setHistory((await listSourceJobs(source.id)).jobs), 'Run history loaded.');
    }}>{history ? 'Hide run history' : 'Run history'}</button>
    {history && <ul>{history.length ? history.map((run) => <li key={run.id}>{new Date(run.created_at).toLocaleString()} · {run.trigger} · {run.state} · {run.documents_seen} documents · {run.chunks_written} chunks{run.error ? ` · ${run.error}` : ''}</li>) : <li>No recorded runs.</li>}</ul>}
  </div>;
}
