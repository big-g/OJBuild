# Local Hunyuan3D-2 integration

This connects an already tested ROCm Hunyuan installation to OpenJarvis. It adds
an authenticated **3D Generation** page for PNG/JPEG uploads, job status and GLB
downloads. Jobs and downloads belong to the submitting account. One job runs at
a time. This integration generates geometry from an image; textures and a
text-to-image stage are not included.

## Install on the tested server

Expected paths and services:

- OpenJarvis: `~/.openjarvis/src`, `openjarvis-api.service`.
- Hunyuan: `~/.local/share/hunyuan3d/Hunyuan3D-2/.venv`.
- Data: `/mnt/ai/hunyuan3d`, existing `hunyuan3d-worker.service` on localhost:8090.
- Ollama: `ollama.service`, localhost:11434.
- OpenJarvis and the worker run as the same Linux account.

Finish any generation and chat requests before upgrading. As that service
account:

```bash
cd ~/.openjarvis/src
git pull --ff-only origin main
cd frontend
npm run build
cd ..
bash deploy/hunyuan/enable.sh
```

The installer backs up the existing worker, adds three dedicated systemd
configuration files and restarts Ollama, the worker and OpenJarvis. It does not
replace model weights or the ROCm Python environment. The capacity setting is
15.92 GiB for the tested RX 9060 XT; adjust `HUNYUAN_GPU_VRAM_GIB` in the worker's
systemd override for other hardware. Ollama is configured with
`OLLAMA_NUM_PARALLEL=1` to bound concurrent KV caches.

## Default Hunyuan weights

In administrator settings, under **All server parameters**, configure
`server.hunyuan_model` as `turbo` or `standard`. It applies live to new jobs and
sets the initial choice on the generation page. Both variants use the tested
FP16 safetensors; arbitrary weight paths and untested precision changes are not
exposed. A per-job choice overrides the configured default.

## Chat while generating

In **Settings → Administrator server settings → Chat model during local
generation**, configure an installed small Ollama chat model. For the tested
Qwen3.5 family, use `qwen3.5:2b` with Turbo. The setting is persisted and applies
to the next generation job without restarting OpenJarvis. It does not change the
normal server default or the model stored in a conversation. Leave it empty for
automatic selection of the smallest installed, smaller model in the server
default's family. No models are downloaded automatically.

At job submission the worker rejects active OpenJarvis inference, unloads
resident Ollama models, and publishes a temporary alternate-model lease.
During the job, local Ollama chat requests use that alternate with a 2048-token
context, up to 1024 output tokens and thinking disabled. GPU embeddings and
model diagnostics are paused; connector retrieval retains its keyword fallback.
Remote Ollama endpoints and cloud generation retain their normal model routing.
When the generation subprocess exits, the lease is removed after ongoing chat
requests finish. Subsequent chat loads the normal requested model on demand.

The initial memory budget reserves 8.5 GiB for Turbo or 11.5 GiB for Standard,
plus 125% of the alternate's on-disk size and 2 GiB for its cache/runtime. This is
a conservative estimate based on the target server's generation tests, not a
measurement of simultaneous peak allocations. An explicitly configured model
that exceeds the budget rejects submission with an actionable error. Automatic
selection with no suitable candidate runs the mesh job with local chat paused.
Standard may require this fallback on a 16 GiB GPU.

## Verify on the GPU

1. Reload OpenJarvis, configure the alternate and start a **Turbo** image job.
2. Confirm its status says chat continues using the configured model.
3. Send a short chat message during generation. `ollama ps` should show the
   alternate, not the large normal model. Check GPU memory during volume decoding
   as well as diffusion.
4. Download the finished GLB. A non-watertight mesh may require repair before
   printing.
5. Send another chat message after completion. `ollama ps` should show the normal
   model again. It is restored on demand rather than preloaded.

The lock coordinates OpenJarvis and this worker; other direct Ollama clients or
GPU programs do not participate. The simultaneous GPU test must be performed on
the server; unit tests exercise routing, locking, ownership and memory policy
without loading model weights.

Worker logs are in `/mnt/ai/hunyuan3d/jobs/<job_id>/generation.log`.

## Disable and roll back

Wait for the active generation job to finish, then stop the worker. Restore its
specific timestamped `worker.py.*.bak` backup to `worker.py`. Remove only the
integration overrides below, reload systemd and restart the three services:

```bash
sudo systemctl stop hunyuan3d-worker.service
# Restore the backup before restarting the worker.
sudo rm /etc/systemd/system/hunyuan3d-worker.service.d/openjarvis.conf
sudo rm /etc/systemd/system/openjarvis-api.service.d/hunyuan.conf
sudo rm /etc/systemd/system/ollama.service.d/hunyuan.conf
rm -f /mnt/ai/hunyuan3d/gpu.lock.json
sudo systemctl daemon-reload
sudo systemctl restart ollama.service hunyuan3d-worker.service openjarvis-api.service
```

The UI will report that local 3D generation is unconfigured. Existing outputs,
job records and administrator settings are retained.
