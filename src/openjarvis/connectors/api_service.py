"""Declarative, versioned service operations on the existing protected source path."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
from xml.etree import ElementTree

from openjarvis.connectors._stubs import Document
from openjarvis.connectors.web_sources import (
    JsonAPIConnector,
    _json_text,
    _pointer,
    _PublicSource,
    _reject_constant,
    _strict_pairs,
    validate_pointer,
    validate_web_config,
)
from openjarvis.security.public_http import (
    fetch_public_source,
    normalize_source_url,
    source_origin,
    validate_request_headers,
)

MAX_DEFINITION = 65536
_TYPES = {"string", "number", "integer", "boolean", "string_list"}
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def json_object(text, label):
    try:
        if not isinstance(text, str) or len(text.encode()) > MAX_DEFINITION:
            raise ValueError
        value = json.loads(
            text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant
        )
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError(f"{label} must be a JSON object of at most 64 KiB") from None


def typed_value(spec, value):
    kind = spec.get("type", "string")
    valid = {
        "string": isinstance(value, str),
        "number": type(value) in (int, float),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "string_list": isinstance(value, list)
        and len(value) <= 64
        and all(isinstance(v, str) and len(v) <= 256 for v in value),
    }
    if not valid.get(kind) or len(_json_text(value).encode()) > 4096:
        raise ValueError(f"Input {spec['name']} requires {kind}")
    if "enum" in spec and (
        any(v not in spec["enum"] for v in value)
        if kind == "string_list"
        else value not in spec["enum"]
    ):
        raise ValueError(f"Input {spec['name']} must use a listed choice")
    if kind in {"number", "integer"} and (
        ("min" in spec and value < spec["min"])
        or ("max" in spec and value > spec["max"])
    ):
        raise ValueError(f"Input {spec['name']} is outside its allowed range")
    return value


def reject_body_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {
                "api_key",
                "apikey",
                "token",
                "password",
                "secret",
                "authorization",
                "access_token",
                "client_secret",
            }:
                raise ValueError("Request credentials belong in the protected vault")
            reject_body_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_body_credentials(item)


def _validate_definition(value):
    """Validation never fetches schemas, executes scripts or grants tool authority."""
    value = deepcopy(value)
    if (
        not isinstance(value, dict)
        or len(_json_text(value).encode()) > MAX_DEFINITION
        or set(value)
        - {
            "version",
            "base_url",
            "description",
            "documentation_url",
            "headers",
            "operations",
        }
        or value.get("version") != 1
    ):
        raise ValueError("Use a version 1 API service definition")
    value["base_url"] = normalize_source_url(value.get("base_url", ""))
    base = urlparse(value["base_url"])
    if base.query or base.path != "/":
        raise ValueError("Service base URL must contain the origin only")
    if value.get("documentation_url"):
        value["documentation_url"] = normalize_source_url(value["documentation_url"])
    value["headers"] = validate_request_headers(value.get("headers", {}))
    operations = value.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 32:
        raise ValueError("A service needs 1–32 named operations")
    seen = set()
    for op in operations:
        if (
            not isinstance(op, dict)
            or set(op)
            - {
                "id",
                "name",
                "description",
                "kind",
                "method",
                "endpoint",
                "headers",
                "parameters",
                "body",
                "body_encoding",
                "steps",
                "response",
                "pagination",
            }
            or not isinstance(op.get("id"), str)
            or not _NAME.fullmatch(op["id"])
            or op["id"] in seen
        ):
            raise ValueError("Operations need unique identifiers and known fields")
        seen.add(op["id"])
        op.setdefault("kind", "read")
        op.setdefault("method", "GET")
        if op["kind"] not in {"read", "action"} or op["method"] not in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            raise ValueError("Unsupported operation kind or HTTP method")
        if op["kind"] == "read" and op["method"] not in {"GET", "POST"}:
            raise ValueError("Indexed reads support GET or declared read-only POST")
        endpoint = op.get("endpoint", "")
        if (
            not isinstance(endpoint, str)
            or not endpoint.startswith("/")
            or endpoint.startswith("//")
            or len(endpoint) > 4096
        ):
            raise ValueError(
                "Operation endpoint must be an absolute path on the service"
            )
        normalize_source_url(
            urljoin(
                value["base_url"],
                re.sub(r"\{[A-Za-z][A-Za-z0-9_]*\}", "input", endpoint),
            )
        )
        for key in ("name", "description"):
            if key in op and (not isinstance(op[key], str) or len(op[key]) > 4096):
                raise ValueError("Operation labels must be bounded text")
        op["headers"] = validate_request_headers(op.get("headers", {}))
        specs, names = op.get("parameters", []), set()
        if not isinstance(specs, list) or len(specs) > 64:
            raise ValueError("Use at most 64 operation inputs")
        for spec in specs:
            if (
                not isinstance(spec, dict)
                or set(spec)
                - {
                    "name",
                    "label",
                    "description",
                    "in",
                    "type",
                    "required",
                    "default",
                    "enum",
                    "min",
                    "max",
                    "style",
                    "required_when",
                }
                or not isinstance(spec.get("name"), str)
                or not _NAME.fullmatch(spec["name"])
                or spec["name"] in names
                or spec.get("type", "string") not in _TYPES
                or spec.get("in", "query") not in {"query", "path", "body", "variable"}
                or type(spec.get("required", False)) is not bool
            ):
                raise ValueError("Use unique typed path, query or body input names")
            names.add(spec["name"])
            for key in ("label", "description"):
                if key in spec and (
                    not isinstance(spec[key], str) or len(spec[key]) > 4096
                ):
                    raise ValueError("Input labels must be bounded text")
            if spec["name"].lower() in {
                "apikey",
                "api_key",
                "token",
                "password",
                "secret",
                "authorization",
                "key",
                "appid",
            }:
                raise ValueError("Credential inputs belong in the protected vault")
            if spec.get("style", "comma") not in {"comma", "repeat"}:
                raise ValueError("List style must be comma or repeat")
            if "enum" in spec and (
                not isinstance(spec["enum"], list)
                or len(spec["enum"]) > 200
                or any(not isinstance(v, str) for v in spec["enum"])
            ):
                raise ValueError("Input choices must be a bounded array")
            if any(
                type(spec[k]) not in (int, float) for k in ("min", "max") if k in spec
            ):
                raise ValueError("Input bounds must be numbers")
            if "default" in spec:
                typed_value(spec, spec["default"])
        for spec in specs:
            if "required_when" in spec and spec["required_when"] not in names:
                raise ValueError("Input dependency must reference another input")
        reject_body_credentials(op.get("body"))
        op.setdefault("body_encoding", "json")
        if op["body_encoding"] not in {"json", "form", "graphql", "text", "xml"}:
            raise ValueError("Unsupported request body encoding")
        response = op.setdefault("response", {})
        if not isinstance(response, dict) or set(response) - {
            "format",
            "mode",
            "records_pointer",
            "id_pointer",
            "title_pointer",
            "content_pointer",
            "time_pointer",
            "columns",
            "units_pointer",
            "timezone_pointer",
            "error_pointer",
            "success_statuses",
            "max_records",
            "xml_path",
        }:
            raise ValueError("Use known response mapping fields")
        if response.get("format", "json") not in {
            "json",
            "csv",
            "xml",
            "text",
        } or response.get("mode", "document") not in {"document", "records", "series"}:
            raise ValueError("Unsupported response format or mapping mode")
        for key, pointer in response.items():
            if key.endswith("_pointer"):
                validate_pointer(pointer)
        columns = response.get("columns", {})
        if not isinstance(columns, dict) or len(columns) > 64:
            raise ValueError("Use at most 64 time-series columns")
        for name, pointer in columns.items():
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError("Time-series columns need valid names")
            validate_pointer(pointer)
        limit = response.get("max_records", 1000)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Response record limit must be 1–1000")
        statuses = response.get("success_statuses", [200])
        if (
            not isinstance(statuses, list)
            or not statuses
            or len(statuses) > 10
            or any(type(s) is not int or not 200 <= s < 300 for s in statuses)
        ):
            raise ValueError("Success statuses must be HTTP 2xx codes")
        steps = op.get("steps", [])
        if not isinstance(steps, list) or len(steps) > 3:
            raise ValueError(
                "Use at most three linked requests after the initial request"
            )
        for step in steps:
            if not isinstance(step, dict) or set(step) != {"url_pointer"}:
                raise ValueError("Linked requests need a JSON URL pointer")
            validate_pointer(step["url_pointer"])
        page = op.get("pagination", {})
        if (
            not isinstance(page, dict)
            or set(page)
            - {
                "mode",
                "next_pointer",
                "parameter",
                "in",
                "max_pages",
                "start",
                "page_size",
                "has_more_pointer",
            }
            or page.get("mode", "none")
            not in {"none", "next_url", "cursor", "link", "page", "offset"}
        ):
            raise ValueError("Unsupported pagination settings")
        if (
            type(page.get("max_pages", 10)) is not int
            or not 1 <= page.get("max_pages", 10) <= 50
        ):
            raise ValueError("Pagination page limit must be 1–50")
        if (
            page.get("mode", "none") != "none"
            and response.get("mode", "document") != "records"
        ):
            raise ValueError("Pagination requires record response mapping")
        for key in ("next_pointer", "has_more_pointer"):
            if key in page:
                validate_pointer(page[key])
        if page.get("mode") in {"cursor", "next_url"} and not page.get("next_pointer"):
            raise ValueError("Cursor and next-URL pagination require a next pointer")
        if (
            page.get("in", "query") not in {"query", "body", "variables"}
            or not isinstance(page.get("parameter", "cursor"), str)
            or not _NAME.fullmatch(page.get("parameter", "cursor"))
        ):
            raise ValueError("Page continuation needs a valid query or body parameter")
        for key, default in (("start", 0), ("page_size", 100)):
            if (
                type(page.get(key, default)) is not int
                or not 0 <= page.get(key, default) <= 10000
            ):
                raise ValueError("Page counters must be bounded integers")
        if page.get("mode") == "offset" and page.get("page_size", 100) == 0:
            raise ValueError("Offset page size must be positive")
    return value


def validate_definition(value):
    try:
        return _validate_definition(value)
    except (TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
        raise ValueError("Invalid API definition field types or nesting") from None


def validate_service_config(config):
    definition = validate_definition(
        json_object(config.get("definition", ""), "Service definition")
    )
    operation_id = config.get("operation", definition["operations"][0]["id"])
    operation = next(
        (op for op in definition["operations"] if op["id"] == operation_id), None
    )
    if not operation:
        raise ValueError("Choose an operation from this service definition")
    if operation["kind"] != "read":
        raise ValueError(
            (
                "Actions require an approved tool adapter; they cannot "
                "be tested or synced as data sources"
            )
        )
    inputs = json_object(config.get("inputs", "{}"), "Operation inputs")
    known = {s["name"] for s in operation.get("parameters", [])}
    if set(inputs) - known:
        raise ValueError("Operation inputs contain unknown names")
    for spec in operation.get("parameters", []):
        if spec["name"] not in inputs and "default" in spec:
            inputs[spec["name"]] = spec["default"]
    for spec in operation.get("parameters", []):
        name = spec["name"]
        if name in inputs:
            typed_value(spec, inputs[name])
        elif spec.get("required") or inputs.get(spec.get("required_when", "")):
            raise ValueError(f"Input {name} is required")
    url, _, _ = compile_request(definition, operation, inputs)
    validated = validate_web_config(
        {"url": url, "credential_id": config.get("credential_id", "")}
    )
    return {
        "definition": _json_text(definition),
        "operation": operation_id,
        "inputs": _json_text(inputs),
        "credential_id": validated.get("credential_id", ""),
        "url": url,
    }


def expand(value, inputs):
    if isinstance(value, dict):
        return {k: expand(v, inputs) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, inputs) for v in value]
    if isinstance(value, str):
        matched = re.fullmatch(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}", value)
        if matched:
            if matched[1] not in inputs:
                raise ValueError("Request template references a missing input")
            return inputs[matched[1]]
        for name in re.findall(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}", value):
            if name not in inputs:
                raise ValueError("Request template references a missing input")
            value = value.replace("{{" + name + "}}", str(inputs[name]))
    return value


def compile_request(definition, operation, inputs):
    endpoint = operation["endpoint"]
    query = []
    body = expand(operation.get("body"), inputs)
    for spec in operation.get("parameters", []):
        name = spec["name"]
        if name not in inputs:
            continue
        value = inputs[name]
        location = spec.get("in", "query")
        if location == "path":
            endpoint = endpoint.replace("{" + name + "}", quote(str(value), safe=""))
        elif location == "query":
            values = (
                value
                if isinstance(value, list) and spec.get("style") == "repeat"
                else [",".join(value) if isinstance(value, list) else value]
            )
            query.extend(
                (name, str(v).lower() if type(v) is bool else str(v)) for v in values
            )
        elif location == "body":
            if body is None:
                body = {}
            if not isinstance(body, dict):
                raise ValueError("Body inputs require an object request body")
            body[name] = value
    if "{" in endpoint or "}" in endpoint:
        raise ValueError("Endpoint contains unresolved path inputs")
    parsed = urlparse(urljoin(definition["base_url"], endpoint))
    query = parse_qsl(parsed.query, keep_blank_values=True) + query
    url = normalize_source_url(urlunparse(parsed._replace(query=urlencode(query))))
    if source_origin(url) != source_origin(definition["base_url"]):
        raise ValueError("Operation must remain on its configured origin")
    headers = {**definition["headers"], **operation["headers"]}
    if body is None:
        encoded = None
    elif operation["body_encoding"] in {"json", "graphql"}:
        encoded = _json_text(body).encode()
        headers["Content-Type"] = "application/json"
    elif operation["body_encoding"] == "form":
        if not isinstance(body, dict):
            raise ValueError("Form request body must be an object")
        encoded = urlencode(body, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        if not isinstance(body, str):
            raise ValueError("Text and XML request bodies must be strings")
        encoded = body.encode()
        headers["Content-Type"] = (
            "application/xml" if operation["body_encoding"] == "xml" else "text/plain"
        )
    if encoded is not None and (operation["method"] == "GET" or len(encoded) > 65536):
        raise ValueError("Use POST for a body of at most 64 KiB")
    return url, headers, encoded


def decode_response(response, mapping):
    kind = mapping.get("format", "json")
    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind == "json":
        return JsonAPIConnector()._decode(response)
    text = response.content.decode("utf-8-sig")
    if kind == "csv":
        if mime not in {"text/csv", "application/csv", "text/plain"}:
            raise ValueError("Expected a CSV response")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(
            reader.fieldnames
        ):
            raise ValueError("CSV needs unique column names")
        rows = []
        for row in reader:
            if len(rows) >= 1000 or None in row:
                raise ValueError("CSV exceeds record limits or contains invalid rows")
            rows.append(row)
        return rows
    if kind == "xml":
        if mime not in {"application/xml", "text/xml"} and not mime.endswith("+xml"):
            raise ValueError("Expected an XML response")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("XML DTDs and entities are not supported")
        try:
            root = ElementTree.fromstring(text)

            def convert(node, depth=0):
                if depth > 32:
                    raise ValueError("XML nesting exceeds limits")
                if not len(node):
                    return node.text or ""
                result = dict(node.attrib)
                for child in node:
                    result.setdefault(child.tag, []).append(convert(child, depth + 1))
                return {
                    k: v[0] if isinstance(v, list) and len(v) == 1 else v
                    for k, v in result.items()
                }

            if mapping.get("xml_path"):
                nodes = root.findall(mapping["xml_path"])
                if len(nodes) > 1000:
                    raise ValueError("XML exceeds record limits")
                return [convert(n) for n in nodes]
            return convert(root)
        except (ElementTree.ParseError, SyntaxError, KeyError):
            raise ValueError("Invalid XML response") from None
    if mime not in {"text/plain", "text/markdown"}:
        raise ValueError("Expected a plain text response")
    return text


class APIServiceConnector(_PublicSource):
    connector_id = "api_service"
    display_name = "API service"
    required_capabilities = ("connector:api_service:read", "network:fetch")

    def _documents(self, *, since=None, cursor=None):
        self.config = validate_service_config(self.config)
        definition = json_object(self.config["definition"], "Service definition")
        op = next(
            o for o in definition["operations"] if o["id"] == self.config["operation"]
        )
        mapping = op["response"]
        current, headers, body = compile_request(
            definition, op, json_object(self.config["inputs"], "Inputs")
        )
        origin, deadline = source_origin(current), time.monotonic() + 60
        pagination = op.get("pagination", {})
        visited, tokens, documents, identities, trace = set(), set(), [], set(), []
        total_bytes, links = 0, 0
        method = op["method"]
        if pagination.get("mode") in {"page", "offset"}:
            start = pagination.get("start", 0)
            parameter = pagination.get("parameter", "cursor")
            if pagination.get("in", "query") == "query":
                current = JsonAPIConnector._query(current, parameter, str(start))
            else:
                if method != "POST" or op["body_encoding"] not in {"json", "graphql"}:
                    raise ValueError("Body pagination requires a JSON POST request")
                payload = json.loads(body or b"{}")
                target = (
                    payload.setdefault("variables", {})
                    if pagination["in"] == "variables"
                    else payload
                )
                target[parameter] = start
                body = _json_text(payload).encode()
        for page in range(pagination.get("max_pages", 10) + len(op.get("steps", []))):
            self.check_sync_cancelled()
            identity = (current, body)
            if identity in visited:
                raise ValueError("API request or pagination loop detected")
            visited.add(identity)
            response = fetch_public_source(
                current,
                accept=next(
                    (v for k, v in headers.items() if k.lower() == "accept"),
                    "application/json",
                ),
                request_headers={
                    k: v for k, v in headers.items() if k.lower() != "accept"
                },
                method=method,
                body=body,
                follow_redirects=method == "GET",
                allowed_origin=origin,
                deadline=deadline,
                authentication=self._authentication,
                success_statuses=tuple(mapping.get("success_statuses", [200])),
                read_errors=True,
                **(
                    {"cancel_event": self._sync_control}
                    if getattr(self, "_sync_control", None)
                    else {}
                ),
            )
            total_bytes += len(response.content)
            if total_bytes > 10 * 1024 * 1024 or time.monotonic() >= deadline:
                raise ValueError("API scan exceeded its byte or time budget")
            if self._authentication:
                self._reject_reflection(
                    response.content.decode("utf-8", errors="replace")
                )
            fetched = datetime.now(timezone.utc)
            trace.append(
                {
                    "method": method,
                    "url": str(response.url),
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "bytes": len(response.content),
                }
            )
            self.request_trace = trace
            if response.status_code == 204:
                return []
            value = decode_response(
                response,
                mapping if links >= len(op.get("steps", [])) else {"format": "json"},
            )
            if self._authentication:
                self._reject_reflection(_json_text(value))
            if links < len(op.get("steps", [])):
                linked = _pointer(value, op["steps"][links]["url_pointer"])
                if not isinstance(linked, str):
                    raise ValueError("Linked response must contain a URL string")
                current = normalize_source_url(urljoin(current, linked))
                if source_origin(current) != origin:
                    raise ValueError(
                        "Linked requests must remain on the configured origin"
                    )
                method, body, links = "GET", None, links + 1
                continue
            error_pointer = mapping.get(
                "error_pointer", "/errors" if op["body_encoding"] == "graphql" else ""
            )
            if error_pointer:
                try:
                    error = _pointer(value, error_pointer)
                except ValueError:
                    error = None
                if error:
                    raise ValueError(
                        (
                            "API response reports an application error; check the "
                            "configured operation"
                        )
                    )
            metadata = {
                "origin": "external",
                "trust": "auto",
                "requested_url": self.config["url"],
                "final_url": str(response.url),
                "fetched_at": fetched.isoformat(),
                "response_version": hashlib.sha256(response.content).hexdigest(),
                "operation_id": op["id"],
                "content_type": response.headers.get("content-type", ""),
            }
            for key in ("units_pointer", "timezone_pointer"):
                if mapping.get(key):
                    metadata[key.removesuffix("_pointer")] = _pointer(
                        value, mapping[key]
                    )
            mode = mapping.get("mode", "document")
            if mode == "series":
                times = _pointer(value, mapping.get("time_pointer", "/hourly/time"))
                columns = {
                    name: _pointer(value, pointer)
                    for name, pointer in mapping.get("columns", {}).items()
                }
                if (
                    not isinstance(times, list)
                    or not columns
                    or any(
                        not isinstance(v, list) or len(v) != len(times)
                        for v in columns.values()
                    )
                ):
                    raise ValueError(
                        "Time-series columns must align with the time array"
                    )
                value = [
                    {
                        "id": t,
                        "time": t,
                        **{name: column[i] for name, column in columns.items()},
                    }
                    for i, t in enumerate(times)
                ]
                rows = value
            elif mode == "records":
                rows = _pointer(value, mapping.get("records_pointer", ""))
            else:
                rows = [value]
            if not isinstance(rows, list) or len(rows) + len(documents) > mapping.get(
                "max_records", 1000
            ):
                raise ValueError("Response mapping needs a bounded record array")
            for row in rows:
                record_id = (
                    _pointer(row, mapping.get("id_pointer", "/id"))
                    if mode in {"records", "series"}
                    else op["id"]
                )
                if (
                    type(record_id) not in (str, int)
                    or record_id == ""
                    or _json_text(record_id) in identities
                ):
                    raise ValueError(
                        "API records need unique stable string or integer IDs"
                    )
                identities.add(_json_text(record_id))
                title = (
                    _pointer(row, mapping["title_pointer"])
                    if mapping.get("title_pointer")
                    else str(record_id)
                )
                content = (
                    _pointer(row, mapping["content_pointer"])
                    if mapping.get("content_pointer")
                    else row
                )
                text = content if isinstance(content, str) else _json_text(content)
                if metadata.get("units") or metadata.get("timezone"):
                    text = _json_text(
                        {
                            "data": content,
                            **{
                                k: metadata[k]
                                for k in ("units", "timezone")
                                if k in metadata
                            },
                        }
                    )
                native = hashlib.sha256(
                    (self.config["url"] + op["id"] + _json_text(record_id)).encode()
                ).hexdigest()
                documents.append(
                    Document(
                        doc_id="api_service:" + native,
                        source_id=native,
                        source="api_service",
                        doc_type="document",
                        title=str(title),
                        content=text,
                        url=str(response.url),
                        timestamp=fetched,
                        metadata={
                            **metadata,
                            "record_id": record_id,
                            "version": hashlib.sha256(text.encode()).hexdigest(),
                        },
                    )
                )
            page_mode = pagination.get("mode", "none")
            continuation = None
            if page_mode == "link":
                next_links = [
                    link
                    for link in response.links.values()
                    if link.get("rel") == "next"
                ]
                continuation = next_links[0]["url"] if next_links else None
            elif page_mode in {"cursor", "next_url"}:
                continuation = _pointer(value, pagination["next_pointer"])
            elif page_mode in {"page", "offset"}:
                has_more = (
                    _pointer(value, pagination["has_more_pointer"])
                    if pagination.get("has_more_pointer")
                    else len(rows) == pagination.get("page_size", 100)
                )
                continuation = (
                    pagination.get("start", 0)
                    + (page + 1 - links)
                    * (pagination.get("page_size", 100) if page_mode == "offset" else 1)
                    if has_more
                    else None
                )
            if continuation in (None, "") or page_mode == "none":
                return documents
            token = JsonAPIConnector._token(continuation)
            if token in tokens:
                raise ValueError("API repeated a continuation token")
            tokens.add(token)
            if page_mode in {"next_url", "link"}:
                current = normalize_source_url(urljoin(current, token))
            elif pagination.get("in", "query") in {"body", "variables"}:
                if method != "POST" or op["body_encoding"] not in {"json", "graphql"}:
                    raise ValueError("Body pagination requires a JSON POST request")
                payload = json.loads(body or b"{}")
                target = (
                    payload.setdefault("variables", {})
                    if pagination["in"] == "variables"
                    else payload
                )
                if not isinstance(target, dict):
                    raise ValueError("Pagination variables must be an object")
                target[pagination.get("parameter", "cursor")] = continuation
                body = _json_text(payload).encode()
            else:
                current = JsonAPIConnector._query(
                    current, pagination.get("parameter", "cursor"), token
                )
            if source_origin(current) != origin:
                raise ValueError("Pagination must remain on the configured origin")
        raise ValueError("API page limit reached before the scan completed")


def probe_service(reader):
    documents = list(reader.sync())
    return {
        "documents": len(documents),
        "final_url": documents[0].url if documents else reader.config["url"],
        "request_trace": getattr(reader, "request_trace", []),
        "sample_documents": [
            {
                "title": d.title[:500],
                "content": d.content[:4096],
                "truncated": len(d.content) > 4096,
                "fetched_at": d.metadata["fetched_at"],
            }
            for d in documents[:3]
        ],
    }
