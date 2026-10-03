import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { LegacySourceImportReview } from './LegacySourceImportsPanel';

it('reviews import effects and expiry without rendering its control ticket', () => {
  const html = renderToStaticMarkup(<LegacySourceImportReview plan={{
    import_id: 'notion', adapter_id: 'notion_pages', name: 'Work pages',
    credential_origin: 'https://api.notion.com', config_version: 1,
    config: { query: '', max_pages: 100 }, settings: [{ label: 'Page limit', value: 100 }], fresh_index: true,
    legacy_connection_kept: true, expires_in_seconds: 600, plan_token: 'private-preview-ticket',
  }} />);
  expect(html).toContain('Work pages');
  expect(html).toContain('https://api.notion.com');
  expect(html).toContain('Page limit: 100');
  expect(html).toContain('new, independent index');
  expect(html).toContain('original connection will be kept');
  expect(html).toContain('does not contact the provider or start syncing');
  expect(html).toContain('10 minutes');
  expect(html).not.toContain('private-preview-ticket');
  expect(html).not.toContain('type="password"');
});

it('describes OAuth bundles without claiming an origin restriction or narrower permissions', () => {
  const html = renderToStaticMarkup(<LegacySourceImportReview plan={{
    import_id: 'gmail', adapter_id: 'gmail_account', name: 'Work mail',
    credential_origin: 'https://www.googleapis.com', credential_storage: 'bundle',
    oauth_grant_preserved: true, refresh_available: true,
    config_version: 1, config: {}, settings: [], fresh_index: true,
    legacy_connection_kept: true, expires_in_seconds: 600, plan_token: 'private-import-ticket',
  }} />);
  expect(html).toContain('encrypted account credential bundle');
  expect(html).toContain('does not narrow');
  expect(html).toContain('Refresh credentials will be copied');
  expect(html).toContain('refresh-token rotation can affect both');
  expect(html).not.toContain('restricted to');
  expect(html).not.toContain('private-import-ticket');
});

it('explains that access-only imports will need new authorization', () => {
  const html = renderToStaticMarkup(<LegacySourceImportReview plan={{
    import_id: 'spotify', adapter_id: 'spotify_account', name: 'Music',
    credential_origin: 'https://api.spotify.com', credential_storage: 'bundle',
    oauth_grant_preserved: true, refresh_available: false,
    config_version: 1, config: {}, settings: [], fresh_index: true,
    legacy_connection_kept: true, expires_in_seconds: 600, plan_token: 'private-ticket',
  }} />);
  expect(html).toContain('No refresh credentials are available');
  expect(html).toContain('access token expires');
  expect(html).not.toContain('private-ticket');
});

it('reviews copied IMAP passwords, endpoint limits and unverified access without secret fields', () => {
  const html = renderToStaticMarkup(<LegacySourceImportReview plan={{
    import_id: 'imap', adapter_id: 'imap_account', name: 'Work mailbox',
    credential_origin: 'mail.example.com:993 (TLS)', credential_storage: 'bundle',
    connection_auth: 'password', oauth_grant_preserved: false,
    config_version: 1, config: { host: 'mail.example.com', mailbox: 'INBOX' },
    settings: [{ label: 'IMAP host', value: 'mail.example.com' }, { label: 'Mailbox', value: 'INBOX' }],
    fresh_index: true, legacy_connection_kept: true, expires_in_seconds: 600, plan_token: 'private-ticket',
  }} />);
  expect(html).toContain('saved username and password');
  expect(html).toContain('encrypted vault storage');
  expect(html).toContain('does not verify login or mailbox access');
  expect(html).toContain('start with public destination access');
  expect(html).toContain('explicitly authorize its LAN IP addresses');
  expect(html).toContain('Mailbox: INBOX');
  expect(html).not.toContain('OAuth grant');
  expect(html).not.toContain('private-ticket');
  expect(html).not.toContain('type="password"');
});
