// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LegacySourceImportsPanel } from './LegacySourceImportsPanel';

const api = vi.hoisted(() => ({ list: vi.fn(), preview: vi.fn(), apply: vi.fn() }));
vi.mock('../../lib/sources-api', () => ({
  listLegacySourceImports: api.list,
  previewLegacySourceImport: api.preview,
  applyLegacySourceImport: api.apply,
}));

let root: Root;
beforeEach(() => {
  vi.resetAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  document.body.replaceChildren();
});

const unavailable = [
  { import_id: 'notion', display_name: 'Notion', state: 'unavailable' },
  { import_id: 'mail', display_name: 'Mail', state: 'imported' },
  { import_id: 'drive', display_name: 'Drive', state: 'removed' },
];

it('omits the redundant provider listing when no import is actionable', async () => {
  api.list.mockResolvedValue({ imports: unavailable });
  await act(async () => root.render(<LegacySourceImportsPanel refresh={vi.fn()} />));
  expect(api.list).toHaveBeenCalled();
  expect(document.querySelector('section')).toBeNull();
  expect(document.body.textContent).toBe('');
});

it('keeps available imports, preview/apply and completion feedback working', async () => {
  api.list.mockResolvedValue({ imports: [...unavailable,
    { import_id: 'weather', display_name: 'Weather', state: 'available' }] });
  api.preview.mockResolvedValue({ import_id: 'weather', name: 'Weather',
    credential_origin: 'https://example.com', settings: [], expires_in_seconds: 600 });
  api.apply.mockImplementation(async () => {
    api.list.mockResolvedValue({ imports: unavailable });
  });
  const refresh = vi.fn().mockResolvedValue(undefined);
  await act(async () => root.render(<LegacySourceImportsPanel refresh={refresh} />));
  expect(document.body.textContent).not.toContain('Notion');
  expect(document.body.textContent).not.toContain('unavailable');
  const button = (text: string) => [...document.querySelectorAll('button')]
    .find(item => item.textContent === text)!;
  await act(async () => button('Import Weather').click());
  await act(async () => button('Preview import').click());
  expect(api.preview).toHaveBeenCalledWith('weather', 'Weather');
  await act(async () => button('Apply import').click());
  expect(api.apply).toHaveBeenCalled();
  expect(refresh).toHaveBeenCalledOnce();
  expect(document.body.textContent).toContain('Connection imported. Choose Sync');
  expect(document.querySelector('form')).toBeNull();
});
