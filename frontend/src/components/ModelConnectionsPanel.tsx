import { useEffect, useRef, useState, type FormEvent } from 'react';
import { getStoredUser } from '../lib/auth';
import { fetchModels } from '../lib/api';
import { useAppStore } from '../lib/store';
import {
  createModelConnection, getModelConnectionAudit, listModelConnections,
  removeModelConnection, testModelConnection, updateModelConnection,
  readModelCapabilities, enableModelConnection,
  type ModelConnection, type ModelConnectionEvent,
} from '../lib/model-connections-api';

export function ModelConnectionsPanel({ onConnectionsChanged, backendRevision = 0 }: { onConnectionsChanged?: () => void; backendRevision?: number }) {
  const isAdmin = getStoredUser()?.is_admin === true;
  const [connections, setConnections] = useState<ModelConnection[]>([]);
  const [editing, setEditing] = useState<ModelConnection | null>(null);
  const [name, setName] = useState('');
  const [url, setUrl] = useState('http://localhost:11434');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [events, setEvents] = useState<{ name: string; entries: ModelConnectionEvent[] } | null>(null);
  const refreshSequence = useRef(0);
  async function refresh() {
    const sequence = ++refreshSequence.current;
    const [servers, models] = await Promise.all([listModelConnections(), fetchModels()]);
    if (sequence !== refreshSequence.current) return;
    setConnections(servers.connections);
    useAppStore.getState().setModels(models);
  }
  useEffect(() => {
    if (isAdmin) void refresh().catch(e => setError(e instanceof Error ? e.message : 'Cannot load model connections'));
    return () => { ++refreshSequence.current; };
  }, [isAdmin, backendRevision]);
  async function act(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(''); setMessage(''); setEvents(null);
    try { await action(); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Model connection operation failed'); await refresh().catch(() => {}); }
    finally { setBusy(false); onConnectionsChanged?.(); }
  }
  function reset() { setEditing(null); setName(''); setUrl('http://localhost:11434'); }
  if (!isAdmin) return null;
  const input = 'block w-full rounded border px-3 py-2 bg-transparent';
  return <section id="model-server-connections" className="rounded-lg border p-4 space-y-3" aria-label="Model server connections">
    <h3 className="font-semibold">Model server connections</h3>
    <p className="text-sm">The backend’s configured Ollama servers carry forward automatically when task configuration loads. Save additional Ollama servers here. Test the catalog, read model capabilities, then enable the connection for chat. Models appear in the installed model picker with their server names. Adding a connection does not change the current chat model.</p>
    <details className="text-sm"><summary className="cursor-pointer">Connection setup help</summary>
      <p className="mt-2">Use localhost for Ollama on the OpenJarvis server, or the private IP of another machine running Ollama. Localhost refers to the backend server, not this browser. Remote Ollama must listen on its LAN address and allow access from the OpenJarvis server through its firewall.</p>
      <p>Test reads the installed model catalog only. It does not pull, load or run models. Read capabilities asks Ollama for a model manifest without running it. Capability labels are provider reports, not behavioral verification. Enable for chat makes reported chat-capable models available to all authenticated users. Requests also check live availability and required tool/image capabilities. HTTP LAN traffic is unencrypted; use only a trusted LAN or verified HTTPS.</p>
      <p>Each connection has its own identity, so identical model names on different servers stay separate. Edits and catalog/capability reads disable the connection; review and enable it again. Disabled/removed connections block later model calls in an active agent run. A generation already submitted may finish. After enabling, these chat models become available in Model task assignments and diagnostics below.</p>
    </details>
    <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); void act(async () => {
      if (editing) await updateModelConnection(editing, name.trim(), url.trim());
      else await createModelConnection(name.trim(), url.trim());
      reset(); setMessage('Connection saved. Test its catalog next.');
    }); }}>
      <label className="block text-sm" htmlFor="model-connection-name">Connection name</label>
      <input id="model-connection-name" className={input} aria-describedby="model-name-hint" required maxLength={24} pattern="[a-z][a-z0-9_]{0,23}" value={name} onChange={e => setName(e.target.value)} placeholder="home_gpu" disabled={busy} />
      <p id="model-name-hint" className="text-xs">1–24 characters. Start with a lowercase letter; use lowercase letters, digits or underscores. Example: home_gpu.</p>
      <label className="block text-sm" htmlFor="model-connection-url">Ollama server URL</label>
      <input id="model-connection-url" className={input} aria-describedby="model-url-hint" type="url" required maxLength={2048} value={url} onChange={e => setUrl(e.target.value)} disabled={busy} />
      <p id="model-url-hint" className="text-xs">HTTP(S) root URL, up to 2048 characters. Examples: http://localhost:11434 or http://192.168.1.20:11434. Use an explicit LAN IP; omit credentials and /api paths. Public servers and authenticated endpoints are not supported in this batch.</p>
      <button disabled={busy} className="rounded border px-3 py-2 disabled:opacity-50">{editing ? 'Save changes' : 'Add Ollama connection'}</button>
      {editing && <button disabled={busy} type="button" className="ml-2 rounded border px-3 py-2" onClick={reset}>Cancel edit</button>}
    </form>
    <ul className="space-y-3">{connections.map(connection => <li key={connection.id} className="rounded border p-3 space-y-2">
      <p><strong>{connection.name}</strong> — {connection.url}</p>
      <p className="text-sm">Chat: {connection.enabled ? 'enabled' : 'disabled'} · Catalog: {connection.discovery_state}{connection.tested_at > 0 && ` · Last test: ${new Date(connection.tested_at * 1000).toLocaleString()}`}</p>
      {connection.catalog.length > 0 && <ul className="text-sm space-y-2">{connection.catalog.map(model => <li key={model.serving_id}>{model.serving_id} · {(model.size_bytes / 1e9).toFixed(1)} GB package · {model.capability_state === 'reported' ? `Ollama reports: ${model.capabilities?.join(', ') || 'none'}` : 'capabilities unknown'} <button disabled={busy || editing !== null} className="rounded border px-2 py-1" type="button" onClick={() => void act(async () => { const result = await readModelCapabilities(connection, model.serving_id); if (result.ok) setMessage(result.message); else setError(result.message); })}>Read capabilities</button></li>)}</ul>}
      {connection.discovery_state === 'discovered' && connection.catalog.length === 0 && <p className="text-sm">No installed models found.</p>}
      <div className="flex flex-wrap gap-2">
        <button disabled={busy || editing !== null} className="rounded border px-3 py-1" type="button" onClick={() => void act(async () => { await enableModelConnection(connection, !connection.enabled); setMessage(connection.enabled ? 'Connection disabled for chat.' : 'Connection enabled. Open the installed model picker to select a model.'); })}>{connection.enabled ? 'Disable for chat' : 'Enable for chat'}</button>
        <button disabled={busy || editing !== null} className="rounded border px-3 py-1" type="button" onClick={() => void act(async () => { const result = await testModelConnection(connection); if (result.ok) setMessage(result.message); else setError(result.message); })}>Test catalog</button>
        <button disabled={busy} className="rounded border px-3 py-1" type="button" onClick={() => { setEditing(connection); setName(connection.name); setUrl(connection.url); setError(''); setMessage(''); }}>Edit</button>
        <button disabled={busy} className="rounded border px-3 py-1" type="button" onClick={() => { if (window.confirm(`Remove ${connection.name}? This removes the saved connection, not models on its server.`)) void act(async () => { await removeModelConnection(connection); if (editing?.id === connection.id) reset(); setMessage('Connection removed.'); }); }}>Remove</button>
        <button disabled={busy} className="rounded border px-3 py-1" type="button" onClick={() => void act(async () => { setEvents({ name: connection.name, entries: (await getModelConnectionAudit(connection.id)).events }); })}>View history</button>
      </div>
    </li>)}</ul>
    {events && <div className="text-sm"><h4>History: {events.name}</h4><ul>{events.entries.map((event, index) => <li key={index}>{event.event} · revision {event.revision} · {event.actor} · {new Date(event.timestamp * 1000).toLocaleString()}</li>)}</ul></div>}
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
  </section>;
}
