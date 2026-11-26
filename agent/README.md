# Controller (agent)

Every 5 s the controller reads Prometheus, asks a policy for a target allocation, passes it through
safety guards, applies it to the tier and logs the decision to SQLite.

```
Prometheus ──> state (9 values, same as training) ──> policy ──> guards ──> adapter ──> tier
                                                   rl | threshold | static        docker update --cpus
                                                                     └──> decisions.sqlite
```

| File | What |
|---|---|
| `autoscaler/modelstore.py` | Downloads `<tier>_best.zip` and `metadata.json` from a GitHub Release (`model-v0.1`) and loads them with Stable-Baselines3 |
| `autoscaler/state.py` | Builds the observation in `metadata.json` order with the simulator's scaling (sim-to-real notes in the docstring) |
| `autoscaler/prom.py` | The PromQL queries (gateway rps and p95, classifier in-flight, cAdvisor CPU and memory) |
| `autoscaler/policies.py` | `rl`, `threshold` (HPA-like, 70 % CPU target, 30 s scale-down window) and `static` (4 units) |
| `autoscaler/guards.py` | Min/max, ±2 units per step, optional scale-down cooldown. Bad metric reads hold the current allocation |
| `autoscaler/adapters/vm_docker.py` | VM tier: 1 unit = 0.5 vCPU of the classifier's cgroup quota (1..8 units) |
| `autoscaler/report.py` | Per-run SLA violation %, p95, average units, cost and actions; `mu` estimates capacity per unit |
| `scripts/ec2-setup.sh` | EC2 user data: Docker, the app stack, a venv with the controller and Locust |
| `scripts/run_test.sh` | One 5-minute test: reset the classifier, run Locust and the controller, then print the summary |

Shadow mode (`--shadow`) logs what the model would do without changing anything. Use it before
letting the model act on a new tier.

## First test on AWS (VM tier, one instance)

1. **Budget alarm** (Billing → Budgets), for example $10 per month.
2. **Launch an instance:** Ubuntu 24.04, **c6i.2xlarge** (8 vCPU, about $0.34/h in us-east-1), 20 GB disk.
   Paste `scripts/ec2-setup.sh` into *Advanced details → User data*. For *EC2 Instance Connect*, allow
   SSH (port 22) only from the `com.amazonaws.<region>.ec2-instance-connect` prefix list; no other
   inbound ports are needed. Setup takes about 5–8 minutes.
   `tail -f /var/log/drl-setup.log` shows progress, and `/opt/drl-setup-done` appears at the end.
3. **Run the tests** as `ubuntu` (about 6 minutes each):
   ```bash
   cd /opt/drl-autoscaler/agent/scripts
   ./run_test.sh calibrate                 # prints e.g. export MU_PER_UNIT=11.80
   export MU_PER_UNIT=...                  # use the printed value for every run below
   ./run_test.sh rl-shadow                 # sanity check: model decisions logged, nothing applied
   ./run_test.sh rl                        # model in control
   ./run_test.sh threshold                 # HPA-like baseline
   ./run_test.sh static                    # fixed allocation baseline
   /opt/venv/bin/python -m autoscaler report --db ~/results/decisions.sqlite --csv ~/results/summary.csv
   ```
4. **Terminate the instance** when finished (its results are in `~/results`; copy them off first).

## Live demo dashboard

`dashboard/` is a small web page (Flask, port 8501, login `examiner` + the password in
`~ubuntu/dashboard-login.txt`) that runs on the app box. Pick a method (RL, threshold, static) and a traffic
scenario (spike, ramp, … or *manual* with a users slider), press **Start**, and watch requests/s, p95 latency
against the SLA, and the allocated CPU units update every few seconds. Each run is summarised in the table
below the graphs. Demo runs go to `~/demo/decisions.sqlite`, separate from the paper's `~/results`.

The load comes from Locust on a second small instance (`scripts/loadgen-setup.sh` as its user data, with
`APP_HOST` = the app box's private IP), so the load generator doesn't take CPU from the service being measured.
Then point the dashboard at it: `sudo bash scripts/install-dashboard.sh http://<loadgen-private-ip>:8089`.
Security group: 8501 open for the dashboard (login protected, plain HTTP, so use a throwaway password);
8080 and 8089 only between the two instances.

## Caveat for v0.1

The model was trained on placeholder profiles. `MU_PER_UNIT` scales the request-rate inputs to the
measured capacity, but startup behaviour and the latency curve still come from the simulator. This
test checks that the whole loop works and shows how far the simulator is from reality. The numbers
for the paper come from v0.2, trained after calibration.
