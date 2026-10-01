import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { SourceAuditHistory, SourceEvolutionControls } from './SourceEvolutionControls';
import type { SourceInstance } from '../../lib/sources-api';
const source: SourceInstance = { id: 'one', adapter_id: 'local_files', name: 'Policies', config: {}, config_version: 1, adapter_config_version: 3, revision: 1, enabled: true, state: 'idle', error: null, configuration_state: 'migration_available' };
describe('source configuration evolution controls', () => {
  it('requires an explicit preview before application', () => {
    const html = renderToStaticMarkup(<SourceEvolutionControls source={source} refresh={async () => {}} />);
    expect(html).toContain('version 1 to 3');
    expect(html).toContain('Preview configuration upgrade');
    expect(html).not.toContain('Apply configuration upgrade');
    expect(html).toContain('Configuration history');
  });
  it('blocks unknown configuration versions without offering an upgrade', () => {
    const html = renderToStaticMarkup(<SourceEvolutionControls source={{ ...source, configuration_state: 'unsupported' }} refresh={async () => {}} />);
    expect(html).toContain('unsupported');
    expect(html).not.toContain('Preview configuration upgrade');
  });
  it('disables preview while a source is syncing', () => {
    const html = renderToStaticMarkup(<SourceEvolutionControls source={{ ...source, state: 'syncing' }} refresh={async () => {}} />);
    expect(html).toMatch(/disabled=""[^>]*>Preview configuration upgrade/);
  });
  it('provides global history including removed sources', () => {
    expect(renderToStaticMarkup(<SourceAuditHistory />)).toContain('including removed sources');
  });
});
