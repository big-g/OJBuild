import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { FilePreview } from './FilePreview';

const file = { id: 'id', filename: 'evil.html', size: 10, sha256: 'hash', created_at: 'date' };
describe('inert previews', () => {
  it('escapes HTML and script markup instead of creating executable DOM', () => {
    const html = renderToStaticMarkup(<FilePreview preview={{ ...file, kind: 'text', text: '<script>alert(1)</script><img src=x onerror=bad()>', truncated: false }} />);
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<iframe');
    expect(html).toContain('does not run the file');
  });
  it('uses canvas for STL and clearly labels bounded previews', () => {
    const html = renderToStaticMarkup(<FilePreview preview={{ ...file, kind: 'stl', triangles: [[0,0,0,1,0,0,0,1,0]], truncated: true }} />);
    expect(html).toContain('<canvas');
    expect(html).toContain('Preview is limited');
    expect(html).not.toContain('<object');
  });
});
