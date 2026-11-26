# AWS VM-tier results, 2026-11-26

Model `model-v0.1` (DDPG, `vm_best.zip`) vs an HPA-like threshold policy vs static allocation, on real AWS.

## Setup

| | |
|---|---|
| App box | `drl-vm-test`, m7i-flex.large (2 vCPU, 8 GiB), Ubuntu 24.04, ap-southeast-2 |
| Load box | `drl-loadgen`, t3.small, Locust in manual mode driven by `dashboard.drive` |
| Autoscaled tier | `imgsvc-classifier` CPU quota, 1 unit = 0.05 vCPU, 1–8 units (`docker update --cpus`) |
| Fixed shares | gateway 0.75, resizer 0.5, store 0.25 cores |
| Load | `paper5min` scenario, 28 peak users (≈ 22 req/s), small uploads (`IMAGE_SET=small`) |
| Calibration | `MU_PER_UNIT = 3.50` req/s per unit, measured at 4 units with a ramp |
| Runs | 3 rounds × {rl, threshold, static}, 5 min each, interleaved; start at 2 units (static: 4) |
| Code | commits `a711122` and earlier on `main` |

Before these runs we checked with `docker stats` at ~29 req/s that the classifier (0.34–0.41 of its 0.40 cores)
was the bottleneck, with gateway and resizer near a third of their limits. An earlier set of runs with
640–1280 px uploads and 0.125-vCPU units was discarded because the resizer saturated first (kept on the
instance in `~/results-v1-large-images`).

## Results (mean ± SD over 3 runs)

| Method | SLA violations % | Mean p95 ms | Worst p95 ms | Avg units | Cost | Scaling actions |
|---|---|---|---|---|---|---|
| RL (DDPG v0.1) | 16.7 ± 6.7 | 360 ± 13 | 1307 ± 415 | 4.83 ± 0.02 | 145.0 ± 0.5 | 21.3 ± 3.1 |
| Threshold (HPA-like) | 14.4 ± 6.7 | 354 ± 60 | 1234 ± 115 | 5.51 ± 0.46 | 165.3 ± 13.7 | 12.0 ± 3.0 |
| Static (4 units) | 38.3 ± 3.3 | 682 ± 34 | 2451 ± 61 | 4.00 | 120.0 | 0 |

Welch t-tests (n = 3 per method):

| Metric | RL vs threshold | RL vs static |
|---|---|---|
| SLA violations | p = 0.71 | p = 0.016 |
| Mean p95 | p = 0.87 | p = 0.001 |
| Worst p95 | p = 0.80 | p = 0.039 |
| Cost | p = 0.12 (RL 12 % lower) | p < 0.001 (RL 21 % higher) |

## Reading

* Both autoscalers cut SLA violations from ~38 % (static) to ~15–17 % and halve mean p95; these differences
  are significant.
* RL and the HPA-like threshold reach the same SLA and latency (no significant difference). RL does it with
  about 12 % less CPU and much less run-to-run variation in cost (SD 0.5 vs 13.7), but with n = 3 the cost
  difference is not significant (p = 0.12). More rounds would settle it.
* RL scales more often (21 vs 12 actions per run).
* `summary.csv` is the per-run table from `python -m autoscaler report`; the step-level data is in
  `~/results/decisions.sqlite` on the instance.
