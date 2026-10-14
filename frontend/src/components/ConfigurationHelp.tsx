import { useId, type ReactNode } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from './ui/dialog';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from './ui/tooltip';

/** Use an existing short first sentence as a summary, never a clipped description. */
export function configurationSummary(description: string): string | undefined {
  const text = description.replace(/\s+/g, ' ').trim();
  const sentence = text.match(/^.*?[.!?](?:\s|$)/)?.[0].trim() || text;
  return sentence && sentence.length <= 160 ? sentence : undefined;
}

export function ConfigurationHelp({ title, summary, children, id }: {
  title: string;
  summary?: string;
  children: ReactNode;
  id?: string;
}) {
  const summaryId = useId();
  const descriptionIds = [id, summary ? summaryId : undefined].filter(Boolean).join(' ') || undefined;
  const trigger = <DialogTrigger type="button"
    aria-label={`Info: ${title}`} aria-describedby={descriptionIds}
    className="inline-flex cursor-pointer rounded px-1 py-0.5 text-xs underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2"
    style={{ color: 'var(--color-text-secondary)' }}>Info</DialogTrigger>;
  return <div className="inline-block align-middle">
    {/* Keep field aria-describedby targets available without expanding the layout. */}
    <div id={id} hidden data-configuration-description>{children}</div>
    {summary && <span id={summaryId} hidden>{summary}</span>}
    <Dialog>
      {summary ? <TooltipProvider delay={350}><Tooltip>
        <TooltipTrigger render={trigger} />
        <TooltipContent role="tooltip">{summary}</TooltipContent>
      </Tooltip></TooltipProvider> : trigger}
      <DialogContent className="sm:max-w-xl max-h-[85dvh] overflow-y-auto"
        style={{ background: 'var(--color-bg)', color: 'var(--color-text)' }}>
        <DialogTitle className="pr-8">{title}</DialogTitle>
        <DialogDescription render={<div />} className="whitespace-pre-wrap leading-relaxed space-y-3">
          {children}
        </DialogDescription>
      </DialogContent>
    </Dialog>
  </div>;
}
