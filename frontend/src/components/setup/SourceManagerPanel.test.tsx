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

it('uses adapter defaults and displays dependent fields only in their mode', () => {
  const fields: SourceAdapter = { ...adapter, fields: [
    { name: 'mode', label: 'Mode', type: 'select', default_value: 'document', options: [
      { value: 'document', label: 'Whole document' }, { value: 'records', label: 'Records' },
    ] },
    { name: 'id_pointer', label: 'Record ID pointer', type: 'text', default_value: '/id', visible_when: { field: 'mode', equals: 'records' } },
    { name: 'max_records', label: 'Record limit', type: 'number', default_value: 200, min: 1, max: 1000, visible_when: { field: 'mode', equals: 'records' } },
    { name: 'complete', label: 'Complete snapshot', type: 'checkbox', default_value: false, description: 'Removes missing records', visible_when: { field: 'mode', equals: 'records' } },
  ] };
  const whole = renderToStaticMarkup(<SourceConfigurationFields adapter={fields} config={{}} onChange={() => {}} />);
  expect(whole).toContain('value="document" selected=""');
  expect(whole).not.toContain('Record ID pointer');
  const records = renderToStaticMarkup(<SourceConfigurationFields adapter={fields} config={{ mode: 'records' }} onChange={() => {}} />);
  expect(records).toContain('value="/id"');
  expect(records).toContain('value="200"');
  expect(records).toContain('min="1"');
  expect(records).toContain('Removes missing records');
  expect(records).not.toContain('checked=""');
});

it('renders protected credential references from adapter metadata without secret fields', () => {
  const definition: SourceAdapter = { ...adapter, fields: [{ name: 'credential_id', label: 'Credential', type: 'credential', credential_kinds: ['bearer'] }] };
  const html = renderToStaticMarkup(<SourceConfigurationFields adapter={definition} config={{ credential_id: 'credential-one' }} onChange={() => {}} credentials={[
    { id: 'credential-one', name: 'API token', kind: 'bearer', origin: 'https://api.example.com', header_name: 'Authorization', revision: 1, created_at: '', updated_at: '' },
    { id: 'wrong-kind', name: 'Other key', kind: 'api_key', origin: 'https://api.example.com', header_name: 'X-API-Key', revision: 1, created_at: '', updated_at: '' },
  ]} />);
  expect(html).toContain('value="credential-one" selected=""');
  expect(html).toContain('API token');
  expect(html).toContain('No authentication');
  expect(html).not.toContain('Other key');
  expect(html).not.toContain('type="password"');
});

it('supports compound conditions and mode-specific field values from metadata', () => {
  const fields: SourceAdapter = { ...adapter, fields: [
    { name: 'mode', label: 'Mode', type: 'select', default_value: 'records', options: [{ value: 'records', label: 'Records' }, { value: 'document', label: 'Document' }], value_updates: { document: { pagination: 'none', sync_mode: 'snapshot', complete_snapshot: false } } },
    { name: 'pagination', label: 'Pagination', type: 'select', default_value: 'none', options: [{ value: 'none', label: 'Single response' }, { value: 'cursor', label: 'Page cursor' }] },
    { name: 'next_pointer', label: 'Next page pointer', type: 'text', visible_when: [{ field: 'mode', equals: 'records' }, { field: 'pagination', one_of: ['cursor', 'next_url'] }] },
  ] };
  const none = renderToStaticMarkup(<SourceConfigurationFields adapter={fields} config={{}} onChange={() => {}} />);
  expect(none).not.toContain('Next page pointer');
  const paginated = renderToStaticMarkup(<SourceConfigurationFields adapter={fields} config={{ pagination: 'cursor' }} onChange={() => {}} />);
  expect(paginated).toContain('Next page pointer');
  const document = renderToStaticMarkup(<SourceConfigurationFields adapter={fields} config={{ mode: 'document', pagination: 'cursor' }} onChange={() => {}} />);
  expect(document).not.toContain('Next page pointer');
});

it('applies server-declared dependent value resets when the sync mode changes', () => {
  const definition: SourceAdapter = { ...adapter, fields: [{ name: 'sync_mode', label: 'Sync contract', type: 'select', options: [{ value: 'snapshot', label: 'Snapshot' }, { value: 'incremental', label: 'Incremental' }], value_updates: { incremental: { complete_snapshot: false } } }] };
  let saved: Record<string, string | number | boolean> = {};
  const tree = SourceConfigurationFields({ adapter: definition, config: { complete_snapshot: true, url: 'https://api.example.com/data' }, onChange: (value) => { saved = value; } });
  const label = tree.props.children[0];
  const select = label.props.children[1];
  select.props.onChange({ target: { value: 'incremental' } });
  expect(saved).toEqual({ complete_snapshot: false, sync_mode: 'incremental', url: 'https://api.example.com/data' });
});
