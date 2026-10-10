const routingError = '3D API returned a web page instead of data. Verify the API URL in Settings and that the updated OpenJarvis backend is running.';

export async function read3DJson<T>(response: Response): Promise<T> {
  const type = response.headers.get('content-type')?.toLowerCase() || '';
  if (!type.includes('json')) throw new Error(routingError);
  const body = await response.json().catch(() => { throw new Error('3D API returned invalid JSON. Check the backend service log.'); });
  if (!response.ok) throw new Error(typeof body?.detail === 'string' ? body.detail : `3D request failed (${response.status})`);
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('3D API returned an invalid response.');
  return body as T;
}

export async function checked3DDownload(response: Response): Promise<Response> {
  if (!response.ok) { await read3DJson(response); }
  if (!response.headers.get('content-type')?.toLowerCase().startsWith('model/gltf-binary')) throw new Error(routingError);
  return response;
}
