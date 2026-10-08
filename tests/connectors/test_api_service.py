"""Declarative API reads are bounded, origin-scoped and atomic before ingestion."""

import json
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from openjarvis.connectors import api_service
from openjarvis.connectors.api_service import (
    APIServiceConnector,
    decode_response,
    probe_service,
    validate_service_config,
)
from openjarvis.connectors.api_templates import APITemplates, import_definition
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict, SourceStore
from openjarvis.security.public_http import validate_request_headers


def definition(**operation):
    return {
        "version": 1,
        "base_url": "https://api.example.com",
        "operations": [{"id": "read", "endpoint": "/data", **operation}],
    }


def reader(value=None, inputs=None):
    return APIServiceConnector(
        config={
            "definition": json.dumps(value or definition()),
            "inputs": json.dumps(inputs or {}),
        }
    )


def response(
    value, *, mime="application/json", headers=None, url="https://api.example.com/data"
):
    content = value.encode() if isinstance(value, str) else json.dumps(value).encode()
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": mime, **(headers or {})},
        request=httpx.Request("GET", url),
    )


@pytest.fixture
def fetch(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(api_service, "fetch_public_source", mock)
    return mock


@pytest.fixture
def manager(tmp_path):
    return SourceManager(
        SourceStore(str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "legacy")),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )


def test_linked_weather_reads_preserve_headers_and_origin(fetch):
    service = definition(
        endpoint="/points/{latitude},{longitude}",
        parameters=[
            {
                "name": "latitude",
                "in": "path",
                "type": "number",
                "required": True,
                "min": -90,
                "max": 90,
            },
            {"name": "longitude", "in": "path", "type": "number", "required": True},
        ],
        steps=[{"url_pointer": "/properties/forecast"}],
        response={
            "mode": "records",
            "records_pointer": "/properties/periods",
            "id_pointer": "/number",
        },
    )
    service["headers"] = {
        "User-Agent": "Jarvis contact@example.com",
        "Accept": "application/geo+json",
    }
    fetch.side_effect = [
        response({"properties": {"forecast": "https://api.example.com/forecast"}}),
        response(
            {
                "properties": {
                    "periods": [
                        {"number": 1, "temperature": 72, "temperatureUnit": "F"}
                    ]
                }
            }
        ),
    ]
    result = probe_service(reader(service, {"latitude": 40, "longitude": -75}))
    assert result["documents"] == 1 and len(result["request_trace"]) == 2
    assert fetch.call_args_list[0].args[0].endswith("/points/40,-75")
    assert fetch.call_args_list[1].args[0].endswith("/forecast")
    assert fetch.call_args.kwargs["request_headers"]["User-Agent"].startswith("Jarvis")
    assert fetch.call_args.kwargs["accept"] == "application/geo+json"
    assert "temperatureUnit" in result["sample_documents"][0]["content"]


def test_series_keeps_units_timezone_and_alignment(fetch):
    service = definition(
        response={
            "mode": "series",
            "time_pointer": "/hourly/time",
            "columns": {"temperature": "/hourly/temperature"},
            "units_pointer": "/units",
            "timezone_pointer": "/timezone",
        }
    )
    fetch.return_value = response(
        {
            "hourly": {"time": ["2026-10-08T01:00"], "temperature": [72]},
            "units": {"temperature": "F"},
            "timezone": "America/New_York",
        }
    )
    documents = list(reader(service).sync())
    assert json.loads(documents[0].content)["units"] == {"temperature": "F"}
    assert documents[0].metadata["timezone"] == "America/New_York"
    fetch.return_value = response(
        {
            "hourly": {"time": ["a", "b"], "temperature": [72]},
            "units": {},
            "timezone": "UTC",
        }
    )
    with pytest.raises(ValueError, match="align"):
        list(reader(service).sync())


