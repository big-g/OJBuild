import { useEffect, useState } from 'react';
import { apiFetch } from '../lib/api';

type Review = { id: string; owner: string; age: number | null; request: string; state: string };
export function ContentReviewPanel() {
  const [reviews, setReviews] = useState<Review[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [viewed, setViewed] = useState<Record<string, boolean>>({});
  async function refresh() {
    const response = await apiFetch('/v1/content-reviews');
    if (!response.ok) { setReviews([]); throw new Error('Record a birthdate establishing adulthood for your administrator account to review images.'); }
    setReviews((await response.json()).reviews); setError('');
  }
  useEffect(() => { void refresh().catch(e => setError(e.message)); }, []);
  async function decide(review: Review, approve: boolean) {
    setBusy(true); setError('');
    try {
      const response = await apiFetch(`/v1/content-reviews/${review.id}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ stage: review.state, approve }) });
      if (!response.ok) throw new Error((await response.json()).detail || 'Review failed');
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : 'Review unavailable'); }
    finally { setBusy(false); }
  }
  return <section aria-label="Image content reviews" className="border rounded p-3 space-y-3"><h3>Image content reviews</h3>
    <p className="text-sm">Approve only requests and images suitable for the account’s recorded age. Approval of a request permits generation; approval of the result permits delivery. Reviews expire after seven days.</p>
    <button type="button" className="underline" disabled={busy} onClick={() => void refresh().catch(e => setError(e.message))}>Refresh reviews</button>
    {error && <p role="alert">{error}</p>}
    {!error && !reviews.length && <p>No pending reviews.</p>}
    {reviews.map(review => <article key={review.id} className="border rounded p-2 space-y-2"><p>Account: {review.owner} · Age: {review.age ?? 'unknown'} · {review.state.replace(/_/g, ' ')}</p><pre className="whitespace-pre-wrap break-words max-h-40 overflow-auto">{review.request}</pre>
      {review.state === 'pending_image' && <ReviewImage review={review} ready={() => setViewed(current => ({ ...current, [review.id]: true }))} />}
      {['pending_request', 'pending_image'].includes(review.state) && <div className="flex gap-3"><button type="button" className="border rounded p-2" disabled={busy || (review.state === 'pending_image' && !viewed[review.id])} onClick={() => void decide(review, true)}>{review.state === 'pending_image' ? 'Approve image for delivery' : 'Approve request for generation'}</button><button type="button" className="border rounded p-2" disabled={busy} onClick={() => void decide(review, false)}>Reject</button></div>}
    </article>)}
  </section>;
}
function ReviewImage({ review, ready }: { review: Review; ready: () => void }) {
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    let disposed = false; let preview = '';
    void apiFetch(`/v1/content-reviews/${review.id}/image`).then(async response => {
      if (!response.ok) throw new Error('Review preview unavailable; do not approve without viewing it.');
      preview = URL.createObjectURL(await response.blob());
      if (disposed) URL.revokeObjectURL(preview); else setUrl(preview);
    }).catch(e => { if (!disposed) setError(e.message); });
    return () => { disposed = true; if (preview) URL.revokeObjectURL(preview); };
  }, [review.id]);
  return url ? <img src={url} alt="Generated image awaiting administrator review" className="max-h-80" onLoad={ready} /> : <p>{error || 'Loading private preview…'}</p>;
}
