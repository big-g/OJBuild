import { useEffect, useState } from 'react';
import { getStoredUser } from '../lib/auth';
import { fetchModels } from '../lib/api';
import { listModelConnections, type ModelConnection } from '../lib/model-connections-api';
import {
  getModelRouting, runModelDiagnostic, saveTaskRule, ROUTING_TASKS,
  type RoutingConfiguration, type RoutingTask, type TaskRule,
} from '../lib/model-routing-api';
import type { ModelInfo } from '../types';

export function ModelRoutingPanel() {
  const admin = !!getStoredUser()?.is_admin;
  const [config, setConfig] = useState<RoutingConfiguration | null>(null);
  const [connections, setConnections] = useState<ModelConnection[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [task, setTask] = useState<RoutingTask>('general');
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  async function refresh() {
    const [next, servers, installed] = await Promise.all([getModelRouting(), listModelConnections(), fetchModels()]);
    setConfig(next); setConnections(servers.connections);
    setModels(installed.filter(m => m.owned_by === 'configured_ollama'));
  }
  useEffect(() => { if (admin) void refresh().catch(e => setError(String(e.message || e))); }, [admin]);
  if (!admin) return null;
  async function act(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await action(); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Operation failed'); await refresh().catch(() => {}); }
    finally { setBusy(false); }
  }
  const model = models.find(m => m.id === selected);
  const connection = connections.find(c => c.id === model?.connection_id);
  const rule = config?.rules.find(r => r.task === task);
  const latest = config?.benchmarks.find(b => b.model_id === selected && b.task === task);
  const ready = !!latest?.passed && latest.connection_revision === connection?.revision && latest.suite_version === config?.suite_version;
  const label = (id: string) => models.find(m => m.id === id)?.display_name || id || 'unassigned';
  return <section className="rounded-lg border p-4 space-y-3" aria-label="Model task assignments">
    <h3 className="font-semibold">Model task assignments and diagnostics</h3>
    <p className="text-sm">Assign shared models to general conversation, coding, analysis or vision. In chat, choose a task to use its assignment or Manual model to use the picker. Nothing is classified or switched automatically.</p>
    <details className="text-sm"><summary className="cursor-pointer">How diagnostics and assignments work</summary>
      <p>Diagnostics submit fixed synthetic prompts to the selected server. They use GPU time and may load a model. Coding checks small code-tracing answers; analysis checks short logic/arithmetic; general checks instructions; vision checks a red image. A tool-call canary also runs if Ollama reports tools. No generated code or tool call is executed, and no personal chat/source data is sent.</p>
      <p>These are small diagnostic probes, not a broad quality ranking. Elapsed time includes availability and generation overhead, not pure tokens per second. Compare results on the same hardware under similar load. A passing current result is required to enable an assignment. Connection changes or a newer result invalidate the previous assignment; review and save it again. Live tool/image capability checks and existing safety controls still apply.</p>
      <p>One diagnostic runs at a time on this API process, with bounded JSON reads and output budgets. Deep Research and scheduled agents retain their existing model configuration; task selection here applies to standard chat. No automatic fallback is configured.</p>
    </details>
    <div className="flex flex-wrap gap-3">
      <label>Task <select aria-label="Assignment task" disabled={busy} className="rounded border p-2 bg-transparent" value={task} onChange={e => setTask(e.target.value as RoutingTask)}>{ROUTING_TASKS.map(t => <option key={t} value={t}>{t}</option>)}</select></label>
      <label>Model <select aria-label="Assignment model" disabled={busy} className="rounded border p-2 bg-transparent" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Choose an enabled model</option>{models.map(m => <option key={m.id} value={m.id}>{m.display_name || m.id}</option>)}</select></label>
    </div>
    <div className="flex flex-wrap gap-2">
      <button disabled={busy || !connection || !model} className="rounded border px-3 py-1" onClick={() => void act(async () => {
        const result = await runModelDiagnostic(selected, connection!.revision, task);
        setNotice(result.passed ? 'Diagnostic passed. Review its measurements, then enable the assignment.' : `Diagnostic failed: ${result.details.failure}`);
      })}>{busy ? 'Working…' : 'Run diagnostic'}</button>
      <button disabled={busy || !ready || !rule} className="rounded border px-3 py-1" onClick={() => void act(async () => {
        await saveTaskRule({ ...rule!, enabled: true, model_id: selected, benchmark_id: latest!.id });
        setNotice(`${task} assignment enabled.`);
      })}>Assign and enable</button>
    </div>
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    <ul className="text-sm space-y-2">{config?.rules.map(r => <li key={r.task}>
      <strong>{r.task}</strong>: {r.enabled ? 'enabled' : 'disabled'} · {label(r.model_id)}
      {r.enabled && <button disabled={busy} className="rounded border px-2 py-1 ml-2" onClick={() => void act(async () => {
        await saveTaskRule({ ...r, enabled: false } as TaskRule); setNotice(`${r.task} assignment disabled.`);
      })}>Disable</button>}
    </li>)}</ul>
    {!!config?.benchmarks.length && <details><summary className="cursor-pointer text-sm">Diagnostic history (latest 100)</summary>
      <ul className="text-sm space-y-2">{config.benchmarks.map(b => <li key={b.id}>
        {new Date(b.timestamp * 1000).toLocaleString()} · {b.task} · {label(b.model_id)} · {b.passed ? 'passed' : 'failed'} · {(b.elapsed_ms / 1000).toFixed(2)}s · {b.tokens} provider-reported tokens · connection revision {b.connection_revision} · {b.suite_version}
        {b.details.failure && <p>{b.details.failure}</p>}
      </li>)}</ul>
    </details>}
    {!!config?.audit.length && <details><summary className="cursor-pointer text-sm">Assignment history (latest 100)</summary><ul className="text-sm">{config.audit.map((event, i) => <li key={i}>{event.task} · revision {event.revision} · {event.event} · {event.actor} · {new Date(event.timestamp * 1000).toLocaleString()}</li>)}</ul></details>}
  </section>;
}
