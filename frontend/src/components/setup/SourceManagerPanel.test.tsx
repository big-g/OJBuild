import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { SourceConfigurationFields } from './SourceManagerPanel';
import type { SourceAdapter } from '../../lib/sources-api';

const adapter: SourceAdapter = {
  adapter_id: 'future_api', display_name: 'Future API', description: 'Future source',
  config_version: 1, required_capabilities: ['network:fetch'], operations: ['test', 'sync'],
  fields: [
    { name: 'endpoint', label: 'API endpoint', type: 'text', required: true },
    { name: 'limit', label: 'Record limit', type: 'number' },
    { name: 'recursive', label: 'Recursive', type: 'checkbox' },
    { name: 'format', label: 'Format', type: 'select', options: [{ value: 'json', label: 'JSON' }] },
  ],
};

describe('adapter-driven source configuration', () => {
  it('renders an unfamiliar adapter entirely from server metadata', () => {
    const html = renderToStaticMarkup(<SourceConfigurationFields
      adapter={adapter} config={{ endpoint: 'https://example.test', limit: 25, recursive: true, format: 'json' }} onChange={() => {}}
    />);
    expect(html).toContain('API endpoint');
    expect(html).toContain('value="https://example.test"');
    expect(html).toContain('type="number"');
    expect(html).toContain('value="25"');
    expect(html).toContain('checked=""');
    expect(html).toContain('value="json" selected=""');
    expect(html).not.toContain('Server folder');
  });
});
