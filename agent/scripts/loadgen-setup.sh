#!/usr/bin/env bash
# Load-generator box (e.g. a free-tier t3.small next to the app box). EC2 user data, runs as root:
#   #!/bin/bash
#   curl -fsSL https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/loadgen-setup.sh | APP_HOST=<app private ip> bash
# Runs Locust as a service in "manual" mode: the demo dashboard on the app box sets the user count through
# Locust's web API on port 8089 (allow it only from the app box's security group).
set -euxo pipefail
exec > >(tee -a /var/log/drl-loadgen-setup.log) 2>&1
: "${APP_HOST:?set APP_HOST to the private IP of the app box}"
REPO_URL="${REPO_URL:-https://github.com/plaethos99/drl-autoscaler.git}"
DIR=/opt/drl-autoscaler

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3-venv
rm -rf "$DIR"
git clone --depth 1 "$REPO_URL" "$DIR"
python3 -m venv /opt/venv
/opt/venv/bin/pip install --upgrade pip
/opt/venv/bin/pip install -r "$DIR/loadgen/requirements.txt"

cat > /etc/systemd/system/drl-locust.service <<EOF
[Unit]
Description=Locust load generator for the DRL autoscaler demo
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
Environment=SCENARIO=manual
WorkingDirectory=$DIR/loadgen
ExecStart=/opt/venv/bin/locust -f $DIR/loadgen/locustfile.py --host http://$APP_HOST:8080 --web-host 0.0.0.0 --web-port 8089
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now drl-locust
touch /opt/drl-loadgen-done
echo "loadgen ready: Locust web API on :8089, target http://$APP_HOST:8080"
