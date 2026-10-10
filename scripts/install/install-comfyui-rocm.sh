#!/usr/bin/env bash
# Separate ROCm image worker. Never modifies the tested Hunyuan environment.
set -euo pipefail
comfy_dir=${OPENJARVIS_COMFY_DIR:-"$HOME/.local/share/openjarvis-comfyui/ComfyUI"}
comfy_revision=${1:?Usage: bash install-comfyui-rocm.sh RELEASE_TAG_OR_COMMIT}
if [[ ! "$comfy_revision" =~ ^(v[0-9]+\.[0-9]+\.[0-9]+|[a-f0-9]{40})$ ]]; then
  printf 'Use a reviewed release tag or full commit SHA.\n' >&2
  exit 1
fi
if [[ -e "$comfy_dir" ]]; then
  printf 'Directory already exists: %s. Inspect it before updating.\n' "$comfy_dir" >&2
  exit 1
fi
mkdir -p "$(dirname "$comfy_dir")"
git clone https://github.com/Comfy-Org/ComfyUI.git "$comfy_dir"
git -C "$comfy_dir" checkout --detach "$comfy_revision"
uv venv --python 3.12 "$comfy_dir/.venv"
uv pip install --python "$comfy_dir/.venv/bin/python" torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm7.2
# Constrain the second install to the GPU builds already selected.
"$comfy_dir/.venv/bin/python" - <<'PY' > "$comfy_dir/rocm-constraints.txt"
from importlib.metadata import version
for name in ('torch', 'torchvision', 'torchaudio'):
    print(f'{name}=={version(name)}')
PY
uv pip install --python "$comfy_dir/.venv/bin/python" --constraint "$comfy_dir/rocm-constraints.txt" -r "$comfy_dir/requirements.txt" --extra-index-url https://download.pytorch.org/whl/rocm7.2 --index-strategy unsafe-best-match
"$comfy_dir/.venv/bin/python" - <<'PY'
import torch
assert torch.version.hip and torch.cuda.is_available(), 'ROCm GPU unavailable'
print('ROCm:', torch.version.hip, 'GPU:', torch.cuda.get_device_name(0))
a = torch.ones((32, 32), device='cuda'); b = a @ a
assert b[0, 0].item() == 32
PY
printf 'Installed revision: '
git -C "$comfy_dir" rev-parse HEAD
printf 'Install an SD/SDXL checkpoint in %s/models/checkpoints, then follow docs/user-guide/generation-workflows.md.\n' "$comfy_dir"
