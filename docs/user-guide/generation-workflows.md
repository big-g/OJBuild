# Generation and saved workflows (Phase 3)

Open **Generation & Workflows** to create, name, edit, save and rerun an ordered
workflow. Each step chooses an approved operation, parameters and either the
initial input or an earlier step's output. Runs execute in the background; their
step results, errors, reports and downloadable files persist across browser
reloads. Editing a definition creates a new revision. Existing runs keep their
original definition and parameters. Save edits before running.

The first acceptance workflow is **GLB inspection, repair and STL**: import a
completed Hunyuan GLB from **3D → Use in workflow**, or upload a GLB/STL; inspect,
repair, set a physical dimension in millimetres, export STL, and reload the STL
for validation. In **3D**, choose **After generation** before submitting to attach
a saved workflow automatically. The attachment is owned by your account and
runs once when that job completes. Cancel a waiting attachment or remove its
history in **3D**; removing a submitted attachment does not cancel its run. A changed definition or revoked approval
makes the attachment fail visibly rather than running changed instructions.

## Operations and limits

| Operation | Behavior |
|---|---|
| Artifact copy | Saves a separate immutable copy |
| Mesh inspect | Dimensions/bounds, vertex/face counts, source instances, connected components, degenerate/duplicate faces, normals, winding, watertightness and volume when valid |
| Mesh simplify | Accepts dense originals and creates a separate PLY copy at the selected face budget (default 300,000); detail and topology may change |
| Remove image background | Local CPU U2Net segmentation; preserves an existing alpha mask by default and saves a transparent PNG for review |
| Mesh repair | Removes duplicate/degenerate faces, merges vertices, removes unused vertices and fixes winding/normals; optional small-hole filling or largest component |
| Mesh scale | Uniformly scales the longest/X/Y/Z dimension to 1–3000 mm, preserving proportions; establishes the workflow's explicit millimetre units |
| Blender remesh | Optional headless voxel remesh; use after scaling, before STL export; voxel size uses current coordinates and may remove detail |
| Mesh export STL | Requires an explicit scale step; rejects non-watertight meshes by default; reloads export and checks dimensions |
| Local image generation | Fixed local ComfyUI SD/SDXL graph, seed, negative prompt, 256–1024 pixel dimensions in multiples of 64, 1–50 sampling steps |
| Hunyuan generation | Uses the existing local Hunyuan worker; turbo/standard model and reproducible seed; receives a PNG from an earlier step or uploaded input |

**Text to image** and **Text to 3D and STL** are reusable templates. Select an
enabled image server before saving. Text to 3D generates a PNG, removes its background, passes the foreground
to Hunyuan, simplifies the GLB, then inspects/repairs/reinspects/scales/exports it. It is image-conditioned shape
generation; texture generation is not added by this workflow.

Original files are preserved. Mesh steps save separate PLY intermediates and
JSON before/after reports; STL is a separate final artifact. PLY intermediates
avoid inventing GLB metre units for Hunyuan's normalized coordinates. Source GLB
scene transforms and instances are applied. External GLB buffers/images are
rejected; upload a self-contained binary GLB. GLB originals may be up to 64 MiB; other files and processed outputs are limited
to 20 MiB. Inspection and simplification accept up to 2 million faces and 1 million
vertices. Other mesh operations accept 400,000 faces and 800,000 vertices. All
mesh inputs are limited to 128 source geometries/instances. Existing
private file quotas apply (100 files / 200 MiB per account), including reports.
Delete old files in **Files** when needed; deleting run history keeps files.

Watertightness is a mesh property, not a guarantee of printability. Check wall
thickness, tolerances, support needs and scale in your slicer. Small-hole repair
cannot reconstruct arbitrary missing geometry; review both reports and geometry
before printing. Blender remeshing changes geometry and may erase detail.

Cancellation stops after the active step. Mesh steps have a bounded process
timeout, image jobs a 10-minute polling limit and Hunyuan jobs a 15-minute limit.
Errors stop successors and preserve completed step artifacts. An API restart
marks interrupted runs failed while preserving their completed results; rerun
explicitly. Waiting automatic attachments survive restarts. One API process and
one worker are supported; do not start another API instance on the same catalog.
The queue holds at most 16 unfinished runs globally; each account retains at most
200 runs, 100 definitions and 100 automatic attachments.

## Preparing foregrounds and dense meshes

