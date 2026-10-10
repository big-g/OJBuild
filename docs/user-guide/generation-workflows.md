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
| Mesh repair | Removes duplicate/degenerate faces, merges vertices, removes unused vertices and fixes winding/normals; optional small-hole filling or largest component |
| Mesh scale | Uniformly scales the longest/X/Y/Z dimension to 1–3000 mm, preserving proportions; establishes the workflow's explicit millimetre units |
| Blender remesh | Optional headless voxel remesh; use after scaling, before STL export; voxel size uses current coordinates and may remove detail |
| Mesh export STL | Requires an explicit scale step; rejects non-watertight meshes by default; reloads export and checks dimensions |
| Local image generation | Fixed local ComfyUI SD/SDXL graph, seed, negative prompt, 256–1024 pixel dimensions in multiples of 64, 1–50 sampling steps |
| Hunyuan generation | Uses the existing local Hunyuan worker; turbo/standard model and reproducible seed; receives a PNG from an earlier step or uploaded input |

**Text to image** and **Text to 3D and STL** are reusable templates. Select an
enabled image server before saving. Text to 3D first generates a PNG, passes it
to Hunyuan and then processes the resulting GLB. It is image-conditioned shape
generation; texture generation is not added by this workflow.

Original files are preserved. Mesh steps save separate PLY intermediates and
JSON before/after reports; STL is a separate final artifact. PLY intermediates
avoid inventing GLB metre units for Hunyuan's normalized coordinates. Source GLB
scene transforms and instances are applied. External GLB buffers/images are
rejected; upload a self-contained binary GLB. Limits are 20 MiB per file,
400,000 faces, 800,000 vertices and 128 source geometries/instances. Existing
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
after the job has completed/stopped and model unloading succeeds. If the API
crashes, communication fails or cancellation cannot be confirmed, it remains. Further
local image, Hunyuan submission and Ollama requests fail visibly. Stop the image
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
