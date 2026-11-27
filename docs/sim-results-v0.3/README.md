# model-v0.3: simulation results (Kubernetes tier)

Trained on Kaggle (CPU, 2026-11-27, Version #4) from commit `8498cbe`: PPO, DQN and DDPG × 5 seeds, 150 000 steps
each, **Kubernetes tier only**. Released model: **DDPG seed 3**, GitHub release
[`model-v0.3`](https://github.com/plaethos99/drl-autoscaler/releases/tag/model-v0.3). The VM and Functions models stay
at [`model-v0.2`](../sim-results-v0.2/README.md); the controller picks the release per tier.

## What changed since v0.2

The `k8s` profile was a placeholder (10 replicas, 15–22 req/s each). It is now fitted to a single-node k3s cluster on
AWS (m7i-flex.large, the same instance type as the VM tier; `apps/image-service/k8s/testbed`):

| | v0.2 (placeholder) | v0.3 (measured on AWS k3s, 2026-11-27) |
|---|---|---|
| Unit | 1 replica | 1 classifier pod with 0.1 vCPU, request = limit (Guaranteed QoS) |
| Range | 1–10 | 1–8 pods |
| Capacity within the SLA | 15–22 req/s | **3.9 req/s per pod** (calibration ramp at 4 pods); sampled 3.6–4.2 |
| CPU capacity | same as above | 7.2 req/s per pod (13.8 ms CPU per request) |
| Latency model | M/M/c (one queue, `units` servers) | **each pod its own queue**: kube-proxy sends every connection to a random pod. Fitted on 262 steady 5 s steps of 8 runs (`training/calibrate_vm.py --tier k8s --steady`): service rate 2.56 × capacity, overhead 45–75 ms, p95 = overhead + 0.5 × service + 6 × queueing delay; median error 36 % (VM tier: 34 %). The shared-queue model fits much worse (68 %). |
| Pod start-up | 1–4 steps | 2–4 steps (11.7 s from `kubectl scale` to ready on an idle node) |
| HPA baseline | CPU utilisation up to 300 % of the request, read every step | CPU **capped at 100 %** (request = limit) and **averaged over 15 s** (metrics-server resolution), as the cluster's HPA sees it |

Separating the three per-pod rates (SLA capacity, latency service rate, CPU capacity) matters on this tier: a 0.1-vCPU
pod can process ~7 req/s, but p95 crosses 500 ms at ~4 req/s per pod because requests that land on the same pod
slow each other down. On the VM tier the three coincide, so its profile is unchanged.

**Check against AWS (same scenario, before training):** the simulated HPA on `paper5min` violates the SLA in 48 % of
steps at 2.35 pods on average; the real cluster HPA on AWS did 50 % at 2.35 pods. Static 4 pods: 22 % simulated, 27 %
measured.

## Summary (all patterns, all seeds, 10 episodes each)

From `sim_eval_summary.csv`:

| Method | SLA violations % | Mean p95 ms | Avg pods | Cost | Scaling actions |
|---|---|---|---|---|---|
| **DDPG** (released) | 4.92 | 276 | 4.80 | 306 | 10.6 |
| PPO | 3.59 | 263 | 5.23 | 328 | 8.6 |
| DQN | 11.44 | 514 | 3.81 | 267 | 25.3 |
| HPA (cluster-like) | 45.00 | 1 492 | 2.41 | 156 | 6.5 |
| Static (4 pods) | 16.05 | 333 | 3.96 | 240 | 0.8 |

DDPG and PPO are almost tied on reward (−23.88 vs −24.01); DDPG was released as the higher-reward algorithm, as in
v0.1 and v0.2.

## Per pattern: released model vs HPA (Mann-Whitney U on SLA violations, `stats_rl_vs_hpa.csv`)

| Pattern | DDPG SLA % | HPA SLA % | p |
|---|---|---|---|
| steady | 0.0 | 50.2 | < 0.001 |
| periodic | 7.6 | 53.5 | < 0.001 |
| ramp | 0.3 | 55.5 | < 0.001 |
| spike | 6.8 | 37.0 | < 0.001 |
| paper5min | 7.4 | 53.5 | < 0.001 |
| azure_trace (train) | 4.4 | 35.0 | < 0.001 |
| double_spike (held out) | 11.2 | 33.7 | < 0.001 |
| sawtooth (held out) | 3.8 | 54.3 | < 0.001 |
| noisy_trace (held out) | 2.2 | 43.2 | < 0.001 |
| **azure_trace_test** (held-out real traffic) | **5.5** | 34.2 | < 0.001 |

Released model on the two AWS test scenarios (10 episodes): `paper5min` 6.3 % at 5.11 pods (HPA 53.5 % at 2.87,
static 20.8 %); `azure_trace_test` 5.0 % at 4.85 pods (HPA 34.2 % at 1.92, static 10.7 %).

## Reading

* With slow pod start-up and CPU that saturates at the request, a CPU-target HPA reacts late and under-provisions
  (about 2.4 pods on average). The RL policy keeps about twice as many pods, scales up ahead of the load and cuts
  SLA violations by about 90 %, on every pattern including held-out real traffic.
* Against static allocation, RL uses about 20 % more pods for about 70 % fewer violations.
* The AWS runs are the real test: [`../aws-results-k8s-v0.3`](../aws-results-k8s-v0.3/README.md). There RL also
  beats the cluster HPA clearly, but a fixed 6 pods (RL's average) beats RL at the same cost: on the real cluster,
  frequent scaling costs more than this simulator charges for it.

## Files

`sim_eval_summary.csv`, `sim_eval_results.csv` (every episode), `stats_rl_vs_hpa.csv`, `train_log.csv`,
`metadata.json` (what the controller checks), `figures/` (learning curves, benchmark, example timeline).