def test_body_cursor_and_graphql_variables(fetch):
    service = definition(
        method="POST",
        body_encoding="graphql",
        body={"query": "query($after: String){items}", "variables": {}},
        response={"mode": "records", "records_pointer": "/data/items"},
        pagination={
            "mode": "cursor",
            "next_pointer": "/data/next",
            "parameter": "after",
            "in": "variables",
        },
    )
    fetch.side_effect = [
        response({"data": {"items": [{"id": 1}], "next": "cursor-2"}}),
        response({"data": {"items": [{"id": 2}], "next": None}}),
    ]
    assert len(list(reader(service).sync())) == 2
    assert json.loads(fetch.call_args_list[1].kwargs["body"])["variables"] == {
        "after": "cursor-2"
    }
    assert fetch.call_args.kwargs["method"] == "POST"
    fetch.side_effect = None
    fetch.return_value = response(
        {"data": {}, "errors": [{"message": "provider error"}]}
    )
    with pytest.raises(ValueError, match="application error"):
        list(reader(service).sync())


def test_link_pagination_and_initial_counter(fetch):
    service = definition(response={"mode": "records"}, pagination={"mode": "link"})
    fetch.side_effect = [
        response(
            [{"id": 1}],
            headers={"link": '<https://api.example.com/data?page=2>; rel="next"'},
        ),
        response([{"id": 2}]),
    ]
    assert len(list(reader(service).sync())) == 2
    assert fetch.call_args.args[0].endswith("?page=2")
    service["operations"][0]["pagination"] = {
        "mode": "page",
        "parameter": "page",
        "start": 1,
        "page_size": 1,
    }
    fetch.side_effect = [response([{"id": 1}]), response([])]
    assert len(list(reader(service).sync())) == 1
    assert parse_qs(urlparse(fetch.call_args_list[-2].args[0]).query) == {"page": ["1"]}
    assert parse_qs(urlparse(fetch.call_args.args[0]).query) == {"page": ["2"]}


@pytest.mark.parametrize(
    "continuation",
    [
        "https://other.example.com/data",
        "https://127.0.0.1/data",
        "https://api.example.com/data",
    ],
)
def test_unsafe_or_looping_continuations_emit_no_partial_documents(fetch, continuation):
    fetch.return_value = response({"items": [{"id": 1}], "next": continuation})
    service = definition(
        response={"mode": "records", "records_pointer": "/items"},
        pagination={"mode": "next_url", "next_pointer": "/next"},
    )
    iterator = reader(service).sync()
    with pytest.raises(ValueError):
        next(iterator)


def test_page_limit_fails_closed_before_indexing(fetch, manager):
    fetch.return_value = response(
        {"items": [{"id": 1}], "next": "https://api.example.com/page2"}
    )
    service = definition(
        response={"mode": "records", "records_pointer": "/items"},
        pagination={"mode": "next_url", "next_pointer": "/next", "max_pages": 1},
    )
    source = manager.create(
        "api_service", "Readings", {"definition": json.dumps(service)}
    )
    with pytest.raises(ValueError, match="page limit"):
        manager.sync(source["id"])
    assert manager.list()[0]["chunks"] == 0


@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "action", "method": "POST"},
        {"endpoint": "/data?api_key=do-not-store"},
        {"headers": {"Authorization": "Bearer do-not-store"}},
        {"parameters": [{"name": "token"}]},
        {"parameters": [{"name": "count", "type": "integer", "default": True}]},
        {"parameters": [{"name": "count", "type": "integer", "max": 3, "default": 4}]},
        {"pagination": {"mode": "cursor", "next_pointer": "/next"}},
    ],
)
def test_invalid_or_action_config_rejected(operation):
    with pytest.raises(ValueError):
        validate_service_config({"definition": json.dumps(definition(**operation))})


def test_defaults_dependencies_repeat_lists_and_escaped_path():
    service = definition(
        endpoint="/items/{owner}",
        parameters=[
            {"name": "dependent", "required_when": "enabled"},
            {"name": "enabled", "type": "boolean", "default": True},
            {"name": "owner", "in": "path", "required": True},
            {"name": "tag", "type": "string_list", "style": "repeat"},
        ],
    )
    config = {
        "definition": json.dumps(service),
        "inputs": json.dumps({"owner": "x/y", "tag": ["a", "b"]}),
    }
    with pytest.raises(ValueError, match="dependent"):
        validate_service_config(config)
    config["inputs"] = json.dumps(
        {"dependent": "yes", "owner": "x/y", "tag": ["a", "b"]}
    )
    result = validate_service_config(config)
    assert "/items/x%2Fy?" in result["url"]
    assert parse_qs(urlparse(result["url"]).query)["tag"] == ["a", "b"]


