import { useEffect, useRef, useState } from 'react';
import { getStoredUser } from '../lib/auth';
import { fetchModels } from '../lib/api';
import { listModelConnections, type ModelConnection } from '../lib/model-connections-api';
import {
  getModelRouting, runModelDiagnostic, saveTaskRule, ROUTING_TASKS,
  type RoutingConfiguration, type RoutingTask, type TaskRule,
} from '../lib/model-routing-api';
import type { ModelInfo } from '../types';
import { routingModelOptions } from '../lib/model-routing-options';

export function ModelRoutingPanel({ connectionsRevision = 0 }: { connectionsRevision?: number }) {
  const admin = !!getStoredUser()?.is_admin;
  const [config, setConfig] = useState<RoutingConfiguration | null>(null);
  const [connections, setConnections] = useState<ModelConnection[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [task, setTask] = useState<RoutingTask>('general');
  const [selected, setSelected] = useState('');
  const [fallback, setFallback] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [inventoryWarning, setInventoryWarning] = useState('');
  const refreshSequence = useRef(0);
  async function refresh() {
    const sequence = ++refreshSequence.current;
    const [next, servers, installed] = await Promise.all([
      getModelRouting(), listModelConnections(), fetchModels().then(models => ({ models, failed: false })).catch(() => ({ models: [] as ModelInfo[], failed: true })),
    ]);
    if (sequence !== refreshSequence.current) return;
    setInventoryWarning(installed.failed ? 'Cannot load the existing engine inventory. Saved server catalogs are still shown; refresh to retry.' : '');
    setConfig(next); setConnections(servers.connections); setModels(installed.models);
  }
  useEffect(() => {
    if (admin) void refresh().catch(e => setError(String(e.message || e)));
    return () => { ++refreshSequence.current; };
  }, [admin, connectionsRevision]);
  if (!admin) return null;
  async function act(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await action(); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Operation failed'); await refresh().catch(() => {}); }
    finally { setBusy(false); }
  }
  const options = routingModelOptions(models, connections);
  const eligibleModels = options.filter(m => m.eligible);
  const model = eligibleModels.find(m => m.id === selected);
  const connection = connections.find(c => c.id === model?.connection_id);
  const rule = config?.rules.find(r => r.task === task);
  const latest = config?.benchmarks.find(b => b.model_id === selected && b.task === task);
  const ready = !!latest?.passed && latest.connection_revision === connection?.revision && latest.suite_version === config?.suite_version;
  const fallbackModel = eligibleModels.find(m => m.id === fallback);
  const fallbackConnection = connections.find(c => c.id === fallbackModel?.connection_id);
  const fallbackResult = config?.benchmarks.find(b => b.model_id === fallback && b.task === task);
  const fallbackReady = !fallback || (fallback !== selected && !!fallbackResult?.passed && fallbackResult.connection_revision === fallbackConnection?.revision && fallbackResult.suite_version === config?.suite_version);
  const label = (id: string) => options.find(m => m.id === id)?.display_name || id || 'unassigned';
  return <section className="rounded-lg border p-4 space-y-3" aria-label="Model task assignments">
    <h3 className="font-semibold">Model task assignments and diagnostics</h3>
    <p className="text-sm">{eligibleModels.length} ready for task assignments · {options.filter(m => !m.eligible).length} awaiting setup or unsupported.</p>
    {!eligibleModels.length && <p role="status" className="text-sm">Installed models need a saved server connection before diagnostics. In <a href="#model-server-connections" className="underline">Model server connections</a> above, add your Ollama URL, Test catalog, Read capabilities for your chat models, then Enable for chat. This list refreshes automatically after connection changes.</p>}
    <button type="button" disabled={busy} className="rounded border px-3 py-1" onClick={() => void act(async () => {})}>Refresh models</button>
    {inventoryWarning && <p role="status" className="text-sm">{inventoryWarning}</p>}
    <p className="text-sm">Assign shared models to general conversation, coding, analysis or vision. In chat, choose a task to use its assignment or Manual model to use the picker. Nothing is classified or switched automatically.</p>
    <details className="text-sm"><summary className="cursor-pointer">How diagnostics and assignments work</summary>
      <p>Diagnostics submit fixed synthetic prompts to the selected server. They use GPU time and may load a model. Each task has three fixed cases: coding checks mutation and boundaries; analysis checks weighted calculations and dependencies; general checks extraction and constraints; vision checks colors, position and region count. A tool-call canary also runs if Ollama reports tools. No generated code or tool call is executed, and no personal chat/source data is sent.</p>
      <p>These bounded behavioral checks are not a broad quality ranking. Elapsed time includes availability and generation overhead, not pure tokens per second. Compare results on the same hardware under similar load. A passing current result is required to enable an assignment. Connection changes or a newer result invalidate the previous assignment; review and save it again. Live tool/image capability checks and existing safety controls still apply.</p>
      <p>One diagnostic runs at a time on this API process, with bounded JSON reads and output budgets. Managed agents and their schedules can select the same task assignments or saved models in Intelligence. Optional fallback uses one separately measured model only if the primary transport is unavailable before inference starts. Unsupported capabilities, changed approvals and generation/stream failures never switch models.</p>
    </details>
    <div className="flex flex-wrap gap-3">
      <label>Task <select aria-label="Assignment task" disabled={busy} className="rounded border p-2 bg-transparent" value={task} onChange={e => setTask(e.target.value as RoutingTask)}>{ROUTING_TASKS.map(t => <option key={t} value={t}>{t}</option>)}</select></label>
      <label>Model <select aria-label="Assignment model" disabled={busy} className="rounded border p-2 bg-transparent" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Choose an enabled model</option>{options.map(m => <option key={m.id} value={m.id} disabled={!m.eligible}>{m.display_name || m.id}{m.setup_hint ? ` — ${m.setup_hint}` : ''}</option>)}</select></label>
      <label>Optional fallback <select aria-label="Assignment fallback" aria-describedby="fallback-hint" disabled={busy} className="rounded border p-2 bg-transparent" value={fallback} onChange={e => setFallback(e.target.value)}><option value="">No fallback</option>{eligibleModels.filter(m => m.id !== selected).map(m => <option key={m.id} value={m.id}>{m.display_name || m.id}</option>)}</select></label>
    </div>
    <p id="fallback-hint" className="text-sm">Run and review this task's diagnostic for each model first. Fallback is optional and defaults to off. A started run stays on its selected model.</p>
    <div className="flex flex-wrap gap-2">
      <button disabled={busy || !connection || !model} className="rounded border px-3 py-1" onClick={() => void act(async () => {
        const result = await runModelDiagnostic(selected, connection!.revision, task);
        setNotice(result.passed ? 'Diagnostic passed. Review its measurements, then enable the assignment.' : `Diagnostic failed: ${result.details.failure}`);
      })}>{busy ? 'Working…' : 'Run diagnostic'}</button>
      <button disabled={busy || !ready || !fallbackReady || !rule} className="rounded border px-3 py-1" onClick={() => void act(async () => {
        await saveTaskRule({ ...rule!, enabled: true, model_id: selected, benchmark_id: latest!.id, fallback_model_id: fallback, fallback_benchmark_id: fallbackResult?.id || '' });
        setNotice(`${task} assignment enabled.`);
      })}>Assign and enable</button>
    </div>
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    <ul className="text-sm space-y-2">{config?.rules.map(r => <li key={r.task}>
      <strong>{r.task}</strong>: {r.enabled ? 'enabled' : 'disabled'} · {label(r.model_id)}
      {r.fallback_model_id && <> · Fallback: {label(r.fallback_model_id)}</>}
      {r.enabled && <button disabled={busy} className="rounded border px-2 py-1 ml-2" onClick={() => void act(async () => {
        await saveTaskRule({ ...r, enabled: false } as TaskRule); setNotice(`${r.task} assignment disabled.`);
      })}>Disable</button>}
    </li>)}</ul>
    {!!config?.benchmarks.length && <details><summary className="cursor-pointer text-sm">Diagnostic history (latest 100)</summary>
      <ul className="text-sm space-y-2">{config.benchmarks.map(b => <li key={b.id}>
        {new Date(b.timestamp * 1000).toLocaleString()} · {b.task} · {label(b.model_id)} · {b.passed ? 'passed' : 'failed'} · {(b.elapsed_ms / 1000).toFixed(2)}s · {b.tokens} provider-reported tokens · connection revision {b.connection_revision} · {b.suite_version}
        {b.details.cases && <ul>{b.details.cases.map(c => <li key={c.name}>{c.name}: {c.passed ? 'passed' : 'failed'} · {(c.elapsed_ms / 1000).toFixed(2)}s</li>)}</ul>}
        {b.details.failure && <p>{b.details.failure}</p>}
      </li>)}</ul>
    </details>}
    {!!config?.audit.length && <details><summary className="cursor-pointer text-sm">Assignment history (latest 100)</summary><ul className="text-sm">{config.audit.map((event, i) => <li key={i}>{event.task} · revision {event.revision} · {event.event} · {event.actor} · {new Date(event.timestamp * 1000).toLocaleString()}</li>)}</ul></details>}
  </section>;
}
