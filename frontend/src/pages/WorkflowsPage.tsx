import { ConfigurationHelp, configurationSummary } from '../components/ConfigurationHelp';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { apiFetch } from '../lib/api';
import { getStoredUser } from '../lib/auth';

type Property = { type?: string; title?: string; default?: unknown; enum?: string[]; minimum?: number; maximum?: number; description?: string };
type Operation = { id: string; description: string; enabled: boolean; capabilities: string[]; parameters: { properties: Record<string, Property>; required?: string[] } };
type Step = { id: string; operation: string; input: string; parameters?: Record<string, unknown> };
type Definition = { version: 1; name: string; steps: Step[] };
type Saved = { id: string; revision: number; definition: Definition };
type Artifact = { id: string; filename: string; size: number };
type Value = { artifact_id?: string; artifact?: Artifact; report_artifact?: Artifact; units?: string; report?: { after?: { dimensions?: number[]; watertight?: boolean; components?: number } } };
type Run = { id: string; status: string; definition: Definition; output?: Value; error?: string; cancel_requested?: boolean; steps: { id: string; operation: string; status: string; error?: string; failure?: { category: string; recovery: string }; output?: Value; elapsed_seconds?: number }[] };
type Server = { id: string; revision: number; name: string; enabled: boolean; url?: string; checkpoint?: string };
type Catalog = { workflows: Saved[]; operations: Operation[]; templates: Definition[]; image_servers: Server[] };

export async function workflowJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await apiFetch(`/v1/workflows${path}`, options);
  let value: { detail?: string };
  try { value = await response.json(); } catch { throw new Error('The workflow service returned an invalid response.'); }
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'Workflow request failed.');
  return value as T;
}
const post = (value: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });

export async function submitSavedWorkflow(saved: Saved, input: { artifact_id?: string; prompt?: string }): Promise<Run> {
  const result = await workflowJson<Run>('/runs', post({ workflow_id: saved.id, revision: saved.revision, input }));
  if (!result.id || !result.status || !result.definition || !Array.isArray(result.steps)) {
    throw new Error('The server did not return a valid workflow run. Reload your run history before retrying.');
  }
  return result;
}

