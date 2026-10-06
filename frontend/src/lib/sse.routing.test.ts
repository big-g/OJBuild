import { afterEach, expect, it, vi } from 'vitest';
import { streamChat } from './sse';
vi.mock('./api', () => ({ getBase: () => 'http://backend', authHeaders: (h: Record<string, string>) => ({ ...h, 'X-OpenJarvis-Session': 'session' }) }));
afterEach(() => vi.unstubAllGlobals());
it('preserves routing event identity when event/data lines span network reads', async () => {
  const wire = 'event: routing_decision\ndata: {"model":"oj/server/model","task":"coding"}\n\ndata: {"choices":[]}\n\ndata: [DONE]\n\n';
  const response = new Response(new ReadableStream({ start(controller) {
    for (const char of wire) controller.enqueue(new TextEncoder().encode(char));
    controller.close();
  } }));
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal('fetch', fetchMock);
  const events = [];
  for await (const event of streamChat({ model: 'manual:model', routing_task: 'coding', messages: [], stream: true })) events.push(event);
  expect(events).toEqual([
    { event: 'routing_decision', data: '{"model":"oj/server/model","task":"coding"}' },
    { event: undefined, data: '{"choices":[]}' },
  ]);
  const options = fetchMock.mock.calls[0][1];
  expect(JSON.parse(options.body).routing_task).toBe('coding');
  expect(options.headers['X-OpenJarvis-Session']).toBe('session');
});
