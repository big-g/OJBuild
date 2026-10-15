import { useRef, useState } from 'react';
import { Link } from 'react-router';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from './ui/dialog';
import { workflowJson } from '../pages/WorkflowsPage';

type SavedImage = { id: string; revision: number; definition: { name: string; steps: { operation: string; parameters?: { instance_id?: string } }[] } };
type Catalog = { image_generation_allowed?: boolean; workflows: SavedImage[]; operations: { id: string; enabled: boolean }[]; image_servers: { id: string; enabled: boolean }[] };

export function DirectImageAction({ prompt, disabled = false }: { prompt: string; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [choices, setChoices] = useState<SavedImage[]>([]);
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [run, setRun] = useState('');
  const active = useRef(false);
  async function prepare() {
    if (active.current) return;
    active.current = true; setBusy(true); setOpen(true); setDraft(prompt);
    setRun(''); setError(''); setChoices([]); setSelected('');
    try {
      const catalog = await workflowJson<Catalog>('');
      if (!catalog.image_generation_allowed) throw new Error('Direct image submission is disabled for your account. Use Jarvis chat to request an image. Ask an administrator to enable it in Settings → Account management.');
      if (!catalog.operations.some(op => op.id === 'image_generate_local' && op.enabled)) throw new Error('The image generation operation needs administrator approval in Generation & Workflows.');
      const enabled = new Set(catalog.image_servers.filter(s => s.enabled).map(s => s.id));
      const images = catalog.workflows.filter(w => w.definition.steps.length === 1 && w.definition.steps[0].operation === 'image_generate_local' && enabled.has(w.definition.steps[0].parameters?.instance_id || ''));
      setChoices(images); setSelected(images[0]?.id || '');
      if (!images.length) throw new Error('Save a Text to image workflow with an enabled image server in Generation & Workflows first.');
    } catch (e) { setError(e instanceof Error ? e.message : 'Image setup unavailable'); }
    finally { active.current = false; setBusy(false); }
  }
  async function generate() {
    const saved = choices.find(w => w.id === selected);
    if (active.current || !saved || run) return;
    active.current = true; setBusy(true); setError('');
    try {
      const result = await workflowJson<{ id: string; status: string }>('/image-runs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workflow_id: saved.id, revision: saved.revision, prompt: draft }),
      });
      if (!result.id || !result.status) throw new Error('Submission acknowledgement was incomplete. Check Workflows before retrying.');
      setRun(result.id);
    } catch (e) { setError(`${e instanceof Error ? e.message : 'Submission failed'}. Check workflow history before retrying; this action does not retry automatically.`); }
    finally { active.current = false; setBusy(false); }
  }
  return <>
    <button type="button" className="p-2 rounded border text-xs disabled:opacity-30" disabled={disabled || busy || !prompt.trim() || prompt.length > 4000} onClick={() => void prepare()} title="Send this text directly to an approved image workflow, without a chat model">Generate image</button>
    <Dialog open={open} onOpenChange={value => { if (!active.current) setOpen(value); }}>
      <DialogContent className="sm:max-w-xl max-h-[85dvh] overflow-y-auto">
        <DialogTitle>Generate image directly</DialogTitle>
        <DialogDescription>Your prompt goes unchanged to the selected workflow. No chat model reviews or rewrites it. The workflow’s checkpoint, negative prompt, seed and other settings still apply.</DialogDescription>
        <label className="block text-sm">Prompt<textarea readOnly value={draft} className="block w-full border rounded p-2 bg-background" rows={4} /></label>
        <label className="block text-sm">Saved image workflow<select aria-label="Saved image workflow" className="block w-full border rounded p-2 bg-background" value={selected} disabled={busy || !!run} onChange={e => setSelected(e.target.value)}><option value="">Choose a workflow</option>{choices.map(w => <option key={w.id} value={w.id}>{w.definition.name} · revision {w.revision}</option>)}</select></label>
        {error && <p role="alert">{error}</p>}
        {run ? <p role="status">Run {run} accepted. View progress and the generated file in Workflows.</p> : <button type="button" disabled={busy || !selected} onClick={() => void generate()} className="border rounded p-2 disabled:opacity-40">{busy ? 'Please wait…' : 'Submit image job'}</button>}
        <Link to="/workflows" className="underline" onClick={() => setOpen(false)}>Open Generation &amp; Workflows</Link>
      </DialogContent>
    </Dialog>
  </>;
}
