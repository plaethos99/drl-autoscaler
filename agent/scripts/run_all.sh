#!/usr/bin/env bash
# The whole VM-tier test set, unattended, in tmux session "drl" (a closed browser terminal doesn't stop it):
# wait for ec2-setup.sh, calibrate, one rl-shadow run, then REPEATS rounds of rl / threshold / static
# (each run ~6 min), then the summary table. Results: ~/results, log: ~/run_all.log
#
#   curl -fsSL https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/run_all.sh | bash
#   tail -f ~/run_all.log          # progress
#   tmux attach -t drl             # live view (detach: Ctrl+B, then D)
#
# Env: REPEATS (3), SCENARIOS ("paper5min"; space-separated, e.g. "paper5min azure_typical", interleaved per
#      round), POLICIES (vm: "rl threshold static", k8s: "rl hpa static"), SHADOW (1 = include one rl-shadow
#      run), MU_PER_UNIT (skip calibration and use this value), MODEL_RELEASE (default: the controller's).
#      The tier comes from TIER in /etc/profile.d (k8s-setup.sh sets TIER=k8s on the Kubernetes box).
set -uo pipefail
URL=https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/run_all.sh
SELF="$HOME/run_all.sh"

case "${1:-}" in
  "")
    curl -fsSL "$URL" -o "$SELF"
    tmux new -d -s drl "bash $SELF --wait 2>&1 | tee -a $HOME/run_all.log; exec bash"
    echo "started in tmux session 'drl'. Progress: tail -f ~/run_all.log"
    ;;
  --wait)
    echo "== $(date -u +%T) UTC: waiting for /opt/drl-setup-done"
    while [ ! -f /opt/drl-setup-done ]; do sleep 10; done
    # A fresh login picks up the docker group and CPUS_PER_UNIT (/etc/profile.d/drl-autoscaler.sh).
    exec sudo -iu "$(id -un)" REPEATS="${REPEATS:-3}" SCENARIOS="${SCENARIOS:-${SCENARIO:-paper5min}}" \
      SHADOW="${SHADOW:-1}" MU_PER_UNIT="${MU_PER_UNIT:-}" ${MODEL_RELEASE:+MODEL_RELEASE="$MODEL_RELEASE"} \
      ${POLICIES:+POLICIES="$POLICIES"} \
      bash "$SELF" --tests
    ;;
  --tests)
    cd /opt/drl-autoscaler/agent/scripts
    mkdir -p "$HOME/results"
    TIER="${TIER:-vm}"
    if [ "$TIER" = k8s ]; then POLICIES="${POLICIES:-rl hpa static}"; else POLICIES="${POLICIES:-rl threshold static}"; fi
    echo "== $(date -u +%T) UTC: tier $TIER, $(nproc) vCPU, unit ${POD_CPUS:-${CPUS_PER_UNIT:-0.5}} vCPU, policies: $POLICIES"
    if [ -z "${MU_PER_UNIT:-}" ]; then
      unset MU_PER_UNIT
      ./run_test.sh calibrate | tee "$HOME/results/calibrate.out"
      MU=$(grep -o 'MU_PER_UNIT=[0-9.]*' "$HOME/results/calibrate.out" | tail -1 | cut -d= -f2)
      case "$MU" in ""|0|0.0|0.00) echo "== no usable MU_PER_UNIT from calibration, stopping"; exit 1 ;; esac
      export MU_PER_UNIT="$MU"
    fi
    echo "MU_PER_UNIT=$MU_PER_UNIT" | tee "$HOME/results/mu.txt"
    echo "== model ${MODEL_RELEASE:-(controller default)}, scenarios: $SCENARIOS"
    first="${SCENARIOS%% *}"
    if [ "${SHADOW:-1}" = 1 ]; then ./run_test.sh rl-shadow "$first" || echo "== rl-shadow failed"; fi
    for i in $(seq 1 "$REPEATS"); do
      for sc in $SCENARIOS; do
        for p in $POLICIES; do
          echo "== $(date -u +%T) UTC: round $i/$REPEATS, $sc, $p"
          ./run_test.sh "$p" "$sc" || echo "== $p $sc round $i failed"
        done
      done
    done
    cd /opt/drl-autoscaler/agent
    /opt/venv/bin/python -m autoscaler report --db "$HOME/results/decisions.sqlite" \
      --csv "$HOME/results/summary.csv"
    echo "== $(date -u +%T) UTC: ALL DONE"
    ;;
esac
