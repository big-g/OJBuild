"""Owner-scoped reusable service templates; copies never change live connections."""

import json
import shlex
import time
import uuid
from urllib.parse import urlparse

from openjarvis.connectors.api_service import validate_definition
from openjarvis.connectors.source_store import SourceConflict


class APITemplates:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS api_service_templates (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
                revision INTEGER NOT NULL, definition TEXT NOT NULL,
                updated_at REAL NOT NULL
            )""")

    @staticmethod
    def public(row):
        value = dict(row)
        value.pop("owner_id", None)
        value["definition"] = json.loads(value["definition"])
        return value

    def list(self, owner):
        with self.store.connection() as db:
            return [
                self.public(r)
                for r in db.execute(
                    (
                        "SELECT * FROM api_service_templates WHERE owner_id=? "
                        "ORDER BY name"
                    ),
                    (owner,),
                )
            ]

    def save(self, owner, name, definition, identity="", revision=0):
        definition = validate_definition(definition)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("Template name must contain 1–120 characters")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if identity:
                row = db.execute(
                    (
                        "SELECT revision FROM api_service_templates WHERE id=? "
                        "AND owner_id=?"
                    ),
                    (identity, owner),
                ).fetchone()
                if row is None or row[0] != revision:
                    raise SourceConflict(
                        "Template changed or unavailable. Refresh and retry"
                    )
                db.execute(
                    (
                        "UPDATE api_service_templates SET "
                        "name=?,definition=?,revision=revision+1,"
                        "updated_at=? WHERE id=?"
                    ),
                    (name.strip(), json.dumps(definition), time.time(), identity),
                )
            else:
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM api_service_templates WHERE owner_id=?",
                        (owner,),
                    ).fetchone()[0]
                    >= 200
                ):
                    raise ValueError("Maximum of 200 API templates per account reached")
                identity = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO api_service_templates VALUES (?,?,?,1,?,?)",
                    (
                        identity,
                        owner,
                        name.strip(),
                        json.dumps(definition),
                        time.time(),
                    ),
                )
            return self.public(
                db.execute(
                    "SELECT * FROM api_service_templates WHERE id=?", (identity,)
                ).fetchone()
            )

    def remove(self, owner, identity, revision):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                (
                    "DELETE FROM api_service_templates WHERE id=? AND "
                    "owner_id=? AND revision=?"
                ),
                (identity, owner, revision),
            )
            if result.rowcount != 1:
                raise SourceConflict(
                    "Template changed or unavailable. Refresh and retry"
                )


def _import_definition(text):
    """Local parsing only. No shell execution, network lookup or authorization."""
    if not isinstance(text, str) or len(text.encode()) > 65536:
        raise ValueError("Import is limited to 64 KiB")
    if text.lstrip().startswith("curl "):
        tokens = shlex.split(text)
        method, headers, body, url = "GET", {}, None, None
        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token in {
                "-H",
                "--header",
                "-X",
                "--request",
                "-d",
                "--data",
                "--data-raw",
                "--url",
            }:
                if i + 1 >= len(tokens):
                    raise ValueError("Incomplete cURL option")
                value = tokens[i + 1]
                if token in {"-H", "--header"}:
                    name, separator, content = value.partition(":")
                    if not separator:
                        raise ValueError("cURL header needs a name and value")
                    headers[name.strip()] = content.strip()
                elif token in {"-X", "--request"}:
                    method = value.upper()
                elif token in {"-d", "--data", "--data-raw"}:
                    body = value
                    method = "POST"
                else:
                    url = value
                i += 2
            elif token.startswith("https://") or token.startswith("http://"):
                url = token
                i += 1
            elif token in {"--compressed", "--silent", "-s", "--show-error", "-S"}:
                i += 1
            else:
                raise ValueError(
                    (
                        "Unsupported cURL option. Remove credentials and shell "
                        "expressions before import"
                    )
                )
        if not url:
            raise ValueError("cURL import needs a URL")
        parsed = urlparse(url)
        encoding = "text"
        if body is not None:
            try:
                body = json.loads(body)
                encoding = "json"
            except ValueError:
                pass
        result = {
            "version": 1,
            "base_url": f"{parsed.scheme}://{parsed.netloc}",
            "headers": headers,
            "operations": [
                {
                    "id": "imported",
                    "method": method,
                    "kind": "read" if method == "GET" else "action",
                    "endpoint": parsed.path
                    + ("?" + parsed.query if parsed.query else ""),
                    "body_encoding": encoding,
                    **({"body": body} if body is not None else {}),
                }
            ],
        }
        return validate_definition(result)
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError(
            "Import a service JSON definition, OpenAPI 3 JSON or a cURL request"
        ) from None
    if not isinstance(value, dict):
        raise ValueError("Imported definition must be an object")
    if "openapi" not in value:
        return validate_definition(value)
    if not str(value["openapi"]).startswith("3."):
        raise ValueError("OpenAPI import supports version 3 JSON")
    servers = value.get("servers", [])
    if not servers or "{" in servers[0].get("url", ""):
        raise ValueError("OpenAPI needs a concrete server URL")
    base = servers[0]["url"]
    parsed = urlparse(base)
    operations = []
    for path, methods in value.get("paths", {}).items():
        for method, op in methods.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            params = []
            for p in methods.get("parameters", []) + op.get("parameters", []):
                if "$ref" in p or "$ref" in p.get("schema", {}):
                    raise ValueError(
                        "Resolve OpenAPI parameter references before import"
                    )
                if p.get("in") not in {"query", "path"}:
                    continue
                schema = p.get("schema", {})
                kind = (
                    "string_list"
                    if schema.get("type") == "array"
                    else schema.get("type", "string")
                )
                params.append(
                    {
                        "name": p["name"],
                        "in": p["in"],
                        "type": kind,
                        "required": p.get("required", False),
                        **{k: schema[k] for k in ("default", "enum") if k in schema},
                        **({"min": schema["minimum"]} if "minimum" in schema else {}),
                        **({"max": schema["maximum"]} if "maximum" in schema else {}),
                    }
                )
            operations.append(
                {
                    "id": f"operation_{len(operations) + 1}",
                    "name": op.get("summary", method + " " + path),
                    "endpoint": parsed.path.rstrip("/") + path,
                    "method": method.upper(),
                    "kind": "read" if method.lower() == "get" else "action",
                    "parameters": params,
                }
            )
    if not operations:
        raise ValueError("OpenAPI contains no supported operations")
    return validate_definition(
        {
            "version": 1,
            "base_url": f"{parsed.scheme}://{parsed.netloc}",
            "operations": operations,
        }
    )


def import_definition(text):
    try:
        return _import_definition(text)
    except (TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
        raise ValueError("Invalid API import field types or nesting") from None
