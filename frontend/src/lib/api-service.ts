export interface APIParameter {
  name: string;
  label?: string;
  description?: string;
  in?: string;
  type?: string;
  required?: boolean;
  default?: unknown;
  enum?: string[];
  min?: number;
  max?: number;
  style?: string;
  required_when?: string;
}
export interface APIOperation {
  id: string;
  name?: string;
  description?: string;
  kind?: string;
  method?: string;
  endpoint: string;
  headers?: Record<string, string>;
  parameters?: APIParameter[];
  body_encoding?: string;
  body?: unknown;
  response?: Record<string, unknown>;
  pagination?: Record<string, unknown>;
  steps?: { url_pointer: string }[];
}
export interface APIServiceDefinition {
  version: number;
  base_url: string;
  description?: string;
  documentation_url?: string;
  headers?: Record<string, string>;
  operations: APIOperation[];
}
export const API_SERVICE_EXAMPLES: Record<string, APIServiceDefinition> = {
  weather_gov: {
    version: 1,
    base_url: "https://api.weather.gov",
    documentation_url: "https://www.weather.gov/documentation/services-web-api",
    headers: { "User-Agent": "OpenJarvis", Accept: "application/geo+json" },
    operations: [
      {
        id: "forecast",
        name: "Location forecast",
        kind: "read",
        method: "GET",
        endpoint: "/points/{latitude},{longitude}",
        parameters: [
          {
            name: "latitude",
            in: "path",
            type: "number",
            required: true,
            min: -90,
            max: 90,
          },
          {
            name: "longitude",
            in: "path",
            type: "number",
            required: true,
            min: -180,
            max: 180,
          },
        ],
        steps: [{ url_pointer: "/properties/forecast" }],
        response: {
          format: "json",
          mode: "records",
          records_pointer: "/properties/periods",
          id_pointer: "/number",
          title_pointer: "/name",
        },
      },
    ],
  },
  open_meteo: {
    version: 1,
    base_url: "https://api.open-meteo.com",
    documentation_url: "https://open-meteo.com/en/docs",
    operations: [
      {
        id: "forecast",
        name: "Hourly forecast",
        kind: "read",
        method: "GET",
        endpoint: "/v1/forecast",
        parameters: [
          {
            name: "latitude",
            type: "number",
            required: true,
            min: -90,
            max: 90,
          },
          {
            name: "longitude",
            type: "number",
            required: true,
            min: -180,
            max: 180,
          },
          {
            name: "hourly",
            type: "string_list",
            default: ["temperature_2m", "precipitation_probability"],
            description: "Comma-separated weather variables.",
          },
          {
            name: "timezone",
            default: "auto",
            description: "auto or a timezone such as America/New_York.",
          },
          {
            name: "temperature_unit",
            default: "fahrenheit",
            enum: ["celsius", "fahrenheit"],
          },
          {
            name: "forecast_days",
            type: "integer",
            default: 7,
            min: 1,
            max: 16,
          },
        ],
        response: {
          format: "json",
          mode: "series",
          time_pointer: "/hourly/time",
          columns: {
            temperature_2m: "/hourly/temperature_2m",
            precipitation_probability: "/hourly/precipitation_probability",
          },
          units_pointer: "/hourly_units",
          timezone_pointer: "/timezone",
        },
      },
    ],
  },
  github: {
    version: 1,
    base_url: "https://api.github.com",
    headers: { Accept: "application/vnd.github+json" },
    operations: [
      {
        id: "issues",
        kind: "read",
        method: "GET",
        endpoint: "/repos/{owner}/{repository}/issues",
        parameters: [
          { name: "owner", in: "path", required: true },
          { name: "repository", in: "path", required: true },
        ],
        response: {
          mode: "records",
          id_pointer: "/id",
          title_pointer: "/title",
        },
        pagination: { mode: "link", max_pages: 10 },
      },
    ],
  },
  notion: {
    version: 1,
    base_url: "https://api.notion.com",
    headers: { "Notion-Version": "2025-09-03" },
    operations: [
      {
        id: "search",
        kind: "read",
        method: "POST",
        endpoint: "/v1/search",
        body_encoding: "json",
        body: { page_size: 100 },
        parameters: [{ name: "query", in: "body", default: "" }],
        response: {
          mode: "records",
          records_pointer: "/results",
          id_pointer: "/id",
        },
        pagination: {
          mode: "cursor",
          next_pointer: "/next_cursor",
          parameter: "start_cursor",
          in: "body",
        },
      },
    ],
  },
  generic: {
    version: 1,
    base_url: "https://api.example.com",
    operations: [
      {
        id: "readings",
        name: "Readings",
        kind: "read",
        method: "GET",
        endpoint: "/readings",
        parameters: [],
        response: { format: "json", mode: "document" },
      },
    ],
  },
};
export function parseServiceDefinition(
  text: unknown,
): APIServiceDefinition | null {
  try {
    const value = JSON.parse(String(text));
    return value?.version === 1 &&
      typeof value.base_url === "string" &&
      Array.isArray(value.operations) &&
      value.operations.length &&
      value.operations.every(
        (o: APIOperation) =>
          o &&
          typeof o.id === "string" &&
          typeof o.endpoint === "string" &&
          (o.name === undefined || typeof o.name === "string") &&
          (o.parameters === undefined ||
            (Array.isArray(o.parameters) &&
              o.parameters.every(
                (p) =>
                  p &&
                  typeof p.name === "string" &&
                  (p.label === undefined || typeof p.label === "string") &&
                  (p.description === undefined ||
                    typeof p.description === "string") &&
                  (p.enum === undefined ||
                    (Array.isArray(p.enum) &&
                      p.enum.every((v) => typeof v === "string"))),
              ))),
      )
      ? (value as APIServiceDefinition)
      : null;
  } catch {
    return null;
  }
}
