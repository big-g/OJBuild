"""Strict named Dropbox, Granola, Oura, GitHub and Weather reads."""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

from openjarvis.connectors.account_scans import (
    AccountScan,
    identity,
    path_id,
    rows,
    timestamp,
)
from openjarvis.connectors.dropbox import _TEXT_EXTENSIONS


class ProviderSource(AccountScan):
    def _documents(self, *, since=None, cursor=None):
        getattr(self, "_" + self.connector_id)()
        self.check()
        return list(self.documents.values())

    def _dropbox(self):
        cursor, seen = None, set()
        seen_entries = set()
        while True:
            endpoint = (
                "/2/files/list_folder/continue" if cursor else "/2/files/list_folder"
            )
            body = (
                {"cursor": cursor}
                if cursor
                else {
                    "path": "",
                    "recursive": True,
                    "include_deleted": False,
                    "limit": 100,
                }
            )
            value, _ = self.request(endpoint, body=body)
            for item in rows(value, "entries"):
                tag = item.get(".tag")
                entry_id = identity(item.get("id"))
                if entry_id in seen_entries:
                    raise ValueError("Dropbox repeated an entry")
                seen_entries.add(entry_id)
                if tag == "folder":
                    identity(item.get("id"))
                    continue
                if tag != "file":
                    raise ValueError("Unexpected Dropbox entry type")
                file_id = identity(item.get("id"))
                rev = identity(item.get("rev"))
                path = identity(item.get("path_lower"))
                name = item.get("name")
                if not isinstance(name, str):
                    raise ValueError("Dropbox omitted file name")
                suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                content = None
                if suffix in _TEXT_EXTENSIONS:
                    data, response = self.request(
                        "/2/files/download",
                        post=True,
                        origin=self.origins[1],
                        headers={"Dropbox-API-Arg": json.dumps({"path": "rev:" + rev})},
                        raw=True,
                    )
                    header = response.headers.get("Dropbox-API-Result", "")
                    self._reject_reflection(header)
                    from openjarvis.connectors.web_sources import (
                        _reject_constant,
                        _strict_pairs,
                    )

                    downloaded = json.loads(
                        header,
                        object_pairs_hook=_strict_pairs,
                        parse_constant=_reject_constant,
                    )
                    if downloaded.get("id") != file_id or downloaded.get("rev") != rev:
                        raise ValueError("Dropbox download changed revision")
                    content = data.decode("utf-8", errors="strict")
                self.add(
                    "dropbox:" + file_id,
                    item,
                    title=name,
                    content=content,
                    ts=timestamp(item.get("server_modified")),
                    metadata={
                        "path": path,
                        "revision": rev,
                        "content_kind": "downloaded_text"
                        if content is not None
                        else "metadata_only",
                    },
                )
            if type(value.get("has_more")) is not bool:
                raise ValueError("Dropbox omitted pagination status")
            next_cursor = identity(value.get("cursor"))
            if not value["has_more"]:
                return
            if next_cursor in seen:
                raise ValueError("Dropbox repeated a cursor")
            seen.add(next_cursor)
            cursor = next_cursor

    def _granola_pages(self, endpoint, field, page_size):
        cursor, seen = None, set()
        while True:
            value, _ = self.request(
                endpoint,
                {"page_size": page_size, **({"cursor": cursor} if cursor else {})},
            )
            yield from rows(value, field)
            if type(value.get("hasMore")) is not bool or "cursor" not in value:
                raise ValueError("Granola omitted pagination status")
            if not value["hasMore"]:
                return
            cursor = identity(value["cursor"])
            if cursor in seen:
                raise ValueError("Granola repeated a cursor")
            seen.add(cursor)

    def _granola(self):
        seen = set()
        for stub in self._granola_pages("/v1/notes", "notes", 30):
            note_id = identity(stub.get("id"))
            if note_id in seen:
                raise ValueError("Granola repeated a note")
            seen.add(note_id)
            endpoint = "/v1/notes/" + path_id(note_id)
            item, _ = self.request(endpoint)
            if not isinstance(item, dict) or item.get("id") != note_id:
                raise ValueError("Granola detail does not match its listing")
            transcript = list(
                self._granola_pages(endpoint + "/transcript", "transcript", 100)
            )
            turns, turn_keys = [], set()
            for turn in transcript:
                text = turn.get("text")
                if not isinstance(text, str):
                    raise ValueError("Invalid Granola transcript text")
                key = (
                    turn.get("start_time"),
                    turn.get("end_time"),
                    json.dumps(turn.get("speaker"), sort_keys=True),
                    text,
                )
                timestamp(turn.get("start_time"))
                timestamp(turn.get("end_time"))
                if key in turn_keys:
                    raise ValueError("Granola repeated a transcript turn")
                turn_keys.add(key)
                turns.append(text)
            item["transcript"] = transcript
            summary = item.get("summary_markdown") or item.get("summary_text")
            if not isinstance(summary, str):
                raise ValueError("Granola omitted summary text")
            private = (
                item.get("private_notes_markdown")
                or item.get("private_notes_text")
                or ""
            )
            if not isinstance(private, str):
                raise ValueError("Invalid Granola private notes")
            self.add(
                "granola:" + note_id,
                item,
                title=item.get("title") or "",
                content=summary + "\n\n" + private + "\n\n" + "\n".join(turns),
                ts=timestamp(item.get("updated_at")),
                metadata={"coverage": "accessible_summarized_notes_and_transcripts"},
                url=item.get("web_url"),
            )

    def _oura(self):
        start = (
            self.started - timedelta(days=self.config.get("lookback_days", 30))
        ).date()
        end = self.started.date()
        for kind in ("sleep", "daily_readiness", "daily_activity"):
            for item in self.collection(
                "/v2/usercollection/" + kind,
                "data",
                {"start_date": start.isoformat(), "end_date": end.isoformat()},
                next_key="next_token",
                cursor_param="next_token",
            ):
                day = item.get("day")
                if not isinstance(day, str):
                    raise ValueError("Oura omitted day")
                date = datetime.strptime(day, "%Y-%m-%d").date()
                if not start <= date <= end:
                    raise ValueError("Oura returned an out-of-window record")
                self.add(
                    f"oura-{kind}-{identity(item['id'])}",
                    item,
                    title=f"Oura {kind} — {day}",
                    ts=datetime.combine(date, datetime.min.time(), timezone.utc),
                    doc_type=kind,
                    metadata={
                        "day": day,
                        "coverage": "configured_date_window",
                        "window_start": start.isoformat(),
                        "window_end": end.isoformat(),
                        "timestamp_semantics": "provider_day",
                    },
                )

    def _github_notifications(self):
        page = 1
        before = self.started.strftime("%Y-%m-%dT%H:%M:%SZ")
        while True:
            params = {
                "all": "true",
                "participating": "false",
                "before": before,
                "per_page": 50,
                "page": page,
            }
            items, response = self.request(
                "/notifications", params, headers={"X-GitHub-Api-Version": "2026-03-10"}
            )
            if (
                not isinstance(items, list)
                or not all(isinstance(item, dict) for item in items)
                or len(items) > 50
            ):
                raise ValueError("Invalid GitHub notifications page")
            for item in items:
                subject = item.get("subject")
                if not isinstance(subject, dict):
                    raise ValueError("Invalid GitHub notification subject")
                updated = timestamp(item.get("updated_at"))
                if updated >= timestamp(before):
                    raise ValueError("GitHub notification falls outside the scan")
                self.add(
                    "github-notification-" + identity(item.get("id")),
                    item,
                    title=subject.get("title", ""),
                    ts=updated,
                    doc_type="notification",
                    metadata={"coverage": "provider_available_notifications"},
                    url=subject.get("url"),
                )
            link = response.links.get("next")
            if not link:
                # A full page without a next Link is verified by an extra page.
                if len(items) < 50:
                    return
                page += 1
                continue
            next_url = link.get("url", "")
            parsed = urlsplit(next_url)
            pairs = parse_qsl(parsed.query, strict_parsing=True)
            query = dict(pairs)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "api.github.com"
                or parsed.path != "/notifications"
                or parsed.fragment
                or len(query) != len(pairs)
                or query
                != {
                    **{key: str(val) for key, val in params.items()},
                    "page": str(page + 1),
                }
            ):
                raise ValueError("Unsafe GitHub pagination link")
            page += 1

    def _weather(self):
        params = {"q": self.config["location"], "units": "imperial"}
        current, _ = self.request("/data/2.5/weather", params)
        forecast, _ = self.request("/data/2.5/forecast", {**params, "cnt": 4})
        if (
            not isinstance(current, dict)
            or str(current.get("cod")) != "200"
            or not isinstance(current.get("main"), dict)
        ):
            raise ValueError("Invalid current weather response")
        entries = rows(forecast, "list")
        if (
            str(forecast.get("cod")) != "200"
            or len(entries) != 4
            or forecast.get("cnt") != 4
        ):
            raise ValueError("Incomplete forecast response")
        times = []
        for entry in [current, *entries]:
            if type(entry.get("dt")) is not int or entry["dt"] <= 0:
                raise ValueError("Invalid weather observation time")
            main = entry.get("main")
            if not isinstance(main, dict) or type(main.get("temp")) not in {int, float}:
                raise ValueError("Weather omitted temperature")
            conditions = rows(entry, "weather")
            if not conditions or not all(
                isinstance(condition.get("description"), str)
                for condition in conditions
            ):
                raise ValueError("Weather omitted conditions")
            times.append(datetime.fromtimestamp(entry["dt"], timezone.utc))
        if any(second <= first for first, second in zip(times[1:], times[2:])):
            raise ValueError("Forecast times did not advance")
        location = self.config["location"]
        self.add(
            "weather-current-" + location,
            current,
            title="Current Weather — " + location,
            ts=times[0],
            doc_type="current",
            metadata={"coverage": "current_observation", "location": location},
        )
        self.add(
            "weather-forecast-" + location,
            forecast,
            title="Weather Forecast — " + location,
            ts=times[1],
            doc_type="forecast",
            metadata={
                "coverage": "four_forecast_intervals",
                "location": location,
                "valid_times": [value.isoformat() for value in times[1:]],
            },
        )
