# Handoff: DRL Autoscaler project

*Written 2026-11-23. Repo: https://github.com/plaethos99/drl-autoscaler (public, branch `main`, commit `e1447f8`)*

## 1. Goal
Research paper: a **deep reinforcement learning (DRL) agent** that allocates cloud resources better than
rule-based autoscalers (Kubernetes HPA, round robin / static). "Better" means fewer SLA violations
(p95 latency > 500 ms) at the same or lower cost, especially during spikes and periodic load.
The guide requires **3 tiers** (VM, serverless functions, Kubernetes) plus an **all-combined** setup, on
**3 clouds** (AWS, Azure, GCP).

| Objective | Meaning |
|---|---|
| 1 | Design the DRL agent: state, action, reward, network; compare PPO vs DQN vs DDPG |
| 2 | Simulated cloud for safe training + synthetic workloads |
| 3 | Integrate with real infrastructure (Kubernetes API + Prometheus; also VM and functions) |
| 4 | Benchmark against HPA and round robin; graphs and statistics |

Source brief: `C:\Users\Admin\Downloads\Intelligent_Resource_Allocation_Framework_Comprehensive.docx`.
Its code is illustrative only: it has a hard-coded latency, an ECS stub, and Sections 5.1–5.9 are filler.

## 2. Decisions already made (don't revisit without the user)
- **Training on Kaggle/Colab only. Never run training, tests or heavy Python on the user's PC** (the user explicitly asked for this).
- Models are published as **GitHub Releases** (`model-v*`), not committed as code.
- **Controller VM on AWS** runs the RL agent (systemd, 5 s loop) and a **Gradio UI** (separate process, reached
  through an SSH tunnel and login). It is separate from the app. One central controller manages all clouds.
- **Load generator** on its own VM (Locust). **Tests last 5 minutes**, plus longer runs later for the paper.
- **Sample app:** Smart Image Processing Service (gateway → resizer → classifier → store/Redis).
- "Function app" means Lambda / Azure Functions / Cloud Run functions. The agent controls memory and warm (provisioned) instances.
- K8s: **k3s on EC2** preferred over EKS (cost). Terraform per cloud, `terraform destroy` after each session.
- Hermes / LLM agent: optional, only as a chat helper and an **extra baseline**, never a replacement for RL.
- Scope: **AWS gets full experiments**. Azure and GCP get smaller portability runs (same model, no retraining).

## 3. What exists

| Path | Status |
|---|---|
| `training/drl_autoscaler_training.py` (+ `.ipynb`, `build_notebook.py`) | Simulator (modes `k8s`, `vm`, `function`), workloads (5 train + 3 held-out), PPO/DQN/DDPG, HPA + static baselines, evaluation, figures, Mann-Whitney test, `release/` export. |
| `apps/image-service/` | FastAPI services, Prometheus metrics, Dockerfile, compose (VM tier), `k8s/` manifests + HPA baseline + agent RBAC + PodMonitor, Lambda/Azure/GCP wrappers. **CI green: 8/8 tests.** Image `ghcr.io/plaethos99/image-service:latest` is public. |
| `loadgen/locustfile.py` | 7 scenarios (`paper5min` default, same shapes as the simulator), synthetic images. |
| `.github/workflows/image-service.yml` | Tests, then build and push to GHCR. |

Key simulator facts: 1 step = 5 s, episode = 60 steps (5 min), SLA = p95 500 ms. Latency uses a pooled M/M/c
(Sakasegawa) model with a 10 s client-timeout cap. Actions are deltas {-2,-1,0,+1,+2}; DDPG maps a continuous value to the same set.
Reward = −(1.0·violation + 0.5·cost + 2.0·drop + 0.02·|Δ|). **Profile numbers are placeholders** until Phase 2 calibration.

## 4. In progress: Kaggle training v0.1
- Notebook: https://www.kaggle.com/code/chaithanyach/drl-autoscaler-training (private, Version #1, "Save & Run All").
  Cell 1 unpacks the training script from a compressed string (Kaggle's import needs a file picker the browser pane can't use),
  and cell 2 runs it with `QUICK=0`.
- At 3.4 h, **25 of 27 runs were done**. Left: `function_DDPG_s1`, `function_DDPG_s2` (~21 min each), then evaluation.
  Expected to finish about 1 hour after that check. It runs on Kaggle's servers, and **nothing was cancelled**.
- Timing on Kaggle CPU: PPO ~1.3 min, DQN ~3.7 min, **DDPG ~21 min** per run.
  For the 5-seed paper run, use more time or fewer DDPG steps.
- Smoke-test sanity result (earlier, k8s, 150k steps): PPO beat HPA on all patterns, e.g. periodic 0% vs 22.5%
  SLA violations and paper5min 1.7% vs 13.5%, but used ~1.5 more replicas → tune `W_COST`.

## 5. Next steps (in order)
1. **Publish model-v0.1** (the user approved this): when Version #1 completes, download `outputs.zip` from the
   notebook's Output tab, review `sim_eval_summary.csv`, `stats_rl_vs_hpa.csv` and `figures/`, then:
   `gh release create model-v0.1 release/*_best.zip release/metadata.json outputs.zip --title "Model v0.1 (simulator, uncalibrated)"`.
   Delete the local download afterwards (the user cares about disk space).
2. **Controller** `agent/`: core (model loader from Releases, state builder matching `metadata.json` observation order,
   safety guards: min/max, cooldown, fallback to HPA, shadow mode), adapters (`k8s`, `vm_docker` via
   `docker update --cpus`, `aws_lambda`, `azure_functions`, `gcp_run`), SQLite decision log, and a **Gradio UI**
   (cloud/tier/method/scenario selection, start/stop Locust, live graphs, results/CSV download).
3. **`infra/` Terraform**: AWS first (controller VM with IAM role, loadgen VM, app VM, k3s 3×t3.medium, Lambda + ECR),
   then Azure and GCP. Set budget alarms first.
4. **Phase 2 calibration** on AWS: measure req/s per classifier replica, startup time and cold start. Update `PROFILES`
   in the notebook → retrain as v0.2.
5. Experiments (AWS full matrix: 4 tiers × 3 methods × 3 scenarios × 5 runs), then Azure/GCP portability, then the paper.

## 6. Environment notes
- Windows 11, PowerShell 5.1 (`Set-Content -Encoding utf8` adds a BOM → use Python or the Write tool for files).
- GitHub CLI is logged in as **plaethos99** (scopes: repo, workflow, gist, read:org; it **cannot** write GHCR itself, but CI can).
  The git identity is repo-local, using the GitHub no-reply address.
- Kaggle account `chaithanyach` is signed in inside the Claude desktop **browser pane**. Notebook settings: Internet on, Accelerator None.
- Memory notes: `~/.claude/projects/C--Users-Admin-Downloads-cost-optimization/memory/`.
- Not yet committed: this `HANDOFF.md`.
