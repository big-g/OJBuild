#!/usr/bin/env bash
# Add a fixed, CPU-only background model without pruning the service environment.
set -euo pipefail
service_name=${1:-openjarvis-api.service}
uv_bin=$(command -v uv)
service_dir=$(systemctl show "$service_name" --property=WorkingDirectory --value)
[[ -d "$service_dir" ]] || { echo 'Service working directory not found' >&2; exit 1; }
cd "$service_dir"
service_python=$("$uv_bin" run --no-sync python -c 'import sys; print(sys.executable)')
"$uv_bin" pip install --python "$service_python" 'rembg[cpu]==2.0.67'
model_dir=/mnt/ai/openjarvis/models/background
mkdir -p "$model_dir"
"$service_python" - "$model_dir" <<'PY'
import sys
import pooch
path = pooch.retrieve(
    'https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx',
    known_hash='md5:60024c5c889badc19c04ad937298a77b',
    path=sys.argv[1], fname='u2net.onnx', progressbar=True,
)
print('Verified CPU foreground model:', path)
PY
printf 'Ready. Run install-generation.sh as well to preserve generation dependencies at service restart.\n'
