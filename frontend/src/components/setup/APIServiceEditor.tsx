import { useEffect, useRef, useState } from "react";
import {
  API_SERVICE_EXAMPLES,
  parseServiceDefinition,
  type APIServiceDefinition,
  type APIOperation,
  type APIParameter,
} from "../../lib/api-service";
import {
  createAPITemplate,
  listAPITemplates,
  removeAPITemplate,
  importAPIService,
  type APITemplate,
  type SourceConfig,
  type SourceCredential,
} from "../../lib/sources-api";

function JSONEditor({
  label,
  value,
  onChange,
  hint,
}: {
  label: string;
  value: unknown;
  onChange: (value: unknown) => void;
  hint?: string;
}) {
  const serialized = JSON.stringify(value ?? {}, null, 2);
  const [text, setText] = useState(serialized);
  const [error, setError] = useState("");
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    setText(serialized);
    setError("");
    field.current?.setCustomValidity("");
  }, [serialized]);
  return (
    <label className="flex flex-col gap-1">
      {label}
      <textarea
        ref={field}
        aria-label={label}
        rows={5}
        maxLength={65536}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          try {
            const parsed: unknown = JSON.parse(e.target.value);
            onChange(parsed);
            setError("");
            e.target.setCustomValidity("");
          } catch {
            setError("Invalid JSON. Fix this field before saving.");
            e.target.setCustomValidity("Invalid JSON");
          }
        }}
      />
      <span>{hint}</span>
      {error && <span role="alert">{error}</span>}
    </label>
  );
}

