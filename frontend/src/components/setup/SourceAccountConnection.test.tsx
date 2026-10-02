import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { SourceAccountConnection } from './SourceAccountConnection';
import type { SourceInstance } from '../../lib/sources-api';

const source: SourceInstance = { id: 'instance-1', adapter_id: 'oura_account', name: 'Personal', config: {}, config_version: 1, revision: 1, enabled: true, state: 'idle', error: null };
it('offers an encrypted token input and describes index reset on replacement', () => {
  const html = renderToStaticMarkup(<SourceAccountConnection source={source} authType="token" refresh={async () => {}} />);
  expect(html).toContain('type="password"');
  expect(html).toContain('Stored encrypted on the server');
  expect(html).toContain('indexed documents');
  expect(html).toContain('Disconnect this account');
});
it('offers per-instance OAuth and application configuration', () => {
  const html = renderToStaticMarkup(<SourceAccountConnection source={source} authType="oauth" refresh={async () => {}} />);
  expect(html).toContain('Authorize account');
  expect(html).toContain('Configure OAuth application');
  expect(html).not.toContain('Account token');
});
