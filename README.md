# DRL Autoscaler — Intelligent Resource Allocation with Deep Reinforcement Learning

Research project: a deep-reinforcement-learning agent that allocates cloud resources for a
microservice workload across **VM, serverless functions and Kubernetes** tiers, on **AWS, Azure and GCP**,
benchmarked against Kubernetes HPA and round-robin (static) allocation.

| Folder | What | Objective |
|---|---|---|
| `training/` | Gymnasium simulator + PPO / DQN / DDPG training notebook (runs on Kaggle/Colab) | 1, 2 |
| `apps/image-service/` | The managed workload: Smart Image Processing Service (gateway → resizer → classifier → store) | 3 |
| `loadgen/` | Locust 5-minute load scenarios (same shapes as the simulator) | 4 |
| `agent/` | Controller: RL agent + safety guards + adapters (VM now; functions / K8s next) and the live demo dashboard | 3 |
| `infra/` *(next)* | Terraform for AWS, Azure, GCP | 3 |
| `docs/` | [AWS deployment guide](docs/DEPLOY.md) and [AWS results](docs/aws-results-2026-11-26/README.md) | 3, 4 |

Trained models are published as GitHub Releases (`model-v*`).

**SLA:** p95 latency ≤ 500 ms. **Decision interval:** 5 s. **Test length:** 5 minutes.
