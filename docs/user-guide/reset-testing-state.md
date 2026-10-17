# Reset memory for fresh-state testing

Run `scripts/reset_memory.py` as the Linux user that runs OpenJarvis, using that
installation's Python environment. The reset applies to **all accounts**. It reads
effective configuration, including administrator database overrides and relocated
memory paths such as `/mnt/ai/openjarvis/memory`. Use the same `OPENJARVIS_HOME` and
other configuration environment as the API service. No data changes occur without
`--apply`.

Close other Jarvis CLI/SDK processes and stop submitting requests from consoles
before applying a reset. The script stops the system `openjarvis-api.service`,
verifies it stopped, backs up all selected targets, then clears the selected data.
It restarts only services that were running beforehand. On failure it leaves
services stopped and prints an error; inspect that error before restarting.
`--offline` skips systemd management for installations where **all writers have
already been stopped manually**.

From the server checkout:

```bash
cd ~/.openjarvis/src
git pull --ff-only origin main

# Preview memory reset:
~/.openjarvis/.venv/bin/python scripts/reset_memory.py

# Apply memory reset:
~/.openjarvis/.venv/bin/python scripts/reset_memory.py --apply
```

The default reset clears extracted facts, global/configured/named-persona
`MEMORY.md` files, SQLite retrieval documents and their search index, conversation
sessions/messages, and managed-agent messages, checkpoints, tasks and summary
memory. Agent definitions and bindings remain. SQLite schemas and non-memory
account/project records are retained.

Accounts, login credentials, administrator settings, tool approvals, saved
workflows, model checkpoints, `SOUL.md`, `USER.md`, generated files, connector
knowledge, traces and telemetry remain by default. This resets learned context,
not model weights or the configured system prompt. It supports the local JSONL
fact store and SQLite retrieval/session stores; externally hosted memory systems
must be reset separately.

Afterward, reload **every** browser/desktop/remote console and start a new chat.
An open console can still submit its old message history. Do not clear all browser
storage unless you also intend to remove saved login/client settings. Scheduled
agents and connector syncs can create new facts or reimport knowledge after restart.

## Options

| Option | Additional effect |
|---|---|
| `--keep-conversations` | Keep conversation history and managed-agent memory; clear long-term memory only |
| `--include-profile` | Also clear configured/global/named-persona `USER.md` files |
| `--include-knowledge` | Also clear indexed chunks in the default `knowledge.db`, preserving connector configuration and source files |
| `--include-generated` | Delete artifacts identified as outputs in retained workflow step records; clear generation runs/triggers, Hunyuan job catalog and quarantined review images |
| `--all-files` | With `--include-generated`, also delete uploaded files from the artifact catalog |
| `--worker-output-dir PATH` | With `--include-generated`, also delete originals under an explicitly selected local ComfyUI/Hunyuan `output` or `outputs` directory; repeat for both workers |
| `--backup-dir PATH` | Use a new backup directory, for example on `/mnt/ai` |
| `--restore PATH` | Restore a backup; requires `--apply` to execute |

`--include-generated` preserves uploaded reference artifacts and saved workflow
definitions, instance settings and operation approvals. Outputs whose workflow
history was previously deleted cannot be reliably distinguished from uploads;
use `--all-files` if you want to clear the entire catalog. Old run IDs and artifact
IDs will no longer resolve after deletion. Review/audit history and account content
protection settings remain; quarantined image bytes and digest approvals are cleared.

ComfyUI and Hunyuan retain their own original output files outside the Jarvis
artifact store. Include their directories explicitly to clear those copies too.
This additionally stops/verifies the user `openjarvis-image.service` and system
`hunyuan3d-worker.service`, and restarts workers before the API. Installations with
different worker service names must stop all writers manually and use `--offline`.
The scan refuses symlinks and unrecognized file types, including model weights;
it never removes model directories, GPU locks or recovery markers.

```bash
# Preview memory + generated-file cleanup, preserving uploads:
~/.openjarvis/.venv/bin/python scripts/reset_memory.py --include-generated

# Apply the same reset:
~/.openjarvis/.venv/bin/python scripts/reset_memory.py --include-generated --apply

# Broad testing reset, including uploaded references and both workers' originals:
~/.openjarvis/.venv/bin/python scripts/reset_memory.py \
  --include-profile --include-knowledge --include-generated --all-files \
  --worker-output-dir /mnt/ai/comfyui/ComfyUI/output \
  --worker-output-dir /mnt/ai/hunyuan3d/outputs \
  --apply
```

## Backup and restore

Each applied reset creates a private timestamped backup under
`~/.openjarvis/backups/memory-reset-<UTC timestamp>` unless `--backup-dir` is set.
SQLite's backup API captures committed WAL data. The manifest records each original
path and backup file. All backups are completed before the first reset; reset
failures trigger restoration from those backups. Backups retain deleted content,
so deletion does not reclaim that content's disk space until the backup is removed.
Ensure there is enough space, especially when including worker outputs.

```bash
~/.openjarvis/.venv/bin/python scripts/reset_memory.py \
  --restore ~/.openjarvis/backups/memory-reset-REPLACE_WITH_TIMESTAMP --apply
```

Restore replaces the full contents of each backed-up SQLite database and restores
the selected files. For example, a generated-file reset backs up `auth.db` to
preserve accounts while clearing its review tables; restoring that snapshot also
reverts later account changes in that database. Restore promptly when undoing a
test reset. Keep the manifest with its numbered backup files. It contains private
paths and the backups contain private data. Restore detects worker output backups
and stops the same fixed workers before restoring them.