function ParameterEditor({
  parameters,
  onChange,
}: {
  parameters: APIParameter[];
  onChange: (parameters: APIParameter[]) => void;
}) {
  const update = (index: number, value: APIParameter) =>
    onChange(parameters.map((p, i) => (i === index ? value : p)));
  return (
    <fieldset className="flex flex-col gap-3">
      <legend>Configure inputs</legend>
      {parameters.map((p, i) => (
        <fieldset key={i} className="flex flex-col gap-2">
          <legend>{p.label || p.name || `Input ${i + 1}`}</legend>
          <label>
            Input name
            <input
              required
              pattern="[A-Za-z][A-Za-z0-9_]*"
              maxLength={64}
              value={p.name}
              onChange={(e) => update(i, { ...p, name: e.target.value })}
            />
          </label>
          <label>
            Input label
            <input
              value={p.label || ""}
              onChange={(e) => update(i, { ...p, label: e.target.value })}
            />
          </label>
          <label>
            Placement
            <select
              value={p.in || "query"}
              onChange={(e) => update(i, { ...p, in: e.target.value })}
            >
              {["query", "path", "body", "variable"].map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </label>
          <label>
            Input type
            <select
              value={p.type || "string"}
              onChange={(e) => {
                const {
                  default: ignored,
                  enum: choices,
                  min,
                  max,
                  ...rest
                } = p;
                void ignored;
                void choices;
                void min;
                void max;
                update(i, { ...rest, type: e.target.value });
              }}
            >
              {["string", "number", "integer", "boolean", "string_list"].map(
                (v) => (
                  <option key={v}>{v}</option>
                ),
              )}
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={!!p.required}
              onChange={(e) => update(i, { ...p, required: e.target.checked })}
            />{" "}
            Required
          </label>
          <label>
            Default value
            <input
              value={
                Array.isArray(p.default)
                  ? p.default.join(", ")
                  : String(p.default ?? "")
              }
              onChange={(e) => {
                const { default: previous, ...rest } = p;
                void previous;
                if (!e.target.value) update(i, rest);
                else
                  update(i, {
                    ...p,
                    default:
                      p.type === "string_list"
                        ? e.target.value.split(",").map((v) => v.trim())
                        : p.type === "boolean"
                          ? e.target.value === "true"
                          : ["number", "integer"].includes(p.type || "")
                            ? Number(e.target.value)
                            : e.target.value,
                  });
              }}
            />
            <span>
              Leave blank for no default. Lists use commas; booleans use true or
              false.
            </span>
          </label>
          {["string", "string_list"].includes(p.type || "string") && (
            <label>
              Allowed choices
              <input
                value={p.enum?.join(", ") || ""}
                onChange={(e) => {
                  const { enum: previous, ...rest } = p;
                  void previous;
                  update(
                    i,
                    e.target.value
                      ? {
                          ...p,
                          enum: e.target.value.split(",").map((v) => v.trim()),
                        }
                      : rest,
                  );
                }}
              />
              <span>Optional comma-separated choices.</span>
            </label>
          )}
          {["number", "integer"].includes(p.type || "") && (
            <>
              {(["min", "max"] as const).map((key) => (
                <label key={key}>
                  {key === "min" ? "Minimum" : "Maximum"}
                  <input
                    type="number"
                    step="any"
                    value={p[key] ?? ""}
                    onChange={(e) => {
                      const next = { ...p };
                      if (e.target.value === "") delete next[key];
                      else next[key] = Number(e.target.value);
                      update(i, next);
                    }}
                  />
                </label>
              ))}
            </>
          )}
          {p.type === "string_list" && (
            <label>
              List encoding
              <select
                value={p.style || "comma"}
                onChange={(e) => update(i, { ...p, style: e.target.value })}
              >
                <option value="comma">Comma-separated</option>
                <option value="repeat">Repeated query parameter</option>
              </select>
            </label>
          )}
          <label>
            Help text
            <input
              value={p.description || ""}
              onChange={(e) => update(i, { ...p, description: e.target.value })}
            />
          </label>
          <button
            type="button"
            onClick={() =>
              onChange(parameters.filter((_, index) => index !== i))
            }
          >
            Remove input
          </button>
        </fieldset>
      ))}
      <button
        type="button"
        onClick={() =>
          onChange([
            ...parameters,
            {
              name: `input_${parameters.length + 1}`,
              type: "string",
              in: "query",
            },
          ])
        }
      >
        Add input
      </button>
    </fieldset>
  );
}

function MappingEditor({
  value,
  onChange,
}: {
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}) {
  const set = (key: string, next: string) => {
    const result = { ...value };
    if (next) result[key] = next;
    else delete result[key];
    onChange(result);
  };
  return (
    <fieldset className="flex flex-col gap-2">
      <legend>Map returned data</legend>
      <label>
        Response format
        <select
          value={String(value.format || "json")}
          onChange={(e) => set("format", e.target.value)}
        >
          {["json", "csv", "xml", "text"].map((v) => (
            <option key={v}>{v}</option>
          ))}
        </select>
      </label>
      <label>
        Mapping mode
        <select
          value={String(value.mode || "document")}
          onChange={(e) => set("mode", e.target.value)}
        >
          <option value="document">Whole document</option>
          <option value="records">Individual records</option>
          <option value="series">Aligned time series</option>
        </select>
      </label>
      {[
        "records_pointer",
        "id_pointer",
        "title_pointer",
        "content_pointer",
        "time_pointer",
        "units_pointer",
        "timezone_pointer",
        "error_pointer",
        ...(value.format === "xml" ? ["xml_path"] : []),
      ].map((key) => (
        <label key={key}>
          {key.replace(/_/g, " ")}
          <input
            value={String(value[key] || "")}
            onChange={(e) => set(key, e.target.value)}
          />
        </label>
      ))}
      <label>
        Maximum records
        <input
          type="number"
          min={1}
          max={1000}
          value={Number(value.max_records ?? 1000)}
          onChange={(e) =>
            onChange({ ...value, max_records: Number(e.target.value) })
          }
        />
      </label>
      <p>
        Use JSON pointers such as /items and /id. Empty record/content pointers
        select the root. Time series also need column mappings in the advanced
        response JSON.
      </p>
    </fieldset>
  );
}

function PaginationEditor({
  value,
  onChange,
}: {
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}) {
  const set = (key: string, next: unknown) =>
    onChange({ ...value, [key]: next });
  return (
    <fieldset className="flex flex-col gap-2">
      <legend>Page continuation</legend>
      <label>
        Pagination mode
        <select
          value={String(value.mode || "none")}
          onChange={(e) => set("mode", e.target.value)}
        >
          {["none", "link", "next_url", "cursor", "page", "offset"].map((v) => (
            <option key={v}>{v}</option>
          ))}
        </select>
      </label>
      {!!value.mode && value.mode !== "none" && (
        <>
          <label>
            Maximum pages
            <input
              type="number"
              min={1}
              max={50}
              value={Number(value.max_pages ?? 10)}
              onChange={(e) => set("max_pages", Number(e.target.value))}
            />
          </label>
          {["cursor", "next_url"].includes(String(value.mode)) && (
            <label>
              Next value pointer
              <input
                value={String(value.next_pointer || "")}
                onChange={(e) => set("next_pointer", e.target.value)}
                placeholder="/next_cursor"
              />
            </label>
          )}
          {["cursor", "page", "offset"].includes(String(value.mode)) && (
            <>
              <label>
                Continuation parameter
                <input
                  value={String(value.parameter || "cursor")}
                  onChange={(e) => set("parameter", e.target.value)}
                />
              </label>
              <label>
                Continuation placement
                <select
                  value={String(value.in || "query")}
                  onChange={(e) => set("in", e.target.value)}
                >
                  <option value="query">Query</option>
                  <option value="body">JSON body</option>
                  <option value="variables">GraphQL variables</option>
                </select>
              </label>
            </>
          )}
          {["page", "offset"].includes(String(value.mode)) && (
            <>
              <label>
                First page or offset
                <input
                  type="number"
                  min={0}
                  max={10000}
                  value={Number(value.start ?? 0)}
                  onChange={(e) => set("start", Number(e.target.value))}
                />
              </label>
              <label>
                Page size
                <input
                  type="number"
                  min={1}
                  max={10000}
                  value={Number(value.page_size ?? 100)}
                  onChange={(e) => set("page_size", Number(e.target.value))}
                />
              </label>
            </>
          )}
        </>
      )}
    </fieldset>
  );
}

export function APIServiceEditor({
  config,
  credentials,
  onChange,
}: {
  config: SourceConfig;
  credentials: SourceCredential[];
  onChange: (config: SourceConfig) => void;
}) {
  const parsed = parseServiceDefinition(config.definition);
  const definition = parsed ?? API_SERVICE_EXAMPLES.generic;
  const selected = String(config.operation || definition.operations[0].id);
  const operation =
    definition.operations.find((o) => o.id === selected) ??
    definition.operations[0];
  let inputs: Record<string, unknown> = {};
  try {
    inputs = JSON.parse(String(config.inputs || "{}"));
  } catch {
    /* Server validation reports malformed inputs. */
  }
  const [templates, setTemplates] = useState<APITemplate[]>([]);
  const [templateName, setTemplateName] = useState("");
  const [importText, setImportText] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => setTemplates((await listAPITemplates()).templates);
  useEffect(() => {
    void load().catch(() => {});
  }, []);
  const changeDefinition = (next: APIServiceDefinition) => {
    const names = new Set(
      (next.operations.find((o) => o.id === selected)?.parameters || []).map(
        (p) => p.name,
      ),
    );
    onChange({
      ...config,
      definition: JSON.stringify(next),
      operation: selected,
      inputs: JSON.stringify(
        Object.fromEntries(
          Object.entries(inputs).filter(([name]) => names.has(name)),
        ),
      ),
    });
  };
  const changeOperation = (next: APIOperation) =>
    changeDefinition({
      ...definition,
      operations: definition.operations.map((o) =>
        o.id === operation.id ? next : o,
      ),
    });
  const useDefinition = (next: APIServiceDefinition) => {
    onChange({
      definition: JSON.stringify(next),
      operation: next.operations[0].id,
      inputs: "{}",
      credential_id: "",
    });
    setNotice(
      "Draft loaded. Enter inputs, choose authentication and test before saving.",
    );
  };
  const perform = async (action: () => Promise<void>) => {
    setBusy(true);
    setNotice("");
    try {
      await action();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "Operation failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section
      className="flex flex-col gap-3"
      aria-label="API service configuration"
    >
      <p>
        Configure a service once, then select a named operation and its inputs
        for this connection. Definitions and inputs are saved in the server
        database. Templates are reusable copies; editing one does not change
        saved connections.
      </p>
      <label>
        Start from an example
        <select
          aria-label="API example"
          defaultValue=""
          onChange={(e) => {
            if (e.target.value)
              useDefinition(API_SERVICE_EXAMPLES[e.target.value]);
          }}
        >
          <option value="">Choose…</option>
          {Object.keys(API_SERVICE_EXAMPLES).map((name) => (
            <option key={name} value={name}>
              {name.replace(/_/g, " ")}
            </option>
          ))}
        </select>
      </label>
      {!!templates.length && (
        <label>
          My saved templates
          <select
            defaultValue=""
            onChange={(e) => {
              const t = templates.find((t) => t.id === e.target.value);
              if (t) useDefinition(t.definition);
            }}
          >
            <option value="">Choose…</option>
            {templates.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name} · revision {t.revision}
              </option>
            ))}
          </select>
        </label>
      )}
      <label>
        Service base URL
        <input
          aria-label="Service base URL"
          required
          type="url"
          value={definition.base_url}
          onChange={(e) =>
            changeDefinition({ ...definition, base_url: e.target.value })
          }
        />
      </label>
      <label>
        Documentation URL
        <input
          type="url"
          value={definition.documentation_url || ""}
          onChange={(e) =>
            changeDefinition({
              ...definition,
              documentation_url: e.target.value,
            })
          }
        />
      </label>
      <JSONEditor
        label="Service headers"
        value={definition.headers || {}}
        onChange={(value) =>
          changeDefinition({
            ...definition,
            headers: value as Record<string, string>,
          })
        }
        hint={
          'Non-secret headers, for example {"User-Agent":"OpenJarvis (contact@example.com)","Accept":"application/geo+json"}. Authentication belongs in protected credentials.'
        }
      />
      <label>
        Operation
        <select
          aria-label="API operation"
          value={operation.id}
          onChange={(e) =>
            onChange({
              ...config,
              definition: JSON.stringify(definition),
              operation: e.target.value,
              inputs: "{}",
            })
          }
        >
          {definition.operations.map((o) => (
            <option key={o.id} value={o.id} disabled={o.kind === "action"}>
              {o.name || o.id}
              {o.kind === "action" ? " — action requires a tool adapter" : ""}
            </option>
          ))}
        </select>
      </label>
      <label>
        Credential
        <select
          aria-label="API credential"
          value={String(config.credential_id || "")}
          onChange={(e) =>
            onChange({
              ...config,
              definition: JSON.stringify(definition),
              credential_id: e.target.value,
            })
          }
        >
          <option value="">No authentication</option>
          {credentials.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name} · {c.kind} · {c.origin}
            </option>
          ))}
        </select>
      </label>
      <p>
        The credential must match this service’s HTTPS origin. Query keys and
        Basic credentials are injected on the server and never added to the
        saved URL.
      </p>
      <fieldset className="flex flex-col gap-2">
        <legend>Operation inputs</legend>
        {operation.parameters?.map((p) => {
          const value = inputs[p.name] ?? p.default ?? "";
          const change = (value: unknown) =>
            onChange({
              ...config,
              definition: JSON.stringify(definition),
              inputs: JSON.stringify({ ...inputs, [p.name]: value }),
            });
          return (
            <label key={p.name} className="flex flex-col gap-1">
              {p.label || p.name}
              {p.enum && p.type !== "string_list" ? (
                <select
                  value={String(value)}
                  required={p.required}
                  onChange={(e) => change(e.target.value)}
                >
                  <option value="">Choose…</option>
                  {p.enum.map((v) => (
                    <option key={v} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              ) : p.type === "boolean" ? (
                <input
                  type="checkbox"
                  checked={Boolean(value)}
                  onChange={(e) => change(e.target.checked)}
                />
              ) : (
                <input
                  aria-label={`API input ${p.name}`}
                  type={
                    ["number", "integer"].includes(p.type || "")
                      ? "number"
                      : "text"
                  }
                  step={p.type === "integer" ? 1 : "any"}
                  required={p.required}
                  min={p.min}
                  max={p.max}
                  value={
                    Array.isArray(value) ? value.join(", ") : String(value)
                  }
                  onChange={(e) => {
                    if (e.target.value === "") {
                      const next = { ...inputs };
                      delete next[p.name];
                      onChange({
                        ...config,
                        definition: JSON.stringify(definition),
                        inputs: JSON.stringify(next),
                      });
                    } else
                      change(
                        p.type === "string_list"
                          ? e.target.value.split(",").map((v) => v.trim())
                          : ["number", "integer"].includes(p.type || "")
                            ? Number(e.target.value)
                            : e.target.value,
                      );
                  }}
                />
              )}
              {p.description && <span>{p.description}</span>}
              {p.type === "string_list" && (
                <span>Separate values with commas.</span>
              )}
            </label>
          );
        })}
        {!operation.parameters?.length && (
          <p>This operation has no configurable inputs.</p>
        )}
      </fieldset>
      <details>
        <summary>Request and response configuration</summary>
        <div className="flex flex-col gap-3">
          <label>
            Operation identifier
            <input value={operation.id} disabled />
            <span>
              Identifiers remain stable. Add or rename operations in the
              complete definition.
            </span>
          </label>
          <label>
            Operation name
            <input
              value={operation.name || ""}
              onChange={(e) =>
                changeOperation({ ...operation, name: e.target.value })
              }
            />
          </label>
          <label>
            HTTP method
            <select
              value={operation.method || "GET"}
              onChange={(e) =>
                changeOperation({ ...operation, method: e.target.value })
              }
            >
              <option>GET</option>
              <option>POST</option>
            </select>
          </label>
          <label>
            Endpoint path
            <input
              value={operation.endpoint}
              onChange={(e) =>
                changeOperation({ ...operation, endpoint: e.target.value })
              }
            />
            <span>
              Use placeholders such as /points/{"{latitude},{longitude}"}. Path
              inputs are escaped separately.
            </span>
          </label>
          <ParameterEditor
            parameters={operation.parameters || []}
            onChange={(parameters) =>
              changeOperation({ ...operation, parameters })
            }
          />
          <JSONEditor
            label="Parameter definitions"
            value={operation.parameters || []}
            onChange={(value) =>
              changeOperation({
                ...operation,
                parameters: value as APIOperation["parameters"],
              })
            }
            hint="Each input declares name, type, placement (query/path/body/variable), optional default, enum, min/max and required_when dependency."
          />
          <JSONEditor
            label="Operation headers"
            value={operation.headers || {}}
            onChange={(value) =>
              changeOperation({
                ...operation,
                headers: value as Record<string, string>,
              })
            }
          />
          <label>
            Body encoding
            <select
              value={operation.body_encoding || "json"}
              onChange={(e) =>
                changeOperation({ ...operation, body_encoding: e.target.value })
              }
            >
              {["json", "form", "graphql", "text", "xml"].map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </label>
          <JSONEditor
            label="Request body template"
            value={operation.body ?? null}
            onChange={(value) => changeOperation({ ...operation, body: value })}
            hint={
              'JSON objects or quoted strings. Use "{{input_name}}" for typed substitution. GraphQL uses {"query":"…","variables":{…}}.'
            }
          />
          <MappingEditor
            value={operation.response || {}}
            onChange={(response) => changeOperation({ ...operation, response })}
          />
          <JSONEditor
            label="Response mapping"
            value={operation.response || { format: "json", mode: "document" }}
            onChange={(value) =>
              changeOperation({
                ...operation,
                response: value as Record<string, unknown>,
              })
            }
            hint="Formats: json/csv/xml/text. Modes: document/records/series. Map records, IDs, titles, content, timestamps, columns, units, timezone and application-error pointers."
          />
          <PaginationEditor
            value={operation.pagination || {}}
            onChange={(pagination) =>
              changeOperation({ ...operation, pagination })
            }
          />
          <JSONEditor
            label="Pagination"
            value={operation.pagination || { mode: "none" }}
            onChange={(value) =>
              changeOperation({
                ...operation,
                pagination: value as Record<string, unknown>,
              })
            }
            hint="Modes: none/link/next_url/cursor/page/offset. Configure continuation placement, pointer, parameter and maximum pages."
          />
          <JSONEditor
            label="Linked requests"
            value={operation.steps || []}
            onChange={(value) =>
              changeOperation({
                ...operation,
                steps: value as APIOperation["steps"],
              })
            }
            hint={
              'Example: [{"url_pointer":"/properties/forecast"}]. Linked GETs stay on this service origin.'
            }
          />
          <button
            type="button"
            onClick={() => {
              const id = `operation_${Date.now()}`;
              const next = {
                ...definition,
                operations: [
                  ...definition.operations,
                  {
                    id,
                    kind: "read",
                    method: "GET",
                    endpoint: "/",
                    response: { mode: "document" },
                  },
                ],
              };
              onChange({
                ...config,
                definition: JSON.stringify(next),
                operation: id,
                inputs: "{}",
              });
            }}
          >
            Add operation
          </button>
        </div>
      </details>
      <details>
        <summary>Templates, import and complete definition</summary>
        <div className="flex flex-col gap-3">
          <label>
            Template name
            <input
              value={templateName}
              maxLength={120}
              onChange={(e) => setTemplateName(e.target.value)}
            />
          </label>
          <button
            type="button"
            disabled={busy || !templateName.trim() || !parsed}
            onClick={() =>
              void perform(async () => {
                await createAPITemplate(templateName, definition);
                await load();
                setNotice(
                  "Template saved without credentials or connection inputs.",
                );
              })
            }
          >
            Save reusable template
          </button>
          {templates.map((t) => (
            <div key={t.id}>
              {t.name}
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  if (
                    window.confirm(
                      `Remove template ${t.name}? Saved connections are unaffected.`,
                    )
                  )
                    void perform(async () => {
                      await removeAPITemplate(t);
                      await load();
                    });
                }}
              >
                Remove template
              </button>
            </div>
          ))}
          <label>
            Import service JSON, OpenAPI 3 JSON or cURL
            <textarea
              aria-label="API import"
              rows={5}
              maxLength={65536}
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
            />
            <span>
              Remove secrets before pasting. Import parses a draft locally; it
              does not execute commands, fetch URLs or authorize operations.
              OpenAPI imports endpoints and query/path inputs; review bodies,
              security, references and mappings.
            </span>
          </label>
          <button
            type="button"
            disabled={busy || !importText.trim()}
            onClick={() =>
              void perform(async () => {
                const result = await importAPIService(importText);
                useDefinition(result.definition);
                setNotice(result.notice);
              })
            }
          >
            Import draft
          </button>
          <label>
            Complete service definition
            <textarea
              aria-label="Complete service definition"
              rows={12}
              maxLength={65536}
              value={String(
                config.definition || JSON.stringify(definition, null, 2),
              )}
              onChange={(e) => {
                e.target.setCustomValidity(
                  parseServiceDefinition(e.target.value)
                    ? ""
                    : "Invalid service definition",
                );
                onChange({ ...config, definition: e.target.value });
              }}
            />
          </label>
          {!parsed && config.definition && (
            <p role="alert">
              Service JSON is incomplete or invalid. Fix it before testing or
              saving.
            </p>
          )}
          <button
            type="button"
            disabled={!parsed}
            onClick={() => {
              const blob = new Blob([JSON.stringify(definition, null, 2)], {
                type: "application/json",
              });
              const url = URL.createObjectURL(blob);
              const a = document.createElement("a");
              a.href = url;
              a.download = "api-service.json";
              a.click();
              URL.revokeObjectURL(url);
            }}
          >
            Export definition
          </button>
        </div>
      </details>
      <p>
        These connections fetch and index declared read operations. Actions,
        OAuth authorization, AWS signing, private LAN destinations, multipart
        uploads and streaming protocols require their specialized adapters;
        importing a definition does not enable them.
      </p>
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