def test_safe_csv_xml_and_text_parsing():
    assert decode_response(
        response("id,value\n1,72\n", mime="text/csv"), {"format": "csv"}
    ) == [{"id": "1", "value": "72"}]
    assert decode_response(
        response("<items><item><id>1</id></item></items>", mime="application/xml"),
        {"format": "xml", "xml_path": "./item"},
    ) == [{"id": "1"}]
    assert (
        decode_response(
            response("<script>unsafe()</script>", mime="text/plain"), {"format": "text"}
        )
        == "<script>unsafe()</script>"
    )
    with pytest.raises(ValueError, match="DTDs"):
        decode_response(
            response(
                '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>',
                mime="application/xml",
            ),
            {"format": "xml"},
        )
    with pytest.raises(ValueError):
        decode_response(response("id,id\n1,2", mime="text/csv"), {"format": "csv"})


def test_templates_are_owner_scoped_revision_checked_and_detached(manager):
    templates = APITemplates(manager.store)
    saved = templates.save("alice", "Readings", definition())
    assert templates.list("bob") == []
    with pytest.raises(SourceConflict):
        templates.remove("bob", saved["id"], 1)
    updated = templates.save(
        "alice", "Updated", definition(endpoint="/new"), saved["id"], 1
    )
    assert updated["revision"] == 2
    assert saved["definition"]["operations"][0]["endpoint"] == "/data"
    with pytest.raises(SourceConflict):
        templates.save("alice", "Stale", definition(), saved["id"], 1)
    templates.remove("alice", saved["id"], 2)
    assert not templates.list("alice")


@pytest.mark.parametrize(
    "text",
    [
        "curl https://api.example.com/data?apikey=secret",
        'curl https://api.example.com/data -H "Authorization: Bearer secret"',
        "curl https://api.example.com/data -u user:password",
        "curl https://api.example.com/data --output /tmp/file",
        "curl https://api.example.com/data ; echo hello",
    ],
)
def test_import_rejects_credentials_and_shell_or_file_options(text):
    with pytest.raises(ValueError):
        import_definition(text)


def test_import_does_not_execute_and_preserves_read_action_distinction():
    value = import_definition(
        'curl https://api.example.com/data -H "User-Agent: Jarvis"'
    )
    assert value["operations"][0]["kind"] == "read"
    value = import_definition('curl https://api.example.com/data -d \'{"query":"x"}\'')
    assert value["operations"][0]["kind"] == "action"
    spec = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/items/{id}": {
                "get": {
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ]
                }
            }
        },
    }
    assert (
        import_definition(json.dumps(spec))["operations"][0]["endpoint"]
        == "/v1/items/{id}"
    )


@pytest.mark.parametrize(
    "kind,secret,header",
    [
        ("query_api_key", "protected-key", "api_key"),
        ("basic", "user:protected-password", ""),
    ],
)
def test_new_credentials_encrypted_bound_and_never_reflected(
    manager, fetch, kind, secret, header
):
    saved = manager.credentials.create(
        "Readings", kind, "https://api.example.com", secret, header_name=header
    )
    config = {"definition": json.dumps(definition()), "credential_id": saved["id"]}
    captured = []

    def fetched(*args, **kwargs):
        from copy import deepcopy

        captured.append(deepcopy(kwargs["authentication"]))
        return response({"value": 72})

    fetch.side_effect = fetched
    assert manager.test("api_service", config)["documents"] == 1
    if kind == "query_api_key":
        assert captured[0]["kind"] == kind and captured[0]["query"] == {header: secret}
    else:
        assert captured[0]["headers"]["Authorization"].startswith("Basic ")
    assert secret not in json.dumps(saved)
    fetch.side_effect = None
    fetch.return_value = response({"reflected": secret})
    with pytest.raises(ValueError):
        manager.test("api_service", config)


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil"},
        {"X-API-Key": "secret"},
        {"X-Version": "v1\r\nx"},
        {"Accept": "a", "accept": "b"},
    ],
)
def test_headers_block_transport_secret_and_injection(headers):
    with pytest.raises(ValueError):
        validate_request_headers(headers)


