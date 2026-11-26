# model-v0.2: simulation results

Trained on Kaggle (CPU, 2026-11-26) from commit `9f413e0`: PPO, DQN and DDPG × 5 seeds × 3 tiers, 150 000
steps each. Released models: **DDPG** on every tier (vm seed 2, k8s seed 1, function seed 2). GitHub release:
[`model-v0.2`](https://github.com/plaethos99/drl-autoscaler/releases/tag/model-v0.2).

## What changed since v0.1

| | v0.1 | v0.2 |
|---|---|---|
| VM tier simulator | placeholder numbers, "units = parallel servers" | **fitted to AWS** (`training/calibrate_vm.py`): one server whose speed grows with its CPU quota, 3.5–4.1 req/s per 0.05-vCPU unit |
| Observation | request rate of the *next* step (an accidental one-step forecast), instantaneous p95 | request rate and p95 of the **past 15 s**, as the real controller reads them from Prometheus |
| Traffic | 5 generated training patterns, 3 generated held-out | + **real traffic**: Azure Functions Invocation Trace 2021, 401 training windows (days 1–10), 203 held-out windows (days 11–14) |
| Seeds | 3 | 5 |

Real traffic: Azure Public Dataset, *AzureFunctionsInvocationTrace2021*, licence CC-BY 4.0. Y. Zhang,
Í. Goiri, G. I. Chaudhry, R. Fonseca, S. Elnikety, C. Delimitrou, R. Bianchini, "Faster and Cheaper
Serverless Computing on Harvested Resources", SOSP 2021. Windows built by `training/build_trace_windows.py`.

## Summary (all patterns, all seeds, 10 episodes each)

From `sim_eval_summary.csv`:

| Tier | Method | SLA violations % | Mean p95 ms | Avg units | Cost |
|---|---|---|---|---|---|
| VM | **DDPG** | 3.61 | 316 | 5.77 | 173 |
| VM | PPO | 2.20 | 230 | 6.58 | 198 |
| VM | HPA-like | 32.30 | 735 | 4.83 | 145 |
| VM | Static | 41.37 | 2 411 | 4.00 | 120 |
| Kubernetes | **DDPG** | 1.58 | 192 | 8.05 | 503 |
| Kubernetes | HPA-like | 13.77 | 797 | 5.33 | 346 |
| Kubernetes | Static | 25.45 | 1 410 | 4.96 | 300 |
| Functions | **DDPG** | 5.07 | 384 | 7.55 | 289 |
| Functions | HPA-like | 3.43 | 370 | 10.36 | 396 |
| Functions | Static | 4.28 | 379 | 9.92 | 366 |

## Held-out real traffic (`azure_trace_test`, released model)

| Tier | DDPG: SLA % / avg units | HPA-like | Static | DDPG vs HPA (Mann-Whitney, SLA %) |
|---|---|---|---|---|
| VM | **5.5** / 5.44 | 34.0 / 4.34 | 25.3 / 4.00 | p < 0.001 |
| Kubernetes | **3.7** / 8.04 | 11.3 / 5.00 | 19.0 / 4.93 | p < 0.001 |
| Functions | 7.2 / **4.07** | 6.2 / 6.86 | 3.2 / 9.85 | p = 0.83 (no difference), with 40 % less capacity |

## Reading

* **VM and Kubernetes:** the RL policy keeps SLA violations far below the HPA-like rule on every pattern,
  including the held-out generated patterns and real traffic from days it never trained on, at the price of
  more allocated capacity (VM about +20 %, Kubernetes about +50 %).
* **Functions:** RL matches HPA on the SLA (no significant difference on 7 of 10 patterns) while keeping far
  less provisioned capacity; on `paper5min` and `sawtooth` HPA is significantly better.
* **Generalisation:** seen vs held-out SLA violations are close for every RL policy
  (`figures/benchmark_*.png`).
* **Caveat:** the calibrated VM simulator is harsher than AWS at high utilisation (HPA-like: 32 % here vs 14 %
  measured on AWS). Only the VM tier is calibrated; Kubernetes and Functions still use placeholder profiles.
  The AWS runs are the real test.

## Files

`sim_eval_summary.csv` (means per tier and method), `sim_eval_results.csv` (every evaluation episode: tier,
method, training seed, pattern, held-out flag and metrics), `stats_rl_vs_hpa.csv` (per-pattern Mann-Whitney test of
SLA violations, best RL algorithm vs HPA), `train_log.csv` (training time per run), `metadata.json` (what the
controller checks), `figures/` (learning curves, benchmarks, example timelines).
