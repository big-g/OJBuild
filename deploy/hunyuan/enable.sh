#!/usr/bin/env bash
# Upgrade the tested worker and connect it to the existing OpenJarvis service.
set -euo pipefail
umask 077
repo="$HOME/.openjarvis/src"
worker_dir=/mnt/ai/hunyuan3d/service
python="$HOME/.local/share/hunyuan3d/Hunyuan3D-2/.venv/bin/python"
[[ $EUID -ne 0 ]] || { echo 'Run as the OpenJarvis service account, not root.'; exit 1; }
[[ -f "$repo/deploy/hunyuan/worker.py" && -f "$worker_dir/worker.py" ]]
[[ "$(systemctl show openjarvis-api.service --property=User --value)" == "$(id -un)" ]] || {
    echo 'OpenJarvis service account differs; configure matching GPU-lock permissions first.'; exit 1;
}
curl --fail --silent --show-error http://127.0.0.1:8090/health |
    "$python" -c 'import json,sys; assert not json.load(sys.stdin)["busy"], "Finish the active job before upgrading"'
"$python" -m py_compile "$repo/deploy/hunyuan/worker.py"
"$python" - <<'PY'
import os, stat
path='/mnt/ai/hunyuan3d/gpu.lock'
fd=os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
try:
    info=os.fstat(fd)
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    os.fchmod(fd, 0o600)
finally:
    os.close(fd)
PY
stamp="$(date +%Y%m%d_%H%M%S)"
sudo systemctl stop hunyuan3d-worker.service
cp "$worker_dir/worker.py" "$worker_dir/worker.py.$stamp.bak"
cp "$repo/deploy/hunyuan/worker.py" "$worker_dir/worker.py"
mkdir -p "$worker_dir/overrides"
cat > "$worker_dir/overrides/hunyuan.conf" <<'UNIT'
[Service]
Environment=HUNYUAN_GPU_LOCK_PATH=/mnt/ai/hunyuan3d/gpu.lock
Environment=HUNYUAN_GPU_VRAM_GIB=15.92
UNIT
cat > "$worker_dir/overrides/openjarvis.conf" <<'UNIT'
[Service]
Environment=OPENJARVIS_HUNYUAN_URL=http://127.0.0.1:8090
Environment=OPENJARVIS_HUNYUAN_INPUTS=/mnt/ai/hunyuan3d/inputs
Environment=OPENJARVIS_GPU_LOCK_PATH=/mnt/ai/hunyuan3d/gpu.lock
UNIT
sudo mkdir -p /etc/systemd/system/hunyuan3d-worker.service.d /etc/systemd/system/openjarvis-api.service.d
sudo install -m 644 "$worker_dir/overrides/hunyuan.conf" /etc/systemd/system/hunyuan3d-worker.service.d/openjarvis.conf
sudo install -m 644 "$worker_dir/overrides/openjarvis.conf" /etc/systemd/system/openjarvis-api.service.d/hunyuan.conf
# Bound Ollama to one concurrent GPU request so KV caches cannot multiply
# beyond the background-model budget.
cat > "$worker_dir/overrides/ollama.conf" <<'UNIT'
[Service]
Environment=OLLAMA_NUM_PARALLEL=1
UNIT
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo install -m 644 "$worker_dir/overrides/ollama.conf" /etc/systemd/system/ollama.service.d/hunyuan.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama.service hunyuan3d-worker.service openjarvis-api.service
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8090/health
echo
echo 'OpenJarvis 3D Generation is enabled. Reload the browser after rebuilding the frontend.'
