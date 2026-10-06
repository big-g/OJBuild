import type { SourceTestResult } from '../../lib/sources-api';

export function APIConnectionHelp() {
  return <details className="text-sm">
    <summary className="cursor-pointer">Connect a service API and use its data with Jarvis</summary>
    <p>This connection reads JSON data from a service using HTTP GET. It can supply monitoring readings, forecasts, inventories or other service data for Jarvis to analyze. AI model providers are configured separately.</p>
    <ol className="list-decimal pl-5 space-y-2">
      <li>Enter the service’s data endpoint, for example <code>https://api.example.com/readings?station=home</code>. Use non-secret query parameters only; never paste keys or passwords into the URL.</li>
      <li>If authentication is required, add a credential in the Protected credentials section. Set its HTTPS origin to the scheme and host, such as <code>https://api.example.com</code>, and choose Bearer token or API key header according to the provider’s instructions. Select that credential in the connection form.</li>
      <li>Choose Whole JSON document for a single response. For a list, choose Individual records and map the array and stable record IDs. For <code>{'{"items":[{"id":"sensor1","title":"Room","body":"21 °C"}]}'}</code>, use <code>/items</code>, <code>/id</code>, <code>/title</code> and <code>/body</code>. Empty array/content pointers select the root or whole record; key names are case-sensitive.</li>
      <li>Select Test connection to check authentication and the complete mapped scan, then inspect the text preview. Testing does not save the connection or index data.</li>
      <li>Save, then Sync to make the data searchable by Jarvis. Configure an interval schedule on the saved card for changing data, and keep Use this source for my account selected.</li>
      <li>Ask a tool-enabled chat to analyze that source—for example, “Using my Home readings source, explain the latest temperature readings and cite the data.” Jarvis’s feedback is an interpretation of retrieved data, not a replacement for the service’s measurements.</li>
    </ol>
    <p>Connections are personal by default. Universal access requires administrator approval. Fetch time says when Jarvis read the API; preserve measurement timestamps and units in the returned data. Scheduled sync updates the knowledge store; it does not automatically send alerts.</p>
    <p>Currently supported: public HTTP/HTTPS JSON GET endpoints, protected header credentials over HTTPS and bounded pagination. Private LAN APIs, OAuth-only general services, XML responses and API operations that change data require additional adapters.</p>
  </details>;
}

export function SourceTestPreview({ result }: { result: SourceTestResult }) {
  return <section className="hud-panel p-3 space-y-2" aria-label="Connection test preview">
    <h4>Mapped API data preview</h4>
    <p>{result.documents ?? 0} documents validated. Showing up to 3 samples and 4,096 characters per sample. This test has not saved or indexed data.</p>
    {result.sample_documents?.map((sample, index) => <article key={index} className="space-y-1">
      <h5 className="break-all">{sample.title}</h5>
      {sample.fetched_at && <p>Fetched at: {sample.fetched_at}</p>}
      <pre className="overflow-x-auto whitespace-pre-wrap break-all">{sample.content}</pre>
      {sample.truncated && <p>Preview truncated; the validated document contains more data.</p>}
    </article>)}
    {result.documents === 0 && <p>No documents were returned. Check the endpoint and response mapping.</p>}
  </section>;
}
