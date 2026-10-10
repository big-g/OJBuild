#!/usr/bin/env bash
# Add CPU mesh dependencies to the existing service interpreter without rebuilding it.
set -euo pipefail
service_name=${1:-openjarvis-api.service}
uv_bin=$(command -v uv)
service_dir=$(systemctl show "$service_name" --property=WorkingDirectory --value)
if [[ ! -d "$service_dir" ]]; then
  printf 'Cannot find WorkingDirectory for %s\n' "$service_name" >&2
  exit 1
fi
cd "$service_dir"
service_python=$("$uv_bin" run --no-sync python -c 'import sys; print(sys.executable)')
"$service_python" -c 'import openjarvis; print("Service package:", openjarvis.__file__)'
"$uv_bin" pip install --python "$service_python" 'trimesh>=5.1,<6' 'scipy>=1.11' 'networkx>=3' 'Pillow>=10'
"$service_python" -c 'import trimesh, scipy, networkx, PIL; print("Mesh dependencies ready:", trimesh.__version__)'
# Preserve the existing manually installed Rust extension and generation extras.
# Future dependency updates must be explicit; uv run must not prune them at restart.
dropin_dir="/etc/systemd/system/$service_name.d"
sudo mkdir -p "$dropin_dir"
printf '[Service]\nEnvironment=UV_NO_SYNC=1\n' | sudo tee "$dropin_dir/phase3-dependencies.conf" >/dev/null
sudo systemctl daemon-reload
printf 'Installed. Rebuild the frontend, then restart %s. Optional Blender: sudo apt-get install blender\n' "$service_name"
