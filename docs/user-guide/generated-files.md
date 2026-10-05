# Generated files and complete output

## Output limits

Settings → Max output tokens controls the budget for each model generation, including
agent chat. The model may impose its own output or context limit. OpenJarvis restores
the shared agent's original settings after each request, including failed requests.

Simple and function-calling orchestrator answers can make up to two continuation
calls after a `length` response. Their token usage includes the follow-up calls;
these calls can add latency or provider cost. Other agent modes preserve the last
provider finish reason but may not continue automatically. A response still ending
with `length` displays an incomplete-output warning in chat, including saved chat
metadata. Ask Jarvis to continue or increase the budget when appropriate. Direct
provider streams report truncation without automatically making follow-up calls.

## Save and inspect files

Ask Jarvis to save a script or ASCII STL using `artifact_save`. This tool saves the
content supplied by the model for the verified current account. It uses the existing
tool capability, approval, confirmation and taint gates; it requires `file:write`.
A blocked tool call must not be presented as a successful file creation. Binary
content uses strict base64 encoding. The model's generation budget also limits how
much content it can supply in a single tool call.

Alternatively, click **Save file** on a completed chat code block and enter a filename.
This stores the displayed code as UTF-8 text through the authenticated file API.
Saving does not run, compile or validate the code. A code block in a truncated answer
may itself be incomplete; the warning is not a file validator.

Open **Files** in the navigation menu to view your saved files. Choose a file to
inspect its name, byte size, creation time and SHA-256 checksum before downloading.

- Scripts, HTML, SVG and other UTF-8 content appear as escaped plain text, limited
  to 64 KiB. No active HTML, Markdown, JavaScript or embedded resources are rendered.
- Other binary formats show up to 512 bytes in hexadecimal.
- ASCII and binary STL files can show a static wireframe with a rotation slider.
  Geometry is bounded to 2,000 facets and ASCII parsing to a 512 KiB prefix. A
  limited preview is explicitly labeled. This is a shape preview, not an STL
  validity, manufacturing or printability certification.

Preview never executes the file. It does **not** certify that the downloaded file is
safe to execute. Downloads use attachment/octet-stream semantics and require a
current authenticated session. They have no public static URL or bearer token in the
URL. Files remain on the server across clients and logins until deleted through the
Files page. Other users, including administrators using the application, cannot list,
preview, download or delete your files. The server operator can still access server
storage through operating-system privileges.

## Server storage

The default directory is `~/.openjarvis/artifacts` for the service account, separate
from the application checkout and static web assets. Metadata lives in `catalog.db`;
blobs reside in hashed account directories under random IDs. Display filenames are
metadata, never filesystem paths. Direct generic file-read/write tools reserve this
root for owner-aware access. Existing privileged shell/code tools are not an
operating-system sandbox and should retain their separate capability restrictions.

Each file is limited to **20 MiB**; each account to **100 files / 200 MiB**. The API
rejects oversized request bodies before JSON decoding. Quotas are enforced atomically
across concurrent saves. Files are immutable; save a replacement as a new file and
delete the old one. Directories use `0700`, files `0600`, and reads verify size/hash
and reject symlinks, non-regular files and hard links.

To relocate storage, set this field in the service account's OpenJarvis TOML config:

```toml
[security]
generated_files_dir = "/mnt/ai/openjarvis-artifacts"
```

Choose a dedicated directory that the service account can create and write. Do not
use the web/static directory. Stop `openjarvis-api.service` before relocating an
existing store, copy the complete directory (catalog plus account directories),
retain ownership/permissions, update configuration, then restart the service. Changing
only the setting starts a separate store; it does not migrate existing files.
Back up the complete store while the service is stopped. A deleted user cannot
access files; account deletion does not currently purge retained artifact blobs.

Hardened storage requires POSIX filesystem primitives, as on the centralized Ubuntu
server. Browser, desktop and Android clients use that server's API. On a server
without these primitives, file endpoints return an authenticated 503 and chat remains
available. No new third-party backend dependencies are required.

## API

All routes require `X-OpenJarvis-Session`; a master API key alone is insufficient.
Ownership comes from that verified session, never a JSON owner field.

| Route | Operation |
|---|---|
| `GET /v1/files` | List the current account's files |
| `POST /v1/files` | Save `{filename, content, encoding: "utf8" or "base64"}` |
| `GET /v1/files/{id}/preview` | Return data-only preview and metadata |
| `GET /v1/files/{id}/download` | Download an authenticated attachment |
| `DELETE /v1/files/{id}` | Delete the current account's file |

File creation from chat requires an authenticated human execution identity. CLI or
background jobs without that identity cannot select an owner themselves.
