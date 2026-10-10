import { expect, it } from 'vitest';
import { read3DJson, checked3DDownload } from './hunyuan-response';

it('explains an HTML fallback instead of leaking a JSON syntax error', async () => {
  const html = () => new Response('<!DOCTYPE html><html>app</html>', { headers: { 'Content-Type': 'text/html' } });
  await expect(read3DJson(html())).rejects.toThrow('updated OpenJarvis backend');
  await expect(checked3DDownload(html())).rejects.toThrow('API URL');
});
it('preserves worker errors and rejects malformed JSON', async () => {
  await expect(read3DJson(Response.json({ detail: 'GPU busy' }, { status: 409 }))).rejects.toThrow('GPU busy');
  await expect(read3DJson(new Response('{', { headers: { 'Content-Type': 'application/json' } }))).rejects.toThrow('invalid JSON');
});
it('accepts JSON job data and binary GLB downloads', async () => {
  await expect(read3DJson(Response.json({ jobs: [] }))).resolves.toEqual({ jobs: [] });
  const response = new Response('glTF', { headers: { 'Content-Type': 'model/gltf-binary' } });
  await expect(checked3DDownload(response)).resolves.toBe(response);
});
