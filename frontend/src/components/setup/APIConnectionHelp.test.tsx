import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { APIConnectionHelp, SourceTestPreview } from './APIConnectionHelp';
import { SourceManagerPanel } from './SourceManagerPanel';

it('offers a distinct service API entry point in configured sources', () => {
  const html = renderToStaticMarkup(<SourceManagerPanel />);
  expect(html).toContain('Add API connection');
  expect(html).toContain('independently of AI providers and MCP tools');
});

it('explains API configuration, response mapping and analysis with source freshness', () => {
  const html = renderToStaticMarkup(<APIConnectionHelp />);
  expect(html).toContain('HTTP GET');
  expect(html).toContain('never paste keys or passwords into the URL');
  expect(html).toContain('/items');
  expect(html).toContain('stable record IDs');
  expect(html).toContain('Test connection');
  expect(html).toContain('Sync to make the data searchable');
  expect(html).toContain('Fetch time');
  expect(html).toContain('does not automatically send alerts');
});

it('renders mapped previews as inert text with truncation and fetch time', () => {
  const html = renderToStaticMarkup(<SourceTestPreview result={{ ok: true, config: {}, documents: 1,
    sample_documents: [{ title: '<img src=x onerror=alert(1)>', content: '<script>alert(1)</script>', truncated: true, fetched_at: '2026-10-06T00:00:00Z' }] }} />);
  expect(html).toContain('&lt;script&gt;');
  expect(html).toContain('&lt;img');
  expect(html).not.toContain('<script>');
  expect(html).not.toContain('<img');
  expect(html).toContain('Preview truncated');
  expect(html).toContain('2026-10-06T00:00:00Z');
  expect(html).toContain('has not saved or indexed data');
});

it('explains an empty successful response without inventing sample data', () => {
  const html = renderToStaticMarkup(<SourceTestPreview result={{ ok: true, config: {}, documents: 0, sample_documents: [] }} />);
  expect(html).toContain('No documents were returned');
  expect(html).not.toContain('<pre');
});
