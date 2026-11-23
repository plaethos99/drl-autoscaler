# Smart Image Processing Service

The workload that the DRL autoscaler manages. A user uploads a photo and gets back a 224×224
thumbnail plus a label ("tabby cat, 0.92"). One codebase runs on every tier: VM, functions,
Kubernetes and the combined setup.

```
client ──POST /process──> gateway ──> resizer ──> classifier (MobileNetV2 ONNX) ──> store ──> Redis
```

| Service | Endpoint | Load | Scaled by the agent? |
|---|---|---|---|
| gateway | `POST /process` (raw image bytes) | light | no (1 replica) |
| resizer | `POST /resize` → JPEG | medium CPU | optional |
| classifier | `POST /classify` → JSON | **heavy CPU** | **yes, main target** |
| store | `POST /results`, `GET /results/{id}`, `GET /stats` | memory | no |

Every service also exposes `GET /health` and `GET /metrics` (Prometheus).
**SLA:** p95 latency of `/process` ≤ 500 ms.

## Metrics the RL agent reads (PromQL)

| State feature | Query |
|---|---|
| p95 latency | `histogram_quantile(0.95, sum(rate(app_request_latency_seconds_bucket{service="gateway"}[30s])) by (le))` |
| request rate | `sum(rate(app_requests_total{service="gateway"}[30s]))` |
| queue length | `sum(app_inflight_requests{service="classifier"})` |
| SLA violations | `sum(rate(app_sla_violations_total{service="gateway"}[30s]))` |
| CPU (K8s) | `sum(rate(container_cpu_usage_seconds_total{namespace="imgsvc",container="classifier"}[30s]))` |
| CPU (VM) | `rate(container_cpu_usage_seconds_total{name="imgsvc-classifier"}[30s])` (cAdvisor) |

## Tiers

**VM** (one EC2 / Azure VM / GCE instance with Docker):
```bash
IMAGE=ghcr.io/plaethos99/image-service:latest docker compose up -d
# agent action = change the classifier's CPU quota (cgroup) without restarting it:
docker update --cpus 2.0 imgsvc-classifier
```

**Kubernetes** (k3s or EKS/AKS/GKE):
```bash
kubectl apply -f k8s/app.yaml -f k8s/agent-rbac.yaml -f k8s/podmonitor.yaml
kubectl -n imgsvc scale deployment classifier --replicas=4   # what the agent does
kubectl apply -f k8s/hpa-baseline.yaml                        # only for the HPA baseline runs
```

**Functions:** the resizer and classifier as functions. The gateway reaches them by setting
`RESIZER_URL` / `CLASSIFIER_URL` (and `*_HEADERS` for auth keys).
- AWS Lambda: `functions/aws_lambda/Dockerfile` (container image in ECR; `ROLE=resizer|classifier`)
- Azure Functions: `functions/azure/` (routes `/api/resize`, `/api/classify`)
- GCP Cloud Run functions: `functions/gcp/` (entry points `resize`, `classify`)
- `functions/build.sh` packages the Azure and GCP folders with the code and the model.

**Combined:** gateway on Kubernetes, classifier as a function, store and Redis on the VM. This is just
a matter of pointing the gateway's `*_URL` variables at each tier.

## Load test (5 minutes)
See `../../loadgen/locustfile.py`. Run it from a separate VM:
```bash
SCENARIO=paper5min PEAK_USERS=120 locust -f locustfile.py --host http://<gateway> --headless --csv results/run1
```

## Image and tests
- `Dockerfile`: one image for all four services (`SERVICE` env var). The model is downloaded at build time.
- Tests (`tests/`) run without the model (synthetic classifier) and without Redis (memory store).
- GitHub Actions (`.github/workflows/image-service.yml`) runs the tests and pushes the image to
  `ghcr.io/<owner>/image-service`. Nothing needs to run on your own computer.
