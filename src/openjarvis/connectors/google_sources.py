"""Strict named Google inventories; no item failures or creation-date skips."""

import base64
import re
from datetime import datetime, timezone

from openjarvis.connectors.account_scans import (
    AccountScan,
    identity,
    path_id,
    rows,
    timestamp,
)
from openjarvis.connectors.gdrive import _EXPORT_MIME_MAP


class GoogleSource(AccountScan):
    def _documents(self, *, since=None, cursor=None):
        getattr(self, "_" + self.connector_id)()
        self.check()
        return list(self.documents.values())

    def _gmail_body(self, message_id, part, depth=0):
        self.check()
        if depth > 20 or not isinstance(part, dict):
            raise ValueError("Invalid Gmail MIME tree")
        parts = part.get("parts", [])
        if not isinstance(parts, list):
            raise ValueError("Invalid Gmail MIME parts")
        chunks = [self._gmail_body(message_id, child, depth + 1) for child in parts]
        if part.get("filename") or part.get("mimeType") not in {
            "text/plain",
            "text/html",
        }:
            return "\n".join(chunks)
        body = part.get("body", {})
        if not isinstance(body, dict):
            raise ValueError("Invalid Gmail message body")
        if body.get("attachmentId"):
            body, _ = self.request(
                f"/gmail/v1/users/me/messages/{path_id(message_id)}/attachments/{path_id(body['attachmentId'])}"
            )
        data = body.get("data", "")
        if not isinstance(data, str) or not re.fullmatch(r"[A-Za-z0-9_=-]*", data):
            raise ValueError("Invalid Gmail body encoding")
        decoded = base64.b64decode(
            data + "=" * (-len(data) % 4), altchars=b"-_", validate=True
        )
        self._reject_reflection(decoded.decode("utf-8", errors="replace"))
        chunks.append(decoded.decode("utf-8", errors="replace"))
        return "\n".join(chunks)

    def _gmail(self):
        for stub in self.collection(
            "/gmail/v1/users/me/messages",
            "messages",
            {"maxResults": 100, "includeSpamTrash": "true"},
            optional=True,
        ):
            message_id = identity(stub["id"])
            item, _ = self.request(
                "/gmail/v1/users/me/messages/" + path_id(message_id), {"format": "full"}
            )
            if (
                not isinstance(item, dict)
                or item.get("id") != message_id
                or not isinstance(item.get("payload"), dict)
            ):
                raise ValueError("Gmail detail does not match its listing")
            date = item.get("internalDate")
            if not isinstance(date, str) or not re.fullmatch(r"[0-9]{1,16}", date):
                raise ValueError("Gmail omitted received time")
            headers = rows(item["payload"], "headers", optional=True)
            header_map = {}
            for header in headers:
                name, value = header.get("name"), header.get("value")
                if not isinstance(name, str) or not isinstance(value, str):
                    raise ValueError("Invalid Gmail header")
                header_map[name.lower()] = value
            doc_id = "gmail:" + message_id
            self.add(
                doc_id,
                item,
                content=self._gmail_body(message_id, item["payload"]),
                title=header_map.get("subject", ""),
                ts=datetime.fromtimestamp(int(date) / 1000, timezone.utc),
                doc_type="email",
                url=f"https://mail.google.com/mail/u/0/#all/{path_id(message_id)}",
            )
            doc = self.documents[doc_id]
            doc.author = header_map.get("from", "")
            doc.participants_raw = [
                header_map[name] for name in ("from", "to", "cc") if name in header_map
            ]
            from openjarvis.connectors.gmail import _normalize_addresses

            doc.participants = [
                address
                for value in doc.participants_raw
                for address in _normalize_addresses(value)
            ]
            doc.thread_id = item.get("threadId")
            doc.metadata.update(message_id=message_id, labels=item.get("labelIds", []))

    def _gdrive(self):
        # Request incompleteSearch explicitly; a filtered/incomplete inventory
        # is an error rather than evidence of a completed scan.
        cursor, seen_cursors, seen = None, set(), set()
        while True:
            params = {
                "pageSize": 100,
                "q": "trashed = false",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "fields": (
                    "nextPageToken,incompleteSearch,"
                    "files(id,name,mimeType,modifiedTime,owners,webViewLink)"
                ),
                **({"pageToken": cursor} if cursor else {}),
            }
            payload, _ = self.request("/drive/v3/files", params)
            if (
                not isinstance(payload, dict)
                or payload.get("incompleteSearch") is not False
            ):
                raise ValueError("Drive inventory is incomplete")
            for item in rows(payload, "files"):
                file_id = identity(item.get("id"))
                if file_id in seen:
                    raise ValueError("Drive repeated a file")
                seen.add(file_id)
                mime = item.get("mimeType")
                if not isinstance(mime, str):
                    raise ValueError("Drive omitted MIME type")
                content = None
                export = _EXPORT_MIME_MAP.get(mime)
                if export:
                    data, _ = self.request(
                        f"/drive/v3/files/{path_id(file_id)}/export",
                        {"mimeType": export},
                        raw=True,
                    )
                    content = data.decode("utf-8", errors="strict")
                self.add(
                    "gdrive:" + file_id,
                    item,
                    title=item.get("name"),
                    content=content,
                    ts=timestamp(item.get("modifiedTime")),
                    metadata={
                        "content_kind": "exported_text" if export else "metadata_only",
                        "mime_type": mime,
                    },
                    url=item.get("webViewLink"),
                )
            next_cursor = payload.get("nextPageToken")
            if next_cursor is None:
                return
            cursor = identity(next_cursor)
            if cursor in seen_cursors:
                raise ValueError("Drive repeated a cursor")
            seen_cursors.add(cursor)

    def _gcalendar(self):
        for calendar in self.collection(
            "/calendar/v3/users/me/calendarList",
            "items",
            {"maxResults": 100},
            optional=True,
        ):
            calendar_id = identity(calendar["id"])
            endpoint = "/calendar/v3/calendars/" + path_id(calendar_id) + "/events"
            # Keep recurring masters rather than expanding infinite series.
            for item in self.collection(
                endpoint,
                "items",
                {"maxResults": 100, "singleEvents": "false", "showDeleted": "false"},
                optional=True,
            ):
                event_id = identity(item["id"])
                self.add(
                    f"gcalendar:{calendar_id}:{event_id}",
                    item,
                    title=item.get("summary", ""),
                    ts=timestamp(item.get("updated"))
                    if item.get("updated")
                    else (
                        None if item.get("status") == "cancelled" else timestamp(None)
                    ),
                    doc_type="event",
                    metadata={
                        "calendar_id": calendar_id,
                        "event_id": event_id,
                        "coverage": "accessible_event_resources_with_recurring_masters",
                    },
                    url=item.get("htmlLink"),
                )

    def _gcontacts(self):
        for item in self.collection(
            "/v1/people/me/connections",
            "connections",
            {
                "pageSize": 100,
                "personFields": (
                    "names,emailAddresses,phoneNumbers,organizations,metadata"
                ),
            },
            key="resourceName",
            optional=True,
        ):
            resource = identity(item["resourceName"])
            names = rows(item, "names", optional=True)
            title = names[0].get("displayName", "") if names else resource
            self.add("gcontacts:" + resource, item, title=title, doc_type="contact")

    def _google_tasks(self):
        for task_list in self.collection(
            "/tasks/v1/users/@me/lists", "items", {"maxResults": 100}, optional=True
        ):
            list_id = identity(task_list["id"])
            for item in self.collection(
                f"/tasks/v1/lists/{path_id(list_id)}/tasks",
                "items",
                {
                    "maxResults": 100,
                    "showCompleted": "true",
                    "showHidden": "true",
                    "showAssigned": "true",
                    "showDeleted": "false",
                },
                optional=True,
            ):
                task_id = identity(item["id"])
                self.add(
                    f"gtasks-{list_id}-{task_id}",
                    item,
                    title=item.get("title", ""),
                    ts=timestamp(item.get("updated")),
                    doc_type="task",
                    metadata={
                        "task_list_id": list_id,
                        "task_list": task_list.get("title", ""),
                        "status": item.get("status"),
                    },
                    url=item.get("webViewLink"),
                )