Run `bash scripts/install/install-generation.sh` from the source checkout, then
`bash scripts/install/install-background-removal.sh`. The latter adds pinned
`rembg[cpu]==2.0.67` to the existing API interpreter without pruning dependencies
and downloads the official U2Net model once to
`/mnt/ai/openjarvis/models/background/u2net.onnx`, verifying its published checksum.
The service account must be able to read it. An administrator can override the
path with `OPENJARVIS_BACKGROUND_MODEL`; request inputs cannot select model paths.
Requests never download a model and explicitly use CPU inference. Existing useful
alpha masks require only Pillow. Background steps have a 6 GiB address-space,
240 CPU-second and 260-second wall-clock bound. Mesh steps use 4 GiB / 240 CPU
seconds. Preserve the ComfyUI `--disable-async-offload` service override on AMD.

1. Upload an image in **Generation & Workflows** and choose **Prepare foreground
   preview**. Approve `image_remove_background` in administrator controls, save,
   and run. Choose **Load step reports and previews** on the run to view its checkerboard preview.
2. Inspect handles, holes, edges and all intended components. If the mask is bad,
   use a clearer reference or an externally prepared transparent PNG. No automatic
   segmentation method guarantees the desired object.
3. Select that foreground artifact as input to **Prepared image to 3D**, or select
   it in the existing 3D page. The direct 3D page accepts prepared images; it does
   not silently apply background removal.
4. Use **3D → Use in workflow** or upload the original GLB directly. Choose **GLB
   inspection, repair and STL**. Its six steps are inspect → simplify → repair →
   reinspect → scale → export. Set the intended millimetre dimension before saving.
   Simplification defaults to 300,000 faces and leaves the source unchanged.
5. Review reports and inspect the STL in a slicer. For meshes requiring voxel
   remeshing, insert Blender remesh after scale, then another inspection before
   export. Failed watertight export is a useful stop, not a reason to automatically
   bypass the check.

Existing saved definitions are immutable by revision and are not silently changed.
Load the updated template and save a new workflow or explicit revision. Reapprove
changed operations if requested by the implementation/provenance gate. The older
`scripts/prepare_workflow_mesh.py` remains an optional standalone utility.

## Phase 3 real-world acceptance

- Run foreground preparation on a real SDXL reference and a transparent PNG.
  Verify the preview, preserved original, and useful mask (especially mug handles).
- Run two consecutive generations on the RX 9060 XT. Both should complete and
  release GPU reservations; no second job should require manual recovery.
- Import the earlier 1,569,230-face Hunyuan GLB directly. Confirm simplification,
  before/after counts, reinspection and intentional millimetre scaling. Inspect
  both shape and wall thickness in the slicer; watertightness alone is insufficient.
- Rerun the same saved workflow, leave/reopen the page during execution, and check
  persisted progress/artifacts. Verify another account cannot access its outputs.
- In chat, ask for saved workflows, request one by name with its source file ID,
  then ask for its status. Confirm the returned run is the same one in Workflows.
  Use a model capable of tool calling; operation approval still applies.
- Test a missing model and a failed mesh export. Each should stop successors,
  display an actionable error and retain completed outputs. Restore prerequisites
  and submit a new run explicitly.

Automated CPU/API/UI tests do not establish live GPU, Blender, slicer, or remote
console acceptance. Phase 4.1 handles device capture/identity and WAN access.

## Administration

In the workflow page's administrator section, approve only the needed operations.
Registration and configuration do not authorize execution. Approval is tied to
operation implementation/provenance and checked again at every step through
ToolExecutor. Existing capability policies and boundary guards still apply. If
capability enforcement is enabled but no enforcing parent policy is available,
the workflow fails closed. Configuration and approval changes are audited in the
versioned SQLite catalog under private file storage `workflows/jobs.db`.

Image servers are separate database instances. Add a name, an exact loopback
root URL such as `http://127.0.0.1:8188`, and an installed checkpoint filename such
as `sd_xl_base_1.0.safetensors`. Saving or editing disables the instance. **Test &
enable** verifies the checkpoint is reported by the provider before enabling;
it does not download a model or prove GPU inference works. Ordinary users see
instance labels and activation state, not provider paths or configuration.

Workflows execute a fixed approved operation catalog. They do not accept Python,
shell commands, Blender scripts, arbitrary ComfyUI graphs or custom nodes. Add
future operations as versioned adapters with schemas, capability requirements,
tests and explicit approval.

## Deploy on the existing Ubuntu server

Pull the tested main branch, then add mesh packages to the interpreter used by
the service. This preserves the existing environment and manually installed Rust
extension. It does not reinstall PyTorch or alter the tested Hunyuan venv.

