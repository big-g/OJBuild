import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { SourceSyncControls } from './SourceSyncControls';
import type { SourceInstance, SourceJob } from '../../lib/sources-api';

const source: SourceInstance = { id: 'source-one', adapter_id: 'json_api', name: 'Policies', config: {}, config_version: 1, revision: 1, enabled: true, state: 'idle', error: null };
const job: SourceJob = { id: 'job-one', source_id: source.id, source_revision: 1, trigger: 'manual', state: 'running', phase: 'fetching', created_at: '2026-10-01T00:00:00Z', started_at: null, finished_at: null, cancel_requested: false, documents_seen: 4, documents_total: 10, chunks_written: 2, pages_read: 3, error: null };

describe('source sync lifecycle controls', () => {
  it('shows counts, cooperative cancellation and persistent schedule controls', () => {
    const html = renderToStaticMarkup(<SourceSyncControls source={{ ...source, latest_job: job }} refresh={async () => {}} />);
    expect(html).toContain('4 documents of 10');
    expect(html).toContain('2 chunks written');
    expect(html).toContain('3 pages');
    expect(html).toContain('Cancel sync');
    expect(html).toContain('Scheduled sync');
    expect(html).toContain('min="5"');
    expect(html).toContain('Run history');
  });
  it('reports pending cancellation and prevents cancellation during commit', () => {
    const html = renderToStaticMarkup(<SourceSyncControls source={{ ...source, latest_job: { ...job, cancel_requested: true } }} refresh={async () => {}} />);
    expect(html).toContain('Cancellation requested');
    expect(html).toContain('disabled=""');
    const committed = renderToStaticMarkup(<SourceSyncControls source={{ ...source, latest_job: { ...job, phase: 'committing' } }} refresh={async () => {}} />);
    expect(committed).toContain('disabled=""');
  });
  it('shows paused schedules for disabled sources and terminal errors', () => {
    const html = renderToStaticMarkup(<SourceSyncControls source={{ ...source, enabled: false, schedule: { revision: 2, enabled: true, interval_seconds: 600 }, latest_job: { ...job, state: 'interrupted', phase: 'finished', error: 'Sync interrupted; retry' } }} refresh={async () => {}} />);
    expect(html).toContain('Schedule paused');
    expect(html).toContain('interrupted');
    expect(html).toContain('Sync interrupted; retry');
    expect(html).not.toContain('Cancel sync');
  });
});
