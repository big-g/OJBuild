import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { MessageBubble } from './MessageBubble';

const message = { id: 'id', role: 'assistant' as const, content: '```python\nprint(42)\n```', timestamp: 1 };
describe('chat generated files and output warnings', () => {
  it('offers explicit saving only after streaming completes', () => {
    const ready = renderToStaticMarkup(<MessageBubble message={message} />);
    const live = renderToStaticMarkup(<MessageBubble message={message} isLive />);
    expect(ready).toContain('Save file');
    expect(ready).not.toMatch(/<button disabled=""[^>]*>Save file/);
    expect(live).toMatch(/<button disabled=""[^>]*>Save file/);
  });
  it('shows the persisted length-limit warning for incomplete answers', () => {
    const html = renderToStaticMarkup(<MessageBubble message={{ ...message, telemetry: { finish_reason: 'length' } }} />);
    expect(html).toContain('may be incomplete');
    const complete = renderToStaticMarkup(<MessageBubble message={{ ...message, telemetry: { finish_reason: 'stop' } }} />);
    expect(complete).not.toContain('may be incomplete');
  });
});