function ReferenceImage({ artifact }: { artifact: Artifact }) {
  const [url, setUrl] = useState('');
  useEffect(() => {
    let disposed = false; let objectUrl = '';
    void apiFetch(`/v1/files/${artifact.id}/download`).then(async response => {
      if (!response.ok) return;
      objectUrl = URL.createObjectURL(await response.blob());
      if (disposed) URL.revokeObjectURL(objectUrl); else setUrl(objectUrl);
    }).catch(() => {});
    return () => { disposed = true; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [artifact.id]);
  return url ? <img src={url} alt="Generated reference" className="max-h-64 rounded border" /> : null;
}

export function WorkflowsPage() {
  const [searchParams] = useSearchParams();
  const [catalog, setCatalog] = useState<Catalog>({ workflows: [], operations: [], templates: [], image_servers: [] });
  const [runs, setRuns] = useState<Run[]>([]);
  const [files, setFiles] = useState<Artifact[]>([]);
  const [definition, setDefinition] = useState<Definition>({ version: 1, name: 'My workflow', steps: [] });
  const [saved, setSaved] = useState<Saved | null>(null);
  const [source, setSource] = useState(searchParams.get('file') || '');
  const [prompt, setPrompt] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refreshWarning, setRefreshWarning] = useState('');
  const pendingRefresh = useRef<Promise<void> | null>(null);
  const refreshAbort = useRef<AbortController | null>(null);
  const actionActive = useRef(false);
  const [busy, setBusy] = useState(false);
  const [acceptedRunId, setAcceptedRunId] = useState('');
  const refreshSequence = useRef(0);
  const [servers, setServers] = useState<Server[]>([]);
  const [serverName, setServerName] = useState('Local image server');
  const [serverUrl, setServerUrl] = useState('http://127.0.0.1:8188');
  const [checkpoint, setCheckpoint] = useState('');
  const [editingServer, setEditingServer] = useState<Server | null>(null);
  const isAdmin = getStoredUser()?.is_admin === true;
  const refresh = useCallback((): Promise<void> => {
    if (pendingRefresh.current) return pendingRefresh.current;
    const sequence = ++refreshSequence.current;
    const controller = new AbortController();
    refreshAbort.current = controller;
    const options = { signal: controller.signal };
    const task = (async () => {
      const results = await Promise.allSettled([
        workflowJson<Catalog>('', options).then(next => {
          if (sequence === refreshSequence.current && !controller.signal.aborted) setCatalog(next);
        }),
        workflowJson<{ runs: Run[] }>('/runs', options).then(history => {
          if (sequence === refreshSequence.current && !controller.signal.aborted) setRuns(history.runs);
        }),
        apiFetch('/v1/files', options).then(async response => {
          if (!response.ok) throw new Error('Could not load your files.');
          const result = await response.json() as { files: Artifact[] };
          if (sequence === refreshSequence.current && !controller.signal.aborted) setFiles(result.files);
        }),
        ...(isAdmin ? [workflowJson<{ image_servers: Server[] }>('/admin', options).then(settings => {
          if (sequence === refreshSequence.current && !controller.signal.aborted) setServers(settings.image_servers);
        })] : []),
      ]);
      if (sequence !== refreshSequence.current || controller.signal.aborted) return;
      const failed = results.filter(result => result.status === 'rejected').length;
      setRefreshWarning(failed ? 'Some sections could not refresh. Showing the last available data; updates will retry automatically.' : '');
    })().finally(() => {
      if (pendingRefresh.current === task) pendingRefresh.current = null;
    });
    pendingRefresh.current = task;
    return task;
  }, [isAdmin]);
  useEffect(() => {
    if (acceptedRunId) document.getElementById(`workflow-run-${acceptedRunId}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [acceptedRunId]);
  const pollingDelay = runs.some(run => ['queued', 'running'].includes(run.status)) ? 5000 : 15000;
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (active) timer = setTimeout(() => { void poll(); }, pollingDelay);
    };
    void poll();
    return () => {
      active = false; clearTimeout(timer);
      refreshSequence.current++;
      refreshAbort.current?.abort();
      pendingRefresh.current = null;
    };
  }, [refresh, pollingDelay]);
  const action = async (fn: () => Promise<void>) => {
    if (actionActive.current) return;
    actionActive.current = true;
    setBusy(true); setError(''); setNotice('');
    try { await fn(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Action failed.'); }
    finally { actionActive.current = false; setBusy(false); }
    // A refresh failure never changes the outcome of an accepted action.
    void refresh();
  };
  const changeStep = (index: number, changes: Partial<Step>) => {
    setSaved(null);
    setDefinition(current => ({ ...current, steps: current.steps.map((step, i) => i === index ? { ...step, ...changes } : step) }));
  };
  const moveStep = (index: number, offset: number) => {
    const steps = [...definition.steps];
    [steps[index], steps[index + offset]] = [steps[index + offset], steps[index]];
    setSaved(null); setDefinition({ ...definition, steps });
  };
  // Preserve the editing identity separately from the saved-for-execution revision.
  const [editing, setEditing] = useState<Saved | null>(null);
  const load = (value: Definition, identity: Saved | null = null) => {
    setDefinition(structuredClone(value)); setEditing(identity); setSaved(identity); setError(''); setNotice('');
  };
  const save = () => action(async () => {
    const result = await workflowJson<Saved>('', post({ definition, ...(editing ? { id: editing.id, revision: editing.revision } : {}) }));
    setEditing(result); setSaved(result); setNotice('Workflow saved.');
  });
  const run = () => action(async () => {
    if (!saved) throw new Error('Save the workflow before running it.');
    const accepted = await submitSavedWorkflow(saved,
      { ...(source ? { artifact_id: source } : {}), ...(prompt ? { prompt } : {}) });
    // Prevent an older in-flight poll from hiding the just-accepted run.
    refreshSequence.current++;
    setRuns(current => [accepted, ...current.filter(row => row.id !== accepted.id)]);
    setAcceptedRunId(accepted.id);
    setNotice(`Run ${accepted.id} accepted. Status updates below; the server continues when you leave this page.`);
  });
  const upload = (file: File) => action(async () => {
    if (file.size > 20 * 1024 * 1024) throw new Error('Choose a file below 20 MiB.');
    const content = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.onerror = () => reject(new Error('Could not read the file.')); reader.readAsDataURL(file);
    });
    const response = await apiFetch('/v1/files', { ...post({ filename: file.name, encoding: 'base64', content }) });
    const result = await response.json() as Artifact & { detail?: string };
    if (!response.ok) throw new Error(result.detail || 'Upload failed.');
    setSource(result.id); setNotice('Original file saved. Workflow operations create separate output files.');
  });
  const download = (artifact: Artifact) => action(async () => {
    const response = await apiFetch(`/v1/files/${artifact.id}/download`);
    if (!response.ok) throw new Error('Download unavailable.');
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = artifact.filename;
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const addStep = () => {
    const operation = catalog.operations[0]; if (!operation) return;
    let index = definition.steps.length + 1;
    while (definition.steps.some(step => step.id === `step${index}`)) index++;
    setSaved(null); setDefinition(current => ({ ...current, steps: [...current.steps, {
      id: `step${index}`, operation: operation.id, input: current.steps[current.steps.length - 1]?.id || 'input', parameters: {},
    }] }));
  };
  const parameterDefaults = (operation: Operation) => Object.fromEntries(Object.entries(operation.parameters.properties).map(([key, property]) => [key,
    property.default ?? (key === 'instance_id' ? catalog.image_servers.find(server => server.enabled)?.id || '' : property.enum?.[0] ?? (property.type === 'boolean' ? false : property.type === 'integer' || property.type === 'number' ? property.minimum ?? 1 : ''))]));
  return <main className="p-6 max-w-5xl mx-auto space-y-5">
    <h1 className="text-2xl font-semibold">Generation &amp; Workflows</h1>
    <p>Save and rerun ordered operations. Each step receives the original input or a previous step’s output. Jobs continue on the server when this page is closed.</p>
    {error && <p role="alert" className="text-red-500">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {refreshWarning && <p role="status">{refreshWarning}</p>}
    <fieldset disabled={busy} aria-label="Workflow editor" className="border rounded p-4 space-y-3">
      <h2 className="text-lg font-semibold">Choose or create a workflow</h2>
      <div className="flex flex-wrap gap-2">
        <button disabled={busy} className="border rounded px-3 py-2" onClick={() => load({ version: 1, name: 'My workflow', steps: [] })}>New workflow</button>
        {catalog.templates.map(template => <button disabled={busy} key={template.name} className="border rounded px-3 py-2" onClick={() => load(template)}>{template.name}</button>)}
      </div>
      <label className="block">Saved workflows<select className="block border rounded p-2 bg-background" value={editing?.id || ''} onChange={e => { const row = catalog.workflows.find(w => w.id === e.target.value); if (row) load(row.definition, row); }}><option value="">Choose a saved workflow</option>{catalog.workflows.map(w => <option key={w.id} value={w.id}>{w.definition.name} · revision {w.revision}</option>)}</select></label>
      <label className="block">Name<input maxLength={100} className="block border rounded p-2 w-full bg-background" value={definition.name} onChange={e => { setSaved(null); setDefinition({ ...definition, name: e.target.value }); }} /></label>
      {definition.steps.map((step, index) => {
        const operation = catalog.operations.find(o => o.id === step.operation);
        return <fieldset key={step.id} className="border rounded p-3 space-y-2">
          <legend>Step {index + 1}: {step.id}</legend>
          <label className="block">Operation<select className="block border rounded p-2 bg-background" value={step.operation} onChange={e => { const selected = catalog.operations.find(o => o.id === e.target.value); if (selected) changeStep(index, { operation: selected.id, parameters: parameterDefaults(selected) }); }}>{catalog.operations.map(o => <option key={o.id} value={o.id}>{o.description}{!o.enabled ? ' · approval needed' : ''}</option>)}</select></label>
          <label className="block">Input from<select className="block border rounded p-2 bg-background" value={step.input} onChange={e => changeStep(index, { input: e.target.value })}><option value="input">Original input</option>{definition.steps.slice(0, index).map(previous => <option key={previous.id} value={previous.id}>{previous.id}</option>)}</select></label>
          {Object.entries(operation?.parameters.properties || {}).map(([key, property]) => {
            const value = step.parameters?.[key] ?? property.default ?? '';
            const hint = [property.minimum !== undefined ? `Minimum ${property.minimum}` : '', property.maximum !== undefined ? `maximum ${property.maximum}` : '', key === 'target_mm' ? 'millimetres; uniform scaling preserves proportions' : '', key === 'voxel_size' ? 'Uses the input mesh units. Smaller values retain more detail and use more memory; remeshing may alter shape.' : '', key === 'keep_largest' ? 'Removes other disconnected components when enabled.' : ''].filter(Boolean).join('; ');
            const set = (next: unknown) => changeStep(index, { parameters: { ...step.parameters, [key]: next } });
            return <div key={key} className="block text-sm"><label htmlFor={`${step.id}-${key}`}>{property.title || key}{operation?.parameters.required?.includes(key) ? ' (required)' : ''}</label>
              {key === 'instance_id' ? <select id={`${step.id}-${key}`} className="block border rounded p-2 bg-background" value={String(value)} onChange={e => set(e.target.value)}><option value="">Choose an enabled image server</option>{catalog.image_servers.filter(s => s.enabled).map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select>
              : property.enum ? <select id={`${step.id}-${key}`} className="block border rounded p-2 bg-background" value={String(value)} onChange={e => set(e.target.value)}>{property.enum.map(option => <option key={option}>{option}</option>)}</select>
              : property.type === 'boolean' ? <input id={`${step.id}-${key}`} type="checkbox" className="ml-2" checked={Boolean(value)} onChange={e => set(e.target.checked)} />
              : <input id={`${step.id}-${key}`} aria-describedby={hint ? `${step.id}-${key}-hint` : undefined} type={property.type === 'integer' || property.type === 'number' ? 'number' : 'text'} min={property.minimum} max={property.maximum} step={property.type === 'integer' ? 1 : 'any'} className="block border rounded p-2 bg-background" value={String(value)} onChange={e => set(property.type === 'integer' || property.type === 'number' ? Number(e.target.value) : e.target.value)} />}
              {hint && <ConfigurationHelp id={`${step.id}-${key}-hint`} title={property.title || key} summary={configurationSummary(hint)}>{hint}</ConfigurationHelp>}
            </div>;
          })}
          <button disabled={busy} className="underline" onClick={() => { setSaved(null); setDefinition({ ...definition, steps: definition.steps.filter((_, i) => i !== index) }); }}>Remove step</button>
          <button disabled={busy || index === 0} className="underline ml-3 disabled:opacity-50" onClick={() => moveStep(index, -1)}>Move up</button>
          <button disabled={busy || index === definition.steps.length - 1} className="underline ml-3 disabled:opacity-50" onClick={() => moveStep(index, 1)}>Move down</button>
        </fieldset>;
      })}
      <div className="flex gap-3"><button disabled={busy || definition.steps.length >= 12} className="border rounded px-3 py-2" onClick={addStep}>Add step</button><button disabled={busy || !definition.steps.length} className="border rounded px-3 py-2" onClick={() => void save()}>Save workflow</button>{editing && <button disabled={busy} className="underline" onClick={() => void action(async () => { await workflowJson(`/${editing.id}?revision=${editing.revision}`, { method: 'DELETE' }); load({ version: 1, name: 'My workflow', steps: [] }); })}>Delete saved workflow</button>}</div>
      <p className="text-sm text-muted-foreground">Removing a step may require updating later input handoffs before saving. To make a variant, choose New workflow or start from a template.</p>
    </fieldset>
    <section className="border rounded p-4 space-y-3">
      <h2 className="text-lg font-semibold">Run against your input</h2>
      <label className="block">Text prompt (up to 4000 characters)<textarea maxLength={4000} className="block border rounded p-2 w-full bg-background" value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="A single toy robot, isolated on a plain background, front three-quarter view" /></label>
      <label className="block">Existing file<select className="block border rounded p-2 bg-background" value={source} onChange={e => setSource(e.target.value)}><option value="">No input file</option>{files.map(file => <option key={file.id} value={file.id}>{file.filename} · {file.id.slice(0, 8)}</option>)}</select></label>
      <label className="block">Upload a GLB or STL (up to 20 MiB)<input type="file" accept=".glb,.stl" disabled={busy} className="block mt-1" onChange={e => { if (e.target.files?.[0]) void upload(e.target.files[0]); e.target.value = ''; }} /></label>
      <p className="text-sm text-muted-foreground">Completed Hunyuan outputs can be imported from <Link className="underline" to="/3d">3D Generation</Link>. For STL, include a scale step specifying the target dimension in millimetres. A watertight mesh still needs slicer inspection.</p>
      <button disabled={busy || !saved} className="border rounded px-4 py-2 disabled:opacity-50" onClick={() => void run()}>Run saved workflow</button>
      {!saved && <p className="text-sm text-muted-foreground">Choose a saved workflow, or save your changes, to enable running. Editing step settings requires saving again; changing the text prompt or input file does not.</p>}
    </section>
    {isAdmin && <details className="border rounded p-4 space-y-3">
      <summary className="font-semibold cursor-pointer">Administrator: operations and local image servers</summary>
      <ConfigurationHelp title="Operation approval" summary="Approval grants permission to run a workflow operation.">Operations start disabled. Approval authorizes the listed permissions for workflow execution; any existing server capability policy also applies. Configuration alone does not enable a server.</ConfigurationHelp>
      {catalog.operations.map(operation => <div key={operation.id} className="border-b py-2"><div>{operation.id} <ConfigurationHelp title={operation.id} summary={configurationSummary(operation.description)}>{operation.description}</ConfigurationHelp></div><p className="text-xs">Required permissions: {operation.capabilities.join(', ')}</p><button disabled={busy} className="underline" onClick={() => void action(async () => { await workflowJson(`/admin/operations/${operation.id}`, post({ enabled: !operation.enabled })); })}>{operation.enabled ? 'Disable' : 'Approve and enable'}</button></div>)}
      <ConfigurationHelp title="Local image server setup" summary="Connect an installed local ComfyUI worker and checkpoint.">Connect a private local ComfyUI server with an installed Stable Diffusion or SDXL checkpoint. No weights are downloaded by OpenJarvis. Image generation pauses local chat; Hunyuan keeps its existing alternate-model policy.</ConfigurationHelp>
      <label className="block">Server name<input className="block border rounded p-2 bg-background" value={serverName} onChange={e => setServerName(e.target.value)} /></label>
      <label className="block">Local URL<input className="block border rounded p-2 bg-background" value={serverUrl} onChange={e => setServerUrl(e.target.value)} aria-describedby="image-url-hint" /><span id="image-url-hint" className="text-sm">Example: http://127.0.0.1:8188. Loopback HTTP only; port 1024–65535.</span></label>
      <label className="block">Installed checkpoint filename<input className="block border rounded p-2 bg-background" value={checkpoint} onChange={e => setCheckpoint(e.target.value)} placeholder="sd_xl_base_1.0.safetensors" aria-describedby="checkpoint-hint" /><span id="checkpoint-hint" className="text-sm">Exact filename from ComfyUI’s checkpoint list, without directories. SD/SDXL only; Flux and custom node graphs need separate adapters.</span></label>
      <button disabled={busy || !checkpoint} className="border rounded px-3 py-2" onClick={() => void action(async () => { await workflowJson('/admin/image-servers', post({ config: { name: serverName, url: serverUrl, checkpoint }, ...(editingServer ? { id: editingServer.id, revision: editingServer.revision } : {}) })); setEditingServer(null); setNotice('Image server saved disabled. Test and enable it below.'); })}>{editingServer ? 'Save changes' : 'Add image server'}</button>
      {editingServer && <button className="underline ml-3" onClick={() => setEditingServer(null)}>Cancel edit</button>}
      {servers.map(server => <div key={server.id} className="border rounded p-3 space-x-3"><span>{server.name} · {server.enabled ? 'enabled' : 'disabled'}</span><button disabled={busy} className="underline" onClick={() => { setEditingServer(server); setServerName(server.name); setServerUrl(server.url || ''); setCheckpoint(server.checkpoint || ''); }}>Edit</button><button disabled={busy} className="underline" onClick={() => void action(async () => { await workflowJson(`/admin/image-servers/${server.id}/enable`, post({ revision: server.revision, enabled: !server.enabled })); })}>{server.enabled ? 'Disable' : 'Test checkpoint and enable'}</button><button disabled={busy} className="underline" onClick={() => void action(async () => { await workflowJson(`/admin/image-servers/${server.id}?revision=${server.revision}`, { method: 'DELETE' }); })}>Remove</button></div>)}
    </details>}
    <section className="space-y-3"><h2 className="text-lg font-semibold">Your runs</h2>
      {!runs.length && <p>No runs yet.</p>}
      {runs.map(run => <article id={`workflow-run-${run.id}`} key={run.id} className={`border rounded p-4 space-y-2 scroll-mt-4 ${run.id === acceptedRunId ? 'ring-2 ring-primary' : ''}`}>
        <h3 className="font-semibold">{run.definition.name} · {run.status}</h3>
        <p className="text-xs text-muted-foreground break-all">Run {run.id}</p>
        {run.error && <p role="alert">{run.error}</p>}
        {run.steps.map(step => <details key={step.id} className="border rounded p-2"><summary>{step.id} · {step.status}{step.elapsed_seconds !== undefined ? ` · ${step.elapsed_seconds}s` : ''}</summary>{step.error && <p role="alert">{step.error}</p>}{step.failure && <p className="text-sm"><strong>{step.failure.category.replace(/_/g, " ")}: </strong>{step.failure.recovery}</p>}{step.output?.report && <pre className="text-xs overflow-auto">{JSON.stringify(step.output.report, null, 2)}</pre>}{step.output?.report_artifact && <button className="underline" onClick={() => void download(step.output!.report_artifact!)}>Download inspection report</button>}{step.output?.artifact && <button className="underline ml-3" onClick={() => void download(step.output!.artifact!)}>Download {step.output.artifact.filename}</button>}{step.output?.artifact?.filename.endsWith('.png') && <ReferenceImage artifact={step.output.artifact} />}</details>)}
        {run.output?.artifact && <button disabled={busy} className="border rounded px-3 py-2" onClick={() => void download(run.output!.artifact!)}>Download final output</button>}
        {['queued', 'running'].includes(run.status) ? <button disabled={busy || run.cancel_requested} className="underline ml-3" onClick={() => void action(async () => { await workflowJson(`/runs/${run.id}/cancel`, post({})); })}>{run.cancel_requested ? 'Stopping after active step…' : 'Stop after active step'}</button> : <button disabled={busy} className="underline ml-3" onClick={() => void action(async () => { await workflowJson(`/runs/${run.id}`, { method: 'DELETE' }); })}>Delete run history</button>}
      </article>)}
    </section>
  </main>;
}
