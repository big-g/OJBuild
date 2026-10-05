import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { RuntimeConfigurationFields, RuntimeConfigurationSummary } from './RuntimeConfigurationFields';
import type { RuntimeAdapter, RuntimeConfig } from '../lib/runtime-tools-api';

const adapter: RuntimeAdapter = {
  adapter_id: 'numeric_formula', label: 'Numeric formula', description: 'Arithmetic', validator_version: 'formula-v1',
  default_config: { expression: 'value * 1.8 + 32', variables: ['value'] },
  fields: [
    { name: 'expression', label: 'Formula', type: 'text', required: true, max_length: 512 },
    { name: 'variables', label: 'Variables', type: 'string_list', required: true },
  ],
};

it('renders adapter-declared formula fields, limits and defaults', () => {
  const html = renderToStaticMarkup(<RuntimeConfigurationFields adapter={adapter} config={{}} onChange={() => {}} />);
  expect(html).toContain('Formula');
  expect(html).toContain('maxLength="512"');
  expect(html).toContain('value * 1.8 + 32');
  expect(html).toContain('required=""');
});

it('converts comma-separated names to declared variables while preserving other fields', () => {
  let saved: RuntimeConfig = {};
  const tree = RuntimeConfigurationFields({ adapter, config: adapter.default_config, onChange: c => { saved = c; } });
  const input = tree.props.children[1].props.children[1];
  input.props.onChange({ target: { value: 'value, divisor' } });
  expect(saved).toEqual({ expression: 'value * 1.8 + 32', variables: ['value', 'divisor'] });
  expect(adapter.default_config.variables).toEqual(['value']);
});

it('shows the exact saved expression and variables in the approval summary', () => {
  const html = renderToStaticMarkup(<RuntimeConfigurationSummary adapter={adapter}
    config={{ expression: 'value / divisor', variables: ['value', 'divisor'] }} />);
  expect(html).toContain('value / divisor');
  expect(html).toContain('value, divisor');
  expect(html).toContain('Formula:');
});

it('renders JSON extraction paths with visible setup hints and exact approval values', () => {
  const jsonAdapter: RuntimeAdapter = {
    adapter_id: 'json_extract', label: 'JSON field extraction', description: 'Select supplied data', validator_version: 'json-extract-v1',
    default_config: { path: '/forecast/temperature' },
    fields: [{ name: 'path', label: 'JSON field path', type: 'text', required: true, max_length: 512,
      description: 'Start with /. Example: /items/0/name selects the first item. Escape / as ~1 and ~ as ~0.' }],
  };
  const html = renderToStaticMarkup(<RuntimeConfigurationFields adapter={jsonAdapter} config={{}} onChange={() => {}} />);
  expect(html).toContain('/forecast/temperature');
  expect(html).toContain('maxLength="512"');
  expect(html).toContain('/items/0/name');
  expect(html).toContain('Escape / as ~1');
  const summary = renderToStaticMarkup(<RuntimeConfigurationSummary adapter={jsonAdapter} config={{ path: '/items/0/name' }} />);
  expect(summary).toContain('JSON field path:');
  expect(summary).toContain('/items/0/name');
});
