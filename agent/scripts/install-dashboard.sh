#!/usr/bin/env bash
# Install (or update) the demo dashboard as a systemd service on the app box. Run with sudo.
#   sudo bash install-dashboard.sh                          # Locust on this box (http://127.0.0.1:8089)
#   sudo bash install-dashboard.sh http://<loadgen-ip>:8089 # Locust on the load-generator box
# Login: user "examiner", password in ~ubuntu/dashboard-login.txt. URL: http://<public-ip>:8501
set -euo pipefail
DIR=/opt/drl-autoscaler
ENV_FILE=/etc/drl-dashboard.env
LOADGEN_URL="${1:-}"

if [ ! -f "$ENV_FILE" ]; then
  PASSWORD=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)
  if [ "$(nproc)" -ge 8 ]; then CPU_UNIT=0.5; else CPU_UNIT=0.125; fi
  cat > "$ENV_FILE" <<EOF
DASH_USER=examiner
DASH_PASSWORD=$PASSWORD
DASH_PORT=8501
LOADGEN_URL=${LOADGEN_URL:-http://127.0.0.1:8089}
CPUS_PER_UNIT=$CPU_UNIT
EOF
  chmod 600 "$ENV_FILE"
  printf 'Dashboard login\n  user: examiner\n  password: %s\n' "$PASSWORD" > /home/ubuntu/dashboard-login.txt
  chown ubuntu:ubuntu /home/ubuntu/dashboard-login.txt
  chmod 600 /home/ubuntu/dashboard-login.txt
elif [ -n "$LOADGEN_URL" ]; then
  sed -i "s#^LOADGEN_URL=.*#LOADGEN_URL=$LOADGEN_URL#" "$ENV_FILE"
fi
# Scripted runs (run_test.sh) use the same load generator when it is a separate box.
if [ -n "$LOADGEN_URL" ] && [ "$LOADGEN_URL" != "http://127.0.0.1:8089" ]; then
  echo "export LOADGEN_URL=$LOADGEN_URL" > /etc/profile.d/drl-loadgen.sh
fi

cat > /etc/systemd/system/drl-dashboard.service <<EOF
[Unit]
Description=DRL autoscaler demo dashboard
After=docker.service network-online.target
Wants=network-online.target

[Service]
User=ubuntu
SupplementaryGroups=docker
EnvironmentFile=$ENV_FILE
WorkingDirectory=$DIR/agent
ExecStart=/opt/venv/bin/python -m dashboard
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable drl-dashboard >/dev/null
systemctl restart drl-dashboard
sleep 2
systemctl --no-pager --lines=3 status drl-dashboard | head -5
echo "dashboard: http://$(curl -fs -m 2 https://checkip.amazonaws.com || echo '<public-ip>'):8501  (login in ~ubuntu/dashboard-login.txt)"
