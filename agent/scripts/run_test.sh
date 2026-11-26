#!/usr/bin/env bash
# One 5-minute test on the VM-tier box: reset the classifier, run Locust + the controller together,
# then print the summary. Results: ~/results (decisions.sqlite + Locust CSVs).
#
#   ./run_test.sh calibrate            # 1 unit fixed, ramp load -> prints MU_PER_UNIT to export
#   ./run_test.sh rl        [scenario] # model controls the classifier's CPU
#   ./run_test.sh rl-shadow [scenario] # model only logs; the allocation stays fixed
#   ./run_test.sh threshold [scenario] # HPA-like baseline
#   ./run_test.sh static    [scenario] # fixed 4 units (2 cores), the simulator's static baseline
#
# scenario: paper5min (default) | steady | periodic | ramp | spike | double_spike | sawtooth
# Env: MU_PER_UNIT (default 9.5), PEAK_USERS (default from MU), DURATION_S (300), START_UNITS (2),
#      CPUS_PER_UNIT (0.5; ec2-setup.sh sets 0.125 on a 2-vCPU box via /etc/profile.d/drl-autoscaler.sh),
#      LOADGEN_URL (Locust on the load-generator box; install-dashboard.sh sets it in /etc/profile.d)
set -euo pipefail

DIR=/opt/drl-autoscaler
VENV=/opt/venv
POLICY="${1:?usage: run_test.sh calibrate|rl|rl-shadow|threshold|static [scenario]}"
SCENARIO="${2:-paper5min}"
MU="${MU_PER_UNIT:-9.5}"
DURATION="${DURATION_S:-300}"
START_UNITS="${START_UNITS:-2}"
RESULTS="${RESULTS:-$HOME/results}"
mkdir -p "$RESULTS"
CPU_UNIT="${CPUS_PER_UNIT:-0.5}"
export DECISION_DB="$RESULTS/decisions.sqlite" MU_PER_UNIT="$MU" CPUS_PER_UNIT="$CPU_UNIT"

# Load that matches training: simulator peak = 80 % of capacity at max allocation (8 units).
# Each Locust user sends ~1 request per (1 s + latency), so users ≈ rps × 1.25.
PEAK_DEFAULT=$(python3 -c "print(round(0.8 * 8 * $MU * 1.25))")
case "$POLICY" in
  calibrate)  ARGS=(--policy static --static-units 1); START=1; SCENARIO=ramp
              PEAK_DEFAULT=$(python3 -c "print(round(3 * $MU * 1.25))") ;;   # ramp to ~3x one unit's capacity
  rl)         ARGS=(--policy rl);            START="$START_UNITS" ;;
  rl-shadow)  ARGS=(--policy rl --shadow);   START="$START_UNITS" ;;
  threshold)  ARGS=(--policy threshold);     START="$START_UNITS" ;;
  static)     ARGS=(--policy static);        START=4 ;;
  *) echo "unknown policy $POLICY"; exit 1 ;;
esac
PEAK="${PEAK_USERS:-$PEAK_DEFAULT}"
RUN_ID="vm-${POLICY}-${SCENARIO}-$(date +%Y%m%d-%H%M%S)"

echo "== $RUN_ID: start units $START x $CPU_UNIT vCPU, peak users $PEAK, MU_PER_UNIT $MU"
docker update --cpus "$(python3 -c "print($START * $CPU_UNIT)")" imgsvc-classifier >/dev/null
echo "   30 s idle so the previous run's traffic leaves the Prometheus windows..."
sleep 30

cd "$DIR/agent"
"$VENV/bin/python" -m autoscaler run "${ARGS[@]}" --start-units "$START" --duration "$DURATION" \
  --run-id "$RUN_ID" > "$RESULTS/$RUN_ID.controller.log" 2>&1 &
CTRL=$!
sleep 1   # first decision happens with no load (baseline step)

if [ -n "${LOADGEN_URL:-}" ]; then
  # Load from the separate load-generator box (its Locust runs in manual mode; the shape is played from here).
  "$VENV/bin/python" -m dashboard.drive --url "$LOADGEN_URL" --scenario "$SCENARIO" --peak "$PEAK" \
    --duration "$DURATION" > "$RESULTS/$RUN_ID.locust.log" 2>&1 || true
else
  SCENARIO="$SCENARIO" PEAK_USERS="$PEAK" DURATION_S="$DURATION" \
    "$VENV/bin/locust" -f "$DIR/loadgen/locustfile.py" --host http://127.0.0.1:8080 --headless \
    --csv "$RESULTS/$RUN_ID" --csv-full-history --only-summary > "$RESULTS/$RUN_ID.locust.log" 2>&1 || true
fi

wait "$CTRL" || { echo "controller failed, see $RESULTS/$RUN_ID.controller.log"; tail -20 "$RESULTS/$RUN_ID.controller.log"; exit 1; }

echo "== Summary"
"$VENV/bin/python" -m autoscaler report --runs "$RUN_ID"
if [ "$POLICY" = calibrate ]; then
  "$VENV/bin/python" -m autoscaler mu "$RUN_ID"
fi
echo "Locust totals:"; grep -hE "Aggregated|/process|locust totals" "$RESULTS/${RUN_ID}_stats.csv" \
  "$RESULTS/$RUN_ID.locust.log" 2>/dev/null | head -2 || true
