import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { APIServiceEditor } from "./APIServiceEditor";
import { SourceTestPreview } from "./APIConnectionHelp";
import {
  API_SERVICE_EXAMPLES,
  parseServiceDefinition,
} from "../../lib/api-service";

it("renders typed weather inputs and request/mapping/import guidance", () => {
  const html = renderToStaticMarkup(
    <APIServiceEditor
      config={{
        definition: JSON.stringify(API_SERVICE_EXAMPLES.open_meteo),
        operation: "forecast",
      }}
      credentials={[]}
      onChange={() => {}}
    />,
  );
  for (const text of [
    "Add input",
    "Input type",
    "Response format",
    "Pagination mode",
    "Operation inputs",
    "latitude",
    "forecast_days",
    "fahrenheit",
    "Request and response configuration",
    "Response mapping",
    "Pagination",
    "Linked requests",
    "Save reusable template",
    "OpenAPI 3 JSON or cURL",
    "protected credentials",
  ])
    expect(html).toContain(text);
  expect(html).toContain('min="-90"');
  expect(html).toContain('max="16"');
});

it("rejects malformed editor shapes without rendering invalid operations", () => {
  for (const operations of [
    [null],
    [{}],
    [{ id: "read", endpoint: "/", parameters: "invalid" }],
  ]) {
    const text = JSON.stringify({
      version: 1,
      base_url: "https://example.com",
      operations,
    });
    expect(parseServiceDefinition(text)).toBeNull();
    expect(
      renderToStaticMarkup(
        <APIServiceEditor
          config={{ definition: text }}
          credentials={[]}
          onChange={() => {}}
        />,
      ),
    ).toContain("Service JSON is incomplete or invalid");
  }
});

it("keeps example service definitions portable with no credential references", () => {
  for (const definition of Object.values(API_SERVICE_EXAMPLES)) {
    expect(parseServiceDefinition(JSON.stringify(definition))).toEqual(
      definition,
    );
    expect(JSON.stringify(definition)).not.toContain("credential_id");
  }
  expect(API_SERVICE_EXAMPLES.weather_gov.operations[0].steps).toEqual([
    { url_pointer: "/properties/forecast" },
  ]);
  expect(API_SERVICE_EXAMPLES.notion.operations[0].method).toBe("POST");
});

it("escapes diagnostic URLs and provider content", () => {
  const html = renderToStaticMarkup(
    <SourceTestPreview
      result={{
        ok: true,
        config: {},
        documents: 1,
        request_trace: [
          {
            method: "GET",
            url: "https://example.com/<script>",
            status: 200,
            content_type: "text/plain",
            bytes: 20,
          },
        ],
        sample_documents: [
          {
            title: "Data",
            content: "<script>run()</script>",
            fetched_at: "2026-10-08",
            truncated: false,
          },
        ],
      }}
    />,
  );
  expect(html).toContain("&lt;script&gt;");
  expect(html).not.toContain("<script>");
  expect(html).toContain("200");
});
