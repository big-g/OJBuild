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

it('offers explicit LAN authorization while keeping public HTTPS as the default', () => {
  const html = renderToStaticMarkup(<MCPConnectionsPanel />);
  expect(html).toContain('<option value="public" selected="">Public HTTPS (default)</option>');
  expect(html).toContain('Authorized private LAN');
  expect(html).toContain('authorized addresses or certificate trust clears a retained token');
  expect(html).not.toContain('LAN endpoints and local package installation are not supported');
});

it('explains the connection name format before submission and associates help with the input', () => {
  const html = renderToStaticMarkup(<MCPConnectionsPanel />);
  expect(html).toContain('Use 1–24 characters. Start with a lowercase letter');
  expect(html).toContain('No spaces, capital letters or hyphens');
  expect(html).toContain('home_tools</code>');
  expect(html).toContain('weather2</code>');
  const description = html.match(/aria-describedby="([^"]+)"/);
  expect(description).not.toBeNull();
  expect(html).toContain(`id="${description![1]}"`);
  expect(html).toContain('title="Use 1–24 characters');
});

it('provides setup help in the app for users unfamiliar with MCP and its approval steps', () => {
  const html = renderToStaticMarkup(<MCPConnectionsPanel />);
  expect(html).toContain('What is MCP? Connection setup help');
  expect(html).toContain('Model Context Protocol');
  expect(html).toContain('not the provider’s display name');
  expect(html).toContain('homepage may not be an MCP endpoint');
  expect(html).toContain('A bearer token is a secret access key');
  expect(html).toContain('never paste a private key');
  expect(html).toContain('Browser chat and scheduled tasks need this option');
});
