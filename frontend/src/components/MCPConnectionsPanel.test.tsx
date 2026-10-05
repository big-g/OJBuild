import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { MCPCatalogReview, MCPConnectionsPanel } from './MCPConnectionsPanel';
import type { MCPConnection } from '../lib/runtime-mcp-api';

const connection: MCPConnection = { id: 'id', revision: 2, has_token: false, discovered: true,
  enabled: false, approved: false, approved_by: '', url: 'https://example.com/mcp',
  name: 'example', fingerprint: 'reviewed-digest', allow_without_confirmation: false,
  tools: [{ name: 'custom_mcp_example_123', remote_name: 'lookup', description: '<script>untrusted</script>',
    parameters: { type: 'object', required: ['query'] }, annotations: { readOnlyHint: true }, contract_digest: 'contract-digest' }],
};

it('shows exact schemas, untrusted hints, digest and confirmation constraints', () => {
  const html = renderToStaticMarkup(<MCPCatalogReview connection={connection} />);
  expect(html).toContain('query');
  expect(html).toContain('readOnlyHint');
  expect(html).toContain('reviewed-digest');
  expect(html).toContain('custom_mcp_example_123');
  expect(html).toContain('without a confirmation callback will block calls');
  expect(html).toContain('&lt;script&gt;');
  expect(html).not.toContain('<script>');
});

it('discloses automated use only when the saved permission allows it', () => {
  expect(renderToStaticMarkup(<MCPCatalogReview connection={{ ...connection, allow_without_confirmation: true }} />))
    .toContain('including chat and scheduled agents');
});

it('starts with password input and automated calls unchecked', () => {
  const html = renderToStaticMarkup(<MCPConnectionsPanel />);
  expect(html).toContain('type="password"');
  expect(html).toContain('autoComplete="off"');
  expect(html).toContain('never returned to this form');
  expect(html).not.toContain('checked=""');
});

it('offers review separately and explains disabled import with retained legacy config', () => {
  const html = renderToStaticMarkup(<MCPConnectionsPanel />);
  expect(html).toContain('Review legacy configuration');
  expect(html).toContain('Import does not edit or disable the legacy configuration');
  expect(html).toContain('fresh approval');
});

it('allows importing ready entries only and escapes imported metadata', async () => {
  const { LegacyMCPReview } = await import('./MCPConnectionsPanel');
  const rows = ['ready', 'blocked', 'already_saved'].map((status, index) => ({
    index, status: status as 'ready' | 'blocked' | 'already_saved',
    label: '<script>never execute</script>', reason: 'manual review required', review_digest: 'opaque',
  }));
  const html = renderToStaticMarkup(<LegacyMCPReview entries={rows} busy={false} onImport={() => {}} />);
  expect((html.match(/disabled=""/g) || []).length).toBe(2);
  expect(html).toContain('&lt;script&gt;');
  expect(html).not.toContain('<script>');
  const busy = renderToStaticMarkup(<LegacyMCPReview entries={rows} busy onImport={() => {}} />);
  expect((busy.match(/disabled=""/g) || []).length).toBe(3);
});
