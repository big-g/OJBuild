import { apiFetch } from './api';

export interface GeneratedFile {
  id: string;
  filename: string;
  size: number;
  sha256: string;
  created_at: string;
}
export interface FilePreview extends GeneratedFile {
  kind: 'text' | 'bytes' | 'stl';
  text?: string;
  triangles?: number[][];
  facets?: number | null;
  truncated: boolean;
}
async function checked(response: Response): Promise<Response> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `File request failed (${response.status})`);
  }
  return response;
}
export async function listFiles(): Promise<GeneratedFile[]> {
  return (await (await checked(await apiFetch('/v1/files'))).json()).files;
}
export async function saveFile(filename: string, content: string): Promise<GeneratedFile> {
  return (await checked(await apiFetch('/v1/files', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, content, encoding: 'utf8' }),
  }))).json();
}
export async function previewFile(id: string): Promise<FilePreview> {
  return (await checked(await apiFetch(`/v1/files/${encodeURIComponent(id)}/preview`))).json();
}
export async function deleteFile(id: string): Promise<void> {
  await checked(await apiFetch(`/v1/files/${encodeURIComponent(id)}`, { method: 'DELETE' }));
}
export async function downloadFile(file: GeneratedFile): Promise<void> {
  const response = await checked(await apiFetch(`/v1/files/${encodeURIComponent(file.id)}/download`));
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = file.filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function suggestedFilename(language: string): string {
  const extensions: Record<string, string> = {
    python: 'py', py: 'py', javascript: 'js', typescript: 'ts', bash: 'sh',
    shell: 'sh', sh: 'sh', powershell: 'ps1', json: 'json', html: 'html',
    svg: 'svg', stl: 'stl', scad: 'scad', css: 'css', sql: 'sql',
  };
  return `generated.${extensions[language.toLowerCase()] || 'txt'}`;
}
