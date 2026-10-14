// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ConfigurationHelp, configurationSummary } from './ConfigurationHelp';
import { CredentialManagerPanel } from './setup/CredentialManagerPanel';

let root: Root;
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  document.body.replaceChildren();
});

it('opens full rich help without submitting a form and returns focus on Escape', async () => {
  const submit = vi.fn();
  await act(async () => root.render(<form onSubmit={submit}>
    <input aria-label="Endpoint" aria-describedby="endpoint-help" />
    <ConfigurationHelp id="endpoint-help" title="Endpoint" summary="Use the service HTTPS endpoint.">
      <p>Full instructions with <a href="https://example.com/docs">documentation</a>.</p>
    </ConfigurationHelp>
  </form>));
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(document.getElementById('endpoint-help')?.hidden).toBe(true);
  const trigger = document.querySelector<HTMLButtonElement>('button[aria-label="Info: Endpoint"]')!;
  await act(async () => { trigger.focus(); trigger.click(); });
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
  expect(dialog).not.toBeNull();
  expect(dialog.textContent).toContain('Full instructions');
  expect(dialog.querySelector('a')?.getAttribute('href')).toBe('https://example.com/docs');
  expect(submit).not.toHaveBeenCalled();
  expect(dialog.getAttribute('aria-labelledby')).toBeTruthy();
  await act(async () => { document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); });
  await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull());
  expect(document.activeElement).toBe(trigger);
});

it('shows the short summary on keyboard focus', async () => {
  await act(async () => root.render(<ConfigurationHelp title="Credentials" summary="Keep credentials on the server.">Full credential instructions.</ConfigurationHelp>));
  await act(async () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
    document.querySelector<HTMLButtonElement>('button')!.focus();
  });
  await vi.waitFor(() => expect(document.querySelector('[role="tooltip"]')?.textContent).toContain('Keep credentials on the server.'));
});

it('keeps full help available when no concise summary exists', async () => {
  expect(configurationSummary('A concise summary. More detailed explanation.')).toBe('A concise summary.');
  expect(configurationSummary('x'.repeat(200))).toBeUndefined();
  await act(async () => root.render(<ConfigurationHelp title="Complex setting">{'x'.repeat(200)}</ConfigurationHelp>));
  await act(async () => document.querySelector<HTMLButtonElement>('button')!.click());
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain('x'.repeat(200));
});


it('gives credential help links distinct visible topics', async () => {
  await act(async () => root.render(<CredentialManagerPanel credentials={[]} refresh={vi.fn()} />));
  const labels = [...document.querySelectorAll('button')].map(button => button.textContent);
  expect(labels).toContain('Storage and access scope · Info');
  expect(labels).toContain('Origin and token format · Info');
  expect(labels).not.toContain('Info');
});