```bash
cd ~/.openjarvis/src
git pull --ff-only
bash scripts/install/install-generation.sh
# Optional, only if you want the Blender remesh operation:
sudo apt-get install blender
cd frontend
npm ci
npm run build
sudo systemctl restart openjarvis-api.service
sudo systemctl status openjarvis-api.service --no-pager
```

The installer reads the service WorkingDirectory, uses `uv run --no-sync` to
find its Python and installs the generation packages into that interpreter. It
adds a `phase3-dependencies.conf` service drop-in with `UV_NO_SYNC=1` so later
service restarts do not prune manually installed generation/Rust packages.
Dependency upgrades must now be explicit. For a fresh development environment,
use `uv sync --extra generation` and install any other extras you require.

The API already needs `OPENJARVIS_GPU_LOCK_PATH=/mnt/ai/hunyuan3d/gpu.lock` and
Hunyuan needs the matching `HUNYUAN_GPU_LOCK_PATH`. Keep those existing settings.

## Local image worker on ROCm

Use a separate ComfyUI environment. Its documented Linux AMD installation uses
ROCm 7.2 PyTorch; see the [official manual installation guide](https://docs.comfy.org/installation/manual_install).
Choose a reviewed release from [official releases](https://github.com/Comfy-Org/ComfyUI/releases)
and pass its tag or full commit to the installer (the version is deliberately
explicit so later installs do not silently track a moving branch):

```bash
cd ~/.openjarvis/src
bash scripts/install/install-comfyui-rocm.sh v0.39.0
```

Install a compatible full SD/SDXL checkpoint in
`~/.local/share/openjarvis-comfyui/ComfyUI/models/checkpoints/`; for example the
[official SDXL base checkpoint](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0).
Use the model publisher's download and license instructions. This adapter uses
CheckpointLoaderSimple, so split FLUX/diffusion-only models are not compatible.
Weights are not downloaded automatically.

Create a user service at `~/.config/systemd/user/openjarvis-image.service`:

```ini
[Unit]
Description=OpenJarvis local image worker
After=network.target

[Service]
WorkingDirectory=%h/.local/share/openjarvis-comfyui/ComfyUI
ExecStart=%h/.local/share/openjarvis-comfyui/ComfyUI/.venv/bin/python main.py --listen 127.0.0.1 --port 8188 --lowvram --disable-all-custom-nodes
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
mkdir -p ~/.config/systemd/user
# Save the service above, then:
systemctl --user daemon-reload
systemctl --user enable --now openjarvis-image.service
sudo loginctl enable-linger "$USER"
```

Configure the instance and approve its operation in Jarvis. Image generation
holds the shared GPU lock exclusively and unloads local Ollama models first.
Local chat pauses during image generation; Hunyuan's existing alternate-chat
behavior remains available during 3D work. ComfyUI is for Jarvis's exclusive use:
submitting directly through its UI bypasses GPU coordination. Keep it loopback
only. Jarvis does not expose provider queues publicly.

Jarvis persists `gpu.lock.blocked` before image submission and clears it only
after the job has completed/stopped, the queue is empty and free GPU memory
returns within 1 GiB of its pre-job level and PyTorch's reserved pool is at most
256 MiB. The allowance accommodates driver/kernel workspaces retained after
first use; the separate allocator check includes cached model allocations.
ComfyUI's empty HTTP 200 response to `/free`
acknowledges the request; Jarvis polls to confirm unloading finishes. If the API
crashes, communication fails or cancellation cannot be confirmed, it remains. Further
local image, Hunyuan submission and Ollama requests fail visibly while recovery
runs. The backend `gpu-recovery` maintenance agent checks every 15 seconds. It
acquires the exclusive shared GPU lock, unloads idle Ollama models, checks the
ComfyUI queue and memory, and retries `/free`. Two healthy samples are required
before clearing the reservation. It never interrupts a nonempty healthy queue.

After three failed idle-memory/worker-health checks it restarts only the dedicated
`openjarvis-image.service` user service. The reservation stays in place until
post-restart checks pass. Restarts have a two-minute cooldown and a persistent
three-attempt budget per incident. No automatic reboot, driver reset, broad
process killing, or generated shell commands are used. Failed jobs are not
resubmitted: retry after recovery to avoid duplicate generation or approvals.

The API must run as the same Linux user as the ComfyUI user service (normally
`big-g`), with that user's systemd manager available. The monitor supplies the
usual `/run/user/<uid>` bus environment if absent. Set
`OPENJARVIS_GPU_AUTO_RESTART=0` on the API service to keep diagnostics and idle
cleanup but disable service restarts. Remote/nonstandard worker URLs and old
unstructured markers require manual recovery; automatic service management is
restricted to `http://127.0.0.1:8188`. Exhausted restart budgets and unavailable
user services also require administrator intervention.

API logs include queue counts, pre-job free-memory baseline, current free VRAM,
PyTorch reserved memory, Ollama model count, and recovery/restart outcomes. These
help distinguish delayed unloading from allocator retention or a sick worker.
The agent is stopped with the API and cannot release an actively held GPU lease.

For a legacy marker or unsuccessful automatic recovery, stop the image
worker, verify no image process remains, then remove the marker and restart:

```bash
systemctl --user stop openjarvis-image.service
# Verify the worker stopped before removing the marker:
systemctl --user status openjarvis-image.service --no-pager
rm /mnt/ai/hunyuan3d/gpu.lock.blocked
systemctl --user start openjarvis-image.service
```

Do not remove the shared lock itself. If you also launched ComfyUI manually,
stop that process before clearing the marker.

## Acceptance

Approve inspect, repair, scale and export; run the first template with an uploaded
GLB or completed Hunyuan output, inspect the before/after JSON and open the STL in
a slicer. The automated CPU acceptance check logs in with a disposable session,
uploads a synthetic GLB, runs two saved revisions with different dimensions and
reloads both downloaded STLs. It leaves the private files and runs for review:

```bash
cd ~/.openjarvis/src
UV_NO_SYNC=1 uv run python scripts/check_phase3.py --username YOUR_USERNAME
```

Then verify a real 512×512 image run, text → image → Hunyuan → STL, optional
Blender remesh, automatic post-generation attachment, cancel/reload, a second
account's isolation, and existing chat/3D behavior on the GPU server. The scripted
mesh check does not certify the model workers or the printer.

## Chat and camera-assisted workflow use

The server orchestrator exposes `workflow_list`, `workflow_submit`, and
`workflow_status` to authenticated users when workflow storage is available.
Ask Jarvis to list your saved workflows, run one by name with a prompt or an
existing file, and check its run ID. Submission returns a queued job; it does
not imply generation completed. The same jobs and output files appear in
Workflows and Files. A lost submission response should be checked against run
history before retrying to avoid duplicate generation.

These tools use the authenticated account, never a model-supplied owner.
Operation approvals, saved revision checks, file ownership and capability
policies still apply. Installations enforcing managed-tool approval must also
approve these three built-in tools through their normal governance process.
An administrator must configure and approve generation operations first; chat
cannot enable providers or grant permissions.

The orchestrator now preserves attached images through its tool loop. Choose a
model supporting both vision and tools for camera-assisted requests. Images
remain request-scoped rather than being stored in conversation history. An
attachment is not automatically a workflow input file: upload an image to Files
and supply its artifact ID for an image-to-3D workflow. Other agent types that
do not declare multimodal support retain their existing direct vision route.

## Submission and recovery feedback

A ComfyUI HTTP 400 rejection is reported as a rejected submission. Jarvis clears
the GPU reservation only after confirming the worker is idle and memory has
returned to the allowed baseline. Timeouts, unverified worker health and unknown
submission outcomes remain blocked. This does not repair accelerator faults.

Failed image steps show a failure category and recovery guidance when available.
The run ID shown in Workflows is also included in operation failure logs. For
`gpu_recovery_required`, follow the stop/verify/clear/restart procedure above;
do not remove the reservation while a worker may still be using the GPU.

The workflow editor is disabled while saving. Accepted actions retain their
success state if a later refresh fails. Run history, files, catalog and admin
settings refresh independently, with polling every five seconds during active
jobs and every fifteen seconds otherwise.

MCP setup now saves and discovers tools together. Review the discovered schemas,
select evidence uses where appropriate, save the classification, and explicitly
approve the catalog. Readiness separates approval, automatic chat calls and
evidence classification. Catalog discovery is not a continuous health check;
each invocation still validates the remote service and reviewed catalog.

## Performance and history

3D history shows 25 jobs per page. Completed/failed worker results are retained
locally, so opening history does not repeatedly contact the GPU worker for old
jobs. Transport errors are not cached as terminal results. The API supports
`GET /v1/3d/jobs?limit=25&offset=0` (maximum limit 100). An uncached historical
job still requires one successful worker status retrieval.

Workflow polling requests compact summaries. Use **Load step reports and
previews** for full details; advancing steps invalidate stale detail snapshots.
`GET /v1/workflows/runs` retains the full response for existing clients;
`?compact=true` omits input payloads and step outputs. Individual run endpoints
continue to enforce the same owner checks. 3D/workflow/dashboard polling pauses
in hidden tabs, and slow 3D/dashboard refreshes do not overlap.

Browser file uploads use `POST /v1/files/upload?filename=example.glb` with the
raw file as the body. Uploads are staged privately and copied in bounded chunks;
normal file size limits, account quotas and integrity checks still apply. The
JSON/base64 save endpoint remains available for existing tools and clients.

Dashboard statistics and energy share a three-second server snapshot. Totals may
lag new events by up to three seconds. Aggregation and image checkpoint probing
run off the API event loop. Evidence verification behavior is unchanged;
`evidence_timings_ms` in result metadata records assessment, conflict, grounding,
repair and repair-validation stages when those stages execute. These timings
measure evidence processing, not overall model generation or retrieval latency.

## Generate an image directly from chat

Save a single-step **Text to image** workflow in **Generation & Workflows**,
using an enabled ComfyUI server and an administrator-approved image operation.
Type the image description into the chat composer, choose **Generate image**,
select the saved workflow, then choose **Submit image job**. Progress and output
files appear in **Generation & Workflows**. An accepted job is queued, not yet
completed. Check the run history before retrying if the acknowledgement fails.

This explicit action sends the prompt unchanged to the workflow without invoking
or requiring a chat model. The saved checkpoint, negative prompt, seed and other
workflow settings still apply. It currently accepts text only; remove attached
images to use it. Normal chat messages continue to use the selected chat model,
whose responses and restrictions are not changed by this feature.

Administrators control **Allow direct image submission** for each account in
**Settings → Account management**. When enabled, users can submit images from
the chat composer's Generate image action and run image workflows directly from
the workflow screen. When disabled, both direct HTTP submission endpoints reject
image workflows, while Jarvis's authenticated workflow tools remain available.
This includes image steps nested within a saved multi-step workflow. Existing
operation approvals, capabilities, ownership and enabled-server checks still apply.
Non-image workflows are unaffected. Changes affect new submissions; jobs already
accepted continue to run. Permission changes are audited. The existing API and
stored field names are retained for compatibility, but now control direct access
only. Existing setting values are preserved.

## Birthdate-based content protection

In **Settings → Account management**, administrators can enter a birthdate and
birthday time zone when creating an account or edit them later with **Save
birthdate**. The page shows the calculated age. Only administrators can change
these fields; ordinary registration does not establish adulthood.

Age is recalculated by the server on access. Accounts under 18 and accounts with
no birthdate use the protected policy. At midnight on the 18th birthday in the
account's IANA time zone (for example `America/New_York`), protection lifts
automatically. February 29 birthdays advance on March 1 in non-leap years.
Existing accounts have no birthdate after upgrade and therefore start protected.
Record the reviewing administrator's birthdate as well. Direct submission and
operation permissions remain independent; a birthday does not grant them.

Protected image generation requires two administrator decisions:

1. The workflow waits for **request review** before contacting ComfyUI. An adult
   administrator reviews the exact prompt, negative prompt and generation settings
   under **Image content reviews** on the account management page.
2. After request approval, the workflow generates the image and holds it in
   private review storage. An adult administrator loads the preview and approves
   or rejects delivery. Only an approved image enters the user's file store and
   becomes downloadable. The approved image is reused, not regenerated.

The same checks cover direct image submissions, Jarvis workflow tools, and image
steps in longer workflows. Reviews are tied to the user, run, exact request and
birthdate revision. Changing birthdate or time zone invalidates earlier decisions.
The server also checks stored image access, including artifact reads by tools, so
changing an adult account to a protected account blocks unreviewed older images.
Uploaded images without review are likewise unavailable to protected accounts.

Use **Refresh reviews** to see new requests/results. Workflow status shows
`awaiting_review`; users can cancel while waiting. Approved/rejected decisions
resume the run automatically. Waiting jobs for newly adult users resume on the
worker's next idle check, without restarting the server. A workflow interrupted
while actually generating still follows normal worker recovery rules.

Reviews expire after seven days. Pending review storage is bounded to 32 requests
and 20 MiB per image. Completed, rejected and cancelled runs discard private review
bytes; review decisions and birthdate changes are audited. Birthdates are exposed
only in administrator account management, and are not copied into the audit log.

This is human-reviewed protection for local image generation, not an automated
content classifier or a filter for general chat, external tools or websites.
Administrators must assess suitability for the displayed age. Local 3D generation
and mesh downloads are blocked for protected accounts because 3D content review
is not yet supported. Keep generation workers private to the server; their raw
output directories are outside OpenJarvis's user-facing access controls.
