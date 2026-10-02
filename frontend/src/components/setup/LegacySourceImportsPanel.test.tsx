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