def test_generic_query_auth_stays_on_wire_and_does_not_follow_redirects(monkeypatch):
    from openjarvis.security import public_http

    target = public_http.PublicTarget(
        "https", "api.example.com", 443, "/data", "api.example.com", ("93.184.216.34",)
    )
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: target)
    wire = MagicMock(return_value=response({"value": 72}))
    monkeypatch.setattr(public_http, "_request_source", wire)
    auth = {
        "kind": "query_api_key",
        "origin": "https://api.example.com",
        "secret": "protected-key",
        "headers": {},
        "query": {"api_key": "protected-key"},
    }
    result = public_http.fetch_public_source(
        "https://api.example.com/data", accept="application/json", authentication=auth
    )
    assert wire.call_args.kwargs["credential_query"] == {"api_key": "protected-key"}
    assert "protected-key" not in str(result.url)
    wire.return_value = httpx.Response(
        302,
        headers={"location": "https://api.example.com/page2"},
        request=httpx.Request("GET", "https://api.example.com/data"),
    )
    with pytest.raises(ValueError, match="redirect"):
        public_http.fetch_public_source(
            "https://api.example.com/data",
            accept="application/json",
            authentication=auth,
        )


def test_case_insensitive_header_override_and_auth_error_sanitization(monkeypatch):
    from openjarvis.security import public_http

    headers = public_http._merge_request_headers(
        {"User-Agent": "default", "user-agent": "configured"}
    )
    assert headers == {"user-agent": "configured"}
    target = public_http.PublicTarget(
        "https", "api.example.com", 443, "/data", "api.example.com", ("93.184.216.34",)
    )
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: target)
    monkeypatch.setattr(
        public_http,
        "_request_source",
        lambda *args, **kwargs: httpx.Response(
            400,
            json={"reason": "location is outside coverage"},
            request=httpx.Request("GET", "https://api.example.com/data"),
        ),
    )
    with pytest.raises(ValueError, match="outside coverage"):
        public_http.fetch_public_source(
            "https://api.example.com/data", accept="application/json", read_errors=True
        )
    with pytest.raises(ValueError) as error:
        public_http.fetch_public_source(
            "https://api.example.com/data",
            accept="application/json",
            read_errors=True,
            authentication={
                "origin": "https://api.example.com",
                "secret": "protected-key",
                "headers": {"Authorization": "Bearer protected-key"},
            },
        )
    assert "outside coverage" not in str(error.value)


def test_basic_password_spaces_and_rotation_stay_encrypted(manager):
    saved = manager.credentials.create(
        "Basic", "basic", "https://api.example.com", "user:long password"
    )
    updated = manager.credentials.rotate(
        saved["id"], saved["revision"], "user:new password"
    )
    with manager.credentials.bound(
        updated["id"], "https://api.example.com/data", ("basic",)
    ) as row:
        import base64

        value = manager.credentials.material(row)["headers"]["Authorization"]
        assert (
            base64.b64decode(value.removeprefix("Basic ")).decode()
            == "user:new password"
        )


def test_query_credential_cannot_conflict_with_operation_input(monkeypatch):
    from openjarvis.security import public_http

    monkeypatch.setattr(
        public_http,
        "validate_public_url",
        lambda url: pytest.fail("No network before validation"),
    )
    with pytest.raises(ValueError, match="conflicts"):
        public_http.fetch_public_source(
            "https://api.example.com/data?custom=public-value",
            accept="application/json",
            authentication={
                "kind": "query_api_key",
                "origin": "https://api.example.com",
                "secret": "protected-key",
                "headers": {},
                "query": {"custom": "protected-key"},
            },
        )
