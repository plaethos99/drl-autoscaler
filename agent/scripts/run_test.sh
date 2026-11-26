#!/usr/bin/env bash
# One 5-minute test: reset the allocation, run the load + the controller together, then print the summary.
# Results: ~/results (decisions.sqlite + logs). TIER=vm (default, docker CPU quota) or TIER=k8s (classifier pods
# on k3s; k8s-setup.sh sets TIER=k8s in /etc/profile.d on that box).
#
#   ./run_test.sh calibrate            # CAL_UNITS (4) fixed, ramp load -> prints MU_PER_UNIT to export
#                                      #   (k8s: also times how long new pods take to become ready)
#   ./run_test.sh rl        [scenario] # model controls the classifier
#   ./run_test.sh rl-shadow [scenario] # model only logs; the allocation stays fixed
#   ./run_test.sh threshold [scenario] # HPA-like baseline (our rule, same 5 s loop as the agent)
#   ./run_test.sh hpa       [scenario] # k8s only: the cluster's own HorizontalPodAutoscaler
#   ./run_test.sh static    [scenario] # fixed 4 units, the simulator's static baseline
#
# scenario: paper5min (default) | steady | periodic | ramp | spike | double_spike | sawtooth | azure_*
# Env: MU_PER_UNIT (default 9.5), PEAK_USERS (default from MU), DURATION_S (300), START_UNITS (2),
#      CPUS_PER_UNIT (vm, 0.5; ec2-setup.sh sets 0.05 on a 2-vCPU box) / POD_CPUS (k8s, 0.1),
#      LOADGEN_URL (Locust on the load-generator box; set in /etc/profile.d by the setup scripts)
set -euo pipefail

DIR=/opt/drl-autoscaler
VENV=/opt/venv
POLICY="${1:?usage: run_test.sh calibrate|rl|rl-shadow|threshold|hpa|static [scenario]}"
SCENARIO="${2:-paper5min}"
TIER="${TIER:-vm}"
MU="${MU_PER_UNIT:-9.5}"
DURATION="${DURATION_S:-300}"
START_UNITS="${START_UNITS:-2}"
MAX_UNITS=8
CAL_UNITS="${CAL_UNITS:-4}"                 # calibration allocation (units)
CAL_PEAK_USERS="${CAL_PEAK_USERS:-40}"      # calibration ramp goes from 10 % to 90 % of this
RESULTS="${RESULTS:-$HOME/results}"
mkdir -p "$RESULTS"
case "$TIER" in
  vm)  CPU_UNIT="${CPUS_PER_UNIT:-0.5}" ;;
  k8s) CPU_UNIT="${POD_CPUS:-0.1}"; export PROM_URL="${PROM_URL:-http://127.0.0.1:30090}" POD_CPUS="$CPU_UNIT" ;;
  *)   echo "unknown TIER $TIER (vm | k8s)"; exit 1 ;;
esac
export DECISION_DB="$RESULTS/decisions.sqlite" MU_PER_UNIT="$MU" CPUS_PER_UNIT="$CPU_UNIT"
NS=imgsvc
HPA_YAML="$DIR/apps/image-service/k8s/testbed/hpa.yaml"

# Set the allocation before a run (k8s: also remove any HPA and wait until all pods are ready).
set_units() {
  if [ "$TIER" = k8s ]; then
    kubectl -n "$NS" delete hpa classifier --ignore-not-found >/dev/null
    kubectl -n "$NS" scale deployment/classifier --replicas="$1" >/dev/null
    kubectl -n "$NS" rollout status deployment/classifier --timeout=300s >/dev/null
  else
    docker update --cpus "$(python3 -c "print($1 * $CPU_UNIT)")" imgsvc-classifier >/dev/null
  fi
}

# Load that matches training: simulator peak = 80 % of capacity at max allocation (8 units).
# Each Locust user sends ~1 request per (1 s + latency), so users ≈ rps × 1.25.
PEAK_DEFAULT=$(python3 -c "print(round(0.8 * $MAX_UNITS * $MU * 1.25))")
case "$POLICY" in
  # Calibrate at CAL_UNITS fixed units and divide by them: with small units a single one can't meet the SLA
  # at any load (one 12 ms inference takes ~250 ms of wall time at 0.05 core), so 1 unit says nothing.
  calibrate)  ARGS=(--policy static --static-units "$CAL_UNITS"); START="$CAL_UNITS"; SCENARIO=ramp
              PEAK_DEFAULT="$CAL_PEAK_USERS" ;;
  rl)         ARGS=(--policy rl);            START="$START_UNITS" ;;
  rl-shadow)  ARGS=(--policy rl --shadow);   START="$START_UNITS" ;;
  threshold)  ARGS=(--policy threshold);     START="$START_UNITS" ;;
  hpa)        [ "$TIER" = k8s ] || { echo "hpa needs TIER=k8s"; exit 1; }
              ARGS=(--policy hpa);           START="$START_UNITS" ;;
  static)     ARGS=(--policy static);        START=4 ;;
  *) echo "unknown policy $POLICY"; exit 1 ;;
esac
PEAK="${PEAK_USERS:-$PEAK_DEFAULT}"
RUN_ID="${TIER}-${POLICY}-${SCENARIO}-$(date +%Y%m%d-%H%M%S)"

if [ "$TIER" = k8s ] && [ "$POLICY" = calibrate ]; then
  # Pod start-up time (scale 1 -> 2, idle node): the simulator's startup delay for the k8s tier.
  set_units 1
  t0=$(date +%s.%N)
  kubectl -n "$NS" scale deployment/classifier --replicas=2 >/dev/null
  kubectl -n "$NS" rollout status deployment/classifier --timeout=300s >/dev/null
  t1=$(date +%s.%N)
  echo "POD_STARTUP_S=$(python3 -c "print(round($t1 - $t0, 1))")" | tee -a "$RESULTS/pod_startup.txt"
fi

echo "== $RUN_ID: start units $START x $CPU_UNIT vCPU, peak users $PEAK, MU_PER_UNIT $MU"
set_units "$START"
if [ "$POLICY" = hpa ]; then
  kubectl apply -f "$HPA_YAML" >/dev/null
  trap 'kubectl -n "$NS" delete hpa classifier --ignore-not-found >/dev/null' EXIT
fi
echo "   30 s idle so the previous run's traffic leaves the Prometheus windows..."
sleep 30

cd "$DIR/agent"
"$VENV/bin/python" -m autoscaler run --tier "$TIER" "${ARGS[@]}" --start-units "$START" --duration "$DURATION" \
  --run-id "$RUN_ID" > "$RESULTS/$RUN_ID.controller.log" 2>&1 &
CTRL=$!
sleep 1   # first decision happens with no load (baseline step)

if [ -n "${LOADGEN_URL:-}" ]; then
  # Load from the separate load-generator box (its Locust runs in manual mode; the shape is played from here,
  # and this box's gateway is sent as the target).
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
  "$VENV/bin/python" -m autoscaler mu "$RUN_ID" --units "$CAL_UNITS"
fi
echo "Locust totals:"; grep -hE "Aggregated|/process|locust totals" "$RESULTS/${RUN_ID}_stats.csv" \
  "$RESULTS/$RUN_ID.locust.log" 2>/dev/null | head -2 || true
