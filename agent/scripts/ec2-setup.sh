#!/usr/bin/env bash
# One-box VM-tier test machine: image service + Prometheus + cAdvisor + Locust + controller.
# Paste as EC2 "User data" (runs as root on first boot) on Ubuntu 24.04, or run with sudo.
# Instance: c6i.2xlarge (8 vCPU). The classifier needs up to 4 cores (8 units x 0.5); the rest
# (gateway, resizer, Locust, controller) uses the other 4. Progress: /var/log/drl-setup.log
set -euxo pipefail
exec > >(tee -a /var/log/drl-setup.log) 2>&1

REPO_URL="${REPO_URL:-https://github.com/plaethos99/drl-autoscaler.git}"
BRANCH="${BRANCH:-main}"
IMAGE="${IMAGE:-ghcr.io/plaethos99/image-service:latest}"
DIR=/opt/drl-autoscaler

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io docker-compose-v2 git python3-venv python3-pip sqlite3
systemctl enable --now docker
usermod -aG docker ubuntu || true

rm -rf "$DIR"
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DIR"
chown -R ubuntu:ubuntu "$DIR"

# App stack (VM tier compose file). Gateway and resizer get 1 core each so the classifier is the
# bottleneck the agent controls.
cd "$DIR/apps/image-service"
IMAGE="$IMAGE" docker compose up -d
docker update --cpus 1.0 imgsvc-gateway imgsvc-resizer

# Controller + Locust in one venv (CPU-only PyTorch keeps it ~1 GB smaller).
python3 -m venv /opt/venv
/opt/venv/bin/pip install --upgrade pip
/opt/venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
/opt/venv/bin/pip install -r "$DIR/agent/requirements.txt" -r "$DIR/loadgen/requirements.txt"
chown -R ubuntu:ubuntu /opt/venv

# Download the model once so the first test doesn't wait for it.
sudo -u ubuntu /opt/venv/bin/python - <<'EOF'
import os, sys
sys.path.insert(0, "/opt/drl-autoscaler/agent")
from autoscaler.config import Settings
from autoscaler.modelstore import fetch
s = Settings()
for name in ("metadata.json", "vm_best.zip"):
    print("model file:", fetch(s.model_repo, s.model_release, name, s.model_dir))
EOF

touch /opt/drl-setup-done
echo "setup finished"
