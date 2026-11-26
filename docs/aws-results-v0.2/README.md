# AWS VM-tier results, model-v0.2 (2026-11-26)

Model [`model-v0.2`](https://github.com/plaethos99/drl-autoscaler/releases/tag/model-v0.2) (DDPG, VM tier, trained on
the AWS-calibrated simulator with generated patterns and real Azure Functions traffic) vs an HPA-like threshold
policy vs static allocation, on real AWS. Simulator results for the same model: [`../sim-results-v0.2`](../sim-results-v0.2/README.md).

## Setup

Same testbed as the v0.1 runs ([`../aws-results-2026-11-26`](../aws-results-2026-11-26/README.md)):

| | |
|---|---|
| App box | `drl-vm-test`, m7i-flex.large (2 vCPU, 8 GiB), Ubuntu 24.04, ap-southeast-2 |
| Load box | `drl-loadgen`, t3.small, Locust in manual mode driven by `dashboard.drive` |
| Autoscaled tier | `imgsvc-classifier` CPU quota, 1 unit = 0.05 vCPU, 1–8 units (`docker update --cpus`) |
| Fixed shares | gateway 0.75, resizer 0.5, store 0.25 cores; small uploads (`IMAGE_SET=small`) |
| Calibration | `MU_PER_UNIT = 3.50` req/s per unit (measured at 4 units in the v0.1 session) |
| Scenarios | `paper5min` (generated, the paper's 5-minute pattern) and `azure_typical` (a held-out 5-minute window of the Azure Functions 2021 trace, day 11–14, never seen in training); 28 peak users |
| Runs | 5 rounds × 2 scenarios × {rl, threshold, static}, 5 min each, interleaved; start at 2 units (static: 4) |
| Code | `run_all.sh` at commit `4771c29`, 16:26–19:13 UTC |

## Results (mean ± SD over 5 runs)

### `paper5min` (generated pattern)

| Method | SLA violations % | Mean p95 ms | Worst p95 ms | Avg units | Cost | Scaling actions |
|---|---|---|---|---|---|---|
| **RL (DDPG v0.2)** | **9.7 ± 3.2** | **266 ± 19** | **952 ± 251** | 6.38 ± 0.10 | 191.3 ± 3.1 | 26.0 ± 2.9 |
| Threshold (HPA-like) | 22.7 ± 5.7 | 376 ± 15 | 1626 ± 297 | 5.55 ± 0.18 | 166.3 ± 5.5 | 10.4 ± 1.7 |
| Static (4 units) | 41.3 ± 2.2 | 765 ± 28 | 2624 ± 119 | 4.00 | 120.0 | 0 |

### `azure_typical` (real traffic, held out)

| Method | SLA violations % | Mean p95 ms | Worst p95 ms | Avg units | Cost | Scaling actions |
|---|---|---|---|---|---|---|
| **RL (DDPG v0.2)** | **15.3 ± 3.0** | **295 ± 15** | **1025 ± 100** | 6.45 ± 0.04 | 193.6 ± 1.4 | 21.0 ± 3.5 |
| Threshold (HPA-like) | 23.7 ± 3.8 | 494 ± 22 | 2966 ± 220 | 5.53 ± 0.12 | 165.8 ± 3.5 | 14.8 ± 1.5 |
| Static (4 units) | 62.3 ± 3.0 | 839 ± 24 | 2273 ± 298 | 4.00 | 120.0 | 0 |

### Significance (two-sided; Welch t-test / exact Mann-Whitney U, n = 5 vs 5)

| Metric | RL vs threshold, `paper5min` | RL vs threshold, `azure_typical` | RL vs static (both) |
|---|---|---|---|
| SLA violations | p = 0.004 / 0.016 (RL −57 %) | p = 0.005 / 0.024 (RL −35 %) | p < 0.001 / 0.008 (RL −75 %) |
| Mean p95 | p < 0.001 / 0.008 (RL −29 %) | p < 0.001 / 0.008 (RL −40 %) | p < 0.001 / 0.008 (RL −65 %) |
| Worst p95 | p = 0.005 / 0.016 (RL −41 %) | p < 0.001 / 0.008 (RL −65 %) | p < 0.001 / 0.008 (RL −55 to −64 %) |
| Cost (avg units) | p < 0.001 / 0.008 (RL **+15 %**) | p < 0.001 / 0.008 (RL **+17 %**) | p < 0.001 / 0.008 (RL +59 to +61 %) |

0.008 is the smallest p-value an exact Mann-Whitney test can give with 5 vs 5 runs (all RL runs better than all
baseline runs).

## v0.2 vs v0.1 (RL, `paper5min`)

| | v0.1 (3 runs) | v0.2 (5 runs) | Welch / MWU |
|---|---|---|---|
| SLA violations % | 16.7 | 9.7 | p = 0.20 / 0.071 |
| Mean p95 ms | 360 | 266 | p < 0.001 / 0.036 |
| Worst p95 ms | 1307 | 952 | p = 0.28 / 0.34 |
| Cost | 145.0 | 191.3 | p < 0.001 / 0.036 (+32 %) |

The two sets ran in different sessions on the same box; the threshold baseline also moved between them
(14.4 % → 22.7 % SLA violations on `paper5min`), so this comparison is weaker than the within-session ones above.

## Reading

* **v0.2 meets the goal v0.1 missed:** it beats the HPA-like rule significantly on SLA violations, mean p95 and
  worst p95, on the generated scenario and on real traffic from days it never trained on.
* **The price is CPU:** RL allocates 15–17 % more units than the threshold rule. v0.1 was the other way round
  (12 % less CPU, same SLA). v0.2 learned a more cautious policy because the calibrated simulator punishes
  queueing hard; this matches what the simulator predicted (VM: +20 % units, far fewer violations).
* **Real traffic is harder:** every method violates the SLA more on `azure_typical` than on `paper5min`; RL's
  advantage holds (−35 % violations, worst p95 1.0 s vs 3.0 s).
* **Static allocation** at 4 units is the cheapest but violates the SLA 41–62 % of the time.
* RL changes the allocation about twice as often as the threshold rule (21–26 vs 10–15 actions per 5 min).
* Simulator vs AWS: the simulator put HPA-like at 32 % violations and DDPG at 3.6 %; on AWS they are about 23 %
  and 10–15 %. The ranking is the same; the absolute numbers differ.

## Reproduce

```
python docs/aws-results-v0.2/analyze.py docs/aws-results-v0.2/summary.csv
```

`summary.csv` is the per-run table from `python -m autoscaler report` on the instance; `analyze.py` computes
the tables above (pure Python). The step-level data is in `~/results/decisions.sqlite` on `drl-vm-test`
(dashboard → Download data). How to set up and run the test: [`../DEPLOY.md`](../DEPLOY.md).
