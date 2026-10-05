import { useEffect, useRef, useState } from 'react';
import type { FilePreview as Preview } from '../../lib/files-api';

function StlPreview({ triangles }: { triangles: number[][] }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [angle, setAngle] = useState(35);
  useEffect(() => {
    const ctx = canvas.current?.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, 600, 400);
    const rad = angle * Math.PI / 180;
    const projected = triangles.map((tri) => [0, 3, 6].map((index) => {
      const x = tri[index], y = tri[index + 1], z = tri[index + 2];
      return [x * Math.cos(rad) - y * Math.sin(rad),
        (x * Math.sin(rad) + y * Math.cos(rad)) * 0.4 - z];
    }));
    const points = projected.flat();
    if (!points.length) return;
    const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const scale = Math.min(560 / (maxX - minX || 1), 360 / (maxY - minY || 1));
    ctx.strokeStyle = '#7c9fff';
    ctx.lineWidth = 0.6;
    for (const tri of projected) {
      ctx.beginPath();
      tri.forEach((p, index) => {
        const x = 300 + (p[0] - (minX + maxX) / 2) * scale;
        const y = 200 + (p[1] - (minY + maxY) / 2) * scale;
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.closePath();
      ctx.stroke();
    }
  }, [triangles, angle]);
  return <div>
    <canvas ref={canvas} width={600} height={400} aria-label="STL wireframe preview"
      style={{ width: '100%', maxWidth: 600, background: '#111827' }} />
    <label className="block text-sm mt-2">Rotate model
      <input aria-label="Rotate model" type="range" min={0} max={360} value={angle}
        onChange={event => setAngle(Number(event.target.value))} className="ml-3" />
    </label>
  </div>;
}

export function FilePreview({ preview }: { preview: Preview }) {
  return <div>
    <p className="text-sm mb-3">This preview does not run the file. It does not certify that the file is safe to download or execute.</p>
    {preview.kind === 'stl' ? <StlPreview triangles={preview.triangles || []} /> :
      <pre className="p-3 overflow-auto whitespace-pre-wrap break-all text-sm"
        style={{ background: 'var(--color-bg-tertiary)', maxHeight: '60vh' }}>
        {preview.text}
      </pre>}
    {preview.kind === 'bytes' && <p className="text-sm mt-2">Binary preview: hexadecimal bytes. Active document rendering is disabled.</p>}
    {preview.truncated && <p role="status" className="text-sm mt-2">Preview is limited; the download contains the full stored file.</p>}
  </div>;
}
