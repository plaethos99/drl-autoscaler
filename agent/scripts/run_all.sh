#!/usr/bin/env bash
# The whole VM-tier test set, unattended, in tmux session "drl" (a closed browser terminal doesn't stop it):
# wait for ec2-setup.sh, calibrate, one rl-shadow run, then REPEATS rounds of rl / threshold / static
# (each run ~6 min), then the summary table. Results: ~/results, log: ~/run_all.log
#
#   curl -fsSL https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/run_all.sh | bash
#   tail -f ~/run_all.log          # progress
#   tmux attach -t drl             # live view (detach: Ctrl+B, then D)
#
# Env: REPEATS (3), SCENARIO (paper5min), SHADOW (1 = include one rl-shadow run),
#      MU_PER_UNIT (skip calibration and use this value)
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
    exec sudo -iu "$(id -un)" REPEATS="${REPEATS:-3}" SCENARIO="${SCENARIO:-paper5min}" SHADOW="${SHADOW:-1}" \
      MU_PER_UNIT="${MU_PER_UNIT:-}" bash "$SELF" --tests
    ;;
  --tests)
    cd /opt/drl-autoscaler/agent/scripts
    mkdir -p "$HOME/results"
    echo "== $(date -u +%T) UTC: $(nproc) vCPU, CPUS_PER_UNIT=${CPUS_PER_UNIT:-0.5}"
    if [ -z "${MU_PER_UNIT:-}" ]; then
      unset MU_PER_UNIT
      ./run_test.sh calibrate | tee "$HOME/results/calibrate.out"
      MU=$(grep -o 'MU_PER_UNIT=[0-9.]*' "$HOME/results/calibrate.out" | tail -1 | cut -d= -f2)
      case "$MU" in ""|0|0.0|0.00) echo "== no usable MU_PER_UNIT from calibration, stopping"; exit 1 ;; esac
      export MU_PER_UNIT="$MU"
    fi
    echo "MU_PER_UNIT=$MU_PER_UNIT" | tee "$HOME/results/mu.txt"
    if [ "${SHADOW:-1}" = 1 ]; then ./run_test.sh rl-shadow "$SCENARIO" || echo "== rl-shadow failed"; fi
    for i in $(seq 1 "$REPEATS"); do
      for p in rl threshold static; do
        echo "== $(date -u +%T) UTC: round $i/$REPEATS, $p"
        ./run_test.sh "$p" "$SCENARIO" || echo "== $p round $i failed"
      done
    done
    cd /opt/drl-autoscaler/agent
    /opt/venv/bin/python -m autoscaler report --db "$HOME/results/decisions.sqlite" \
      --csv "$HOME/results/summary.csv"
    echo "== $(date -u +%T) UTC: ALL DONE"
    ;;
esac
