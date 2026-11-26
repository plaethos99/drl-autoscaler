#!/usr/bin/env bash
# Kubernetes-tier test machine: single-node k3s with the image service (apps/image-service/k8s/testbed),
# Prometheus and the controller. EC2 user data (runs as root on first boot) on Ubuntu 24.04:
#   #!/bin/bash
#   curl -fsSL https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/k8s-setup.sh | LOADGEN_HOST=<load generator private ip> bash
# Instance: m7i-flex.large (2 vCPU, 8 GiB), the same type as the VM-tier box. Allow port 8080 from the load
# generator (same security group as the VM-tier box). Progress: /var/log/drl-setup.log
set -euxo pipefail
exec > >(tee -a /var/log/drl-setup.log) 2>&1

REPO_URL="${REPO_URL:-https://github.com/plaethos99/drl-autoscaler.git}"
BRANCH="${BRANCH:-main}"
DIR=/opt/drl-autoscaler
POD_CPUS=0.1          # 1 unit = 1 classifier pod; must match apps/image-service/k8s/testbed/app.yaml (100m)

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3-venv python3-pip sqlite3 curl

rm -rf "$DIR"
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DIR"
chown -R ubuntu:ubuntu "$DIR"

# k3s: no Traefik (the gateway is published with ServiceLB), kubeconfig readable by the controller,
# HPA evaluated every 5 s (default 15 s) so the HPA baseline decides as often as the agent.
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --write-kubeconfig-mode 644 --disable traefik \
  --kube-controller-manager-arg=horizontal-pod-autoscaler-sync-period=5s" sh -
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 3; done

kubectl apply -f "$DIR/apps/image-service/k8s/testbed/app.yaml"
kubectl apply -f "$DIR/apps/image-service/k8s/testbed/prometheus.yaml"
for d in redis store resizer classifier gateway; do
  kubectl -n imgsvc rollout status "deployment/$d" --timeout=900s
done
kubectl -n monitoring rollout status deployment/prometheus --timeout=600s
kubectl -n kube-system rollout status deployment/metrics-server --timeout=600s || true

cat > /etc/profile.d/drl-autoscaler.sh <<EOF
export TIER=k8s
export POD_CPUS=$POD_CPUS
export PROM_URL=http://127.0.0.1:30090
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
EOF
if [ -n "${LOADGEN_HOST:-}" ]; then
  echo "export LOADGEN_URL=http://$LOADGEN_HOST:8089" > /etc/profile.d/drl-loadgen.sh
fi

# Controller in a venv (CPU-only PyTorch keeps it ~1 GB smaller).
python3 -m venv /opt/venv
/opt/venv/bin/pip install --upgrade pip
/opt/venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
/opt/venv/bin/pip install -r "$DIR/agent/requirements.txt" flask
chown -R ubuntu:ubuntu /opt/venv

touch /opt/drl-setup-done
echo "k8s setup finished: $(kubectl -n imgsvc get pods --no-headers | wc -l) pods in imgsvc"
