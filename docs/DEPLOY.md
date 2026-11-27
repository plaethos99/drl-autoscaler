# Deploying the VM-tier and Kubernetes-tier testbeds on AWS

This guide rebuilds the setup used for the AWS results in
[`aws-results-v0.2`](aws-results-v0.2/README.md) and [`aws-results-2026-11-26`](aws-results-2026-11-26/README.md): the image service with the RL controller and
the demo dashboard on one EC2 instance, and the load generator on a second one. All of it can be done from
the AWS web console. Nothing is installed by hand; each instance sets itself up on first boot from a script
in this repository.

Time: about 20 minutes to deploy, then about 1 hour for the full set of measured runs. The Kubernetes tier adds a
third instance; see [Kubernetes tier](#kubernetes-tier-a-third-instance-with-k3s).

## What gets built

```
 GitHub                                   AWS EC2 (one region, default VPC)
 ──────                                   ─────────────────────────────────
 Release model-v0.1 (vm_best.zip) ──┐     ┌─ drl-vm-test (2 vCPU) ─────────────────────────────┐
 this repo (agent/, apps/, loadgen/)├────►│ image service: gateway, resizer, classifier, store, │
 image ghcr.io/…/image-service ─────┘     │   Redis, Prometheus, cAdvisor (Docker)               │
                                          │ controller + trained model, decision log (SQLite)    │
                                          │ demo dashboard  :8501  (login)                       │
                                          └───────────────▲───────────────────────────────────────┘
                                                          │ HTTP :8080 (simulated users)
                                          ┌─ drl-loadgen (t3.small) ──────────┐
                                          │ Locust :8089 (driven by the dashboard / run_test.sh)
                                          └───────────────────────────────────┘
```

Every 5 seconds the controller reads requests/s and p95 latency from Prometheus and the classifier's CPU
use from cgroup v2, builds the same 9-value state the model was trained on, asks the policy (RL model,
HPA-like threshold, or static) for a new allocation, applies safety limits, and sets the classifier's CPU
quota with `docker update --cpus`. Every decision is logged to SQLite.

## Before you start

* An AWS account. The AWS Free Plan is enough: the two instance types below are free-tier eligible and run on
  the plan's credits. A brand-new account can take up to a day to finish verification; until then some
  services (for example CloudShell) are blocked.
* Pick one region and stay in it. These instructions were tested in **Asia Pacific (Sydney)**,
  `ap-southeast-2`.
* Optional but recommended: **Billing → Budgets → Create budget**, a monthly cost budget of $10 with an email
  alert.

| Instance | Type | Disk | On-demand price (Sydney) |
|---|---|---|---|
| drl-vm-test | m7i-flex.large (2 vCPU, 8 GiB) | 20 GiB gp3 | ~$0.12/h |
| drl-loadgen | t3.small (2 vCPU, 2 GiB) | 8 GiB gp3 | ~$0.03/h |

Stopped instances cost only their disks (a few cents a day).

## Step 1: Launch the app instance (drl-vm-test)

EC2 console → **Instances → Launch instances**.

| Field | Value |
|---|---|
| Name | `drl-vm-test` |
| AMI | Quick Start → Ubuntu → **Ubuntu Server 24.04 LTS** (64-bit x86). Not 26.04: the setup script was written for 24.04 |
| Instance type | **m7i-flex.large** |
| Key pair | **Proceed without a key pair** (you connect with EC2 Instance Connect in the browser) |
| Network settings → Firewall | **Create security group**. Keep the SSH rule, but change *Source type* to **Custom** and pick the prefix list **`com.amazonaws.<region>.ec2-instance-connect`** (IPv4) instead of 0.0.0.0/0 |
| Configure storage | **20** GiB gp3 |
| Advanced details → User data | the two lines below |

```bash
#!/bin/bash
curl -fsSL https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/ec2-setup.sh | bash
```

Click **Launch instance**. The console names the new security group `launch-wizard-N`; step 2 uses it.

What [`ec2-setup.sh`](../agent/scripts/ec2-setup.sh) does on first boot (3–5 minutes, log in
`/var/log/drl-setup.log`):

1. Installs Docker, git and Python, and clones this repository to `/opt/drl-autoscaler`.
2. Starts the image service from [`apps/image-service/docker-compose.yml`](../apps/image-service/docker-compose.yml)
   with the image that GitHub Actions publishes to `ghcr.io/plaethos99/image-service`.
3. Sizes the testbed. On a box with fewer than 8 vCPU: 1 unit = 0.05 vCPU for the classifier (1–8 units),
   gateway 0.75 core, resizer 0.5 core. These values make the classifier, the tier the controller scales, the
   bottleneck (see *Why these sizes* below).
4. Creates `/opt/venv` with CPU-only PyTorch, the controller and Locust.
5. Downloads `metadata.json` and `vm_best.zip` from the GitHub Release `model-v0.1`.
6. Installs the dashboard as the systemd service `drl-dashboard` on port 8501 and writes a random login to
   `/home/ubuntu/dashboard-login.txt`.
7. Writes `/opt/drl-setup-done` and logs `setup finished (2 vCPU, CPUS_PER_UNIT=0.05)`.

Note the instance's **Private IPv4 address** on its details page (for example `172.31.36.228`). The load
generator needs it.

## Step 2: Open the firewall between the two machines and for the dashboard

EC2 → **Security Groups** → the group created in step 1 → **Edit inbound rules → Add rule**, twice:

| Type | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | prefix list `…ec2-instance-connect` (already there) | browser terminal |
| Custom TCP | **8501** | Anywhere-IPv4 (0.0.0.0/0) | the dashboard; it asks for a login |
| **All TCP** | 0–65535 | **this same security group** (`sg-…`) | app ↔ load generator (8080, 8089) |

**Save rules.** Prometheus (9090) listens only on the instance itself and is never exposed.

The dashboard login travels over plain HTTP, so use the generated throwaway password, not one you use
anywhere else. If the examiner will view from a known network, you can restrict 8501 to that address
instead of 0.0.0.0/0.

## Step 3: Launch the load generator (drl-loadgen)

**Launch instances** again:

| Field | Value |
|---|---|
| Name | `drl-loadgen` |
| AMI | Ubuntu Server 24.04 LTS |
| Instance type | **t3.small** |
| Key pair | Proceed without a key pair |
| Firewall | **Select existing security group** → the one from step 1 |
| Storage | default 8 GiB |
| User data | below, with **your app instance's private IP** |

```bash
#!/bin/bash
curl -fsSL https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/loadgen-setup.sh | APP_HOST=172.31.36.228 bash
```

[`loadgen-setup.sh`](../agent/scripts/loadgen-setup.sh) installs Locust and runs it as the service
`drl-locust` in *manual* mode on port 8089: it waits for the dashboard or the test script to set the
number of simulated users. Each user uploads a small synthetic photo to `/process`, waits about 1 s and
repeats.

Note this instance's **private IP** too (for example `172.31.4.183`).

## Step 4: Point the app instance at the load generator

On **drl-vm-test**: **Connect → EC2 Instance Connect → username `ubuntu` → Connect**. In the terminal:

```bash
tail -n 2 /var/log/drl-setup.log      # should end with "setup finished (2 vCPU, CPUS_PER_UNIT=0.05)"
sudo bash /opt/drl-autoscaler/agent/scripts/install-dashboard.sh http://172.31.4.183:8089
```

The second command (with **your loadgen private IP**) stores the load generator's address for both the
dashboard and the scripted runs, and restarts the dashboard.

Check that everything answers:

```bash
systemctl is-active drl-dashboard                                  # active
sudo docker ps --format '{{.Names}} {{.Status}}'                   # 7 containers, all Up
curl -s 172.31.4.183:8089/stats/requests | grep '"state"'          # "ready"
```

## Step 5: Run the measured experiments

```bash
curl -fsSL https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/run_all.sh | SHADOW=0 bash
tail -f ~/run_all.log            # progress; Ctrl+C stops watching, not the run
```

[`run_all.sh`](../agent/scripts/run_all.sh) runs inside the tmux session `drl`, so closing the browser
terminal does not stop it. It:

1. **Calibrates** capacity: holds the classifier at 4 units, ramps the load, and records the highest
   requests/s per unit that kept p95 ≤ 500 ms (`MU_PER_UNIT`, 3.5 on m7i-flex.large). The controller uses it
   to scale its inputs the way the simulator did.
2. Runs **3 rounds** of `rl`, `threshold` and `static`, 5 minutes each on the `paper5min` scenario, with the
   peak load set to 80 % of the capacity at 8 units, as in training. Each run starts with a 30 s idle period.
3. Prints and saves the per-run summary to `~/results/summary.csv`. Step-level data stays in
   `~/results/decisions.sqlite`.

Total time is about an hour. Options: `REPEATS=5` for more rounds, `SHADOW=1` to add one run where the model
only advises (nothing is applied), `SCENARIO=spike` for another traffic shape.

To copy the results off the instance, print them in the terminal and copy the text, or use the runs table
in the dashboard:

```bash
cat ~/results/summary.csv
```

Single runs can also be started by hand, for example `cd /opt/drl-autoscaler/agent/scripts && ./run_test.sh rl spike`.

Do not use the dashboard while the measured runs are going: both drive the same load generator and app.

## Step 6: Demo with the dashboard

Open `http://<drl-vm-test public IPv4>:8501` in any browser. The dashboard shows its sign-in page: user
**examiner**, password from:

```bash
cat ~/dashboard-login.txt
```

The session lasts 12 hours (or until the dashboard restarts); **Sign out** is at the top right. Scripts can
call the dashboard's API with the same credentials as HTTP basic auth (`curl -u examiner:<password> …`).

In the dashboard:

1. Choose a **method**: RL (DDPG), Threshold (HPA-like) or Static.
2. Choose a **traffic scenario**. `spike` is the clearest demo: quiet, a 6× burst from 2:00 to 2:40, then
   quiet. `manual` gives you a users slider to change the load live.
3. **▶ Start.** Requests/s, p95 latency against the red SLA line, and the allocated CPU units next to the CPU
   actually used update every few seconds.
4. **■ Stop** ends the run early. Each run's SLA violations, mean p95, average units, cost and number of
   scaling actions appear in the table at the bottom, so you can run RL and then Threshold on the same
   scenario and compare.

Demo runs are stored in `~/demo/decisions.sqlite`, separate from the measured results.

## Step 7: Stop, start and clean up

* **Stop** (keeps everything, only the disks are billed): Instances → select both → **Instance state → Stop
  instance**.
* **Start again**: select both → **Instance state → Start instance**. Wait about 2 minutes; every service
  starts by itself. The **public IP changes** on every start (take the new one from the instance details);
  the private IPs stay the same, so the dashboard still finds the load generator.
* **Delete** when the project is over: select both → **Terminate (delete) instance**, then delete the
  security group and the budget. This removes the disks and every result stored on them, so copy the
  results first.

## Why these sizes (testbed validity)

The comparison is only fair if the component the controller scales is the bottleneck. On the first attempt
it was not: with 640–1280 px uploads and 0.125-vCPU units, the resizer saturated at ~35 req/s while the
classifier used a third of its quota, so no policy could bring latency down and all methods looked alike.
The fixes, all in the scripts above:

* Locust sends phone-sized uploads by default (`IMAGE_SET=small`), which makes the resizer cheap.
* The classifier unit is 0.05 vCPU (8 units = 0.4 core); gateway and resizer get 0.75 and 0.5 core.
  At ~29 req/s the classifier ran at 85–100 % of its quota while the gateway and resizer stayed near a
  third of theirs.
* Calibration runs at 4 units: at 0.05 core, a single unit cannot meet the 500 ms SLA at any load.
* The load comes from a separate instance, so Locust does not take CPU from the app.

To check the bottleneck yourself while load is running:

```bash
sudo docker stats --no-stream --format '{{.Name}} {{.CPUPerc}}'    # 100 % = one full core
```

On an instance with 8 or more vCPU, `ec2-setup.sh` uses the simulator's 0.5-vCPU units instead. Repeat this
check before measuring there.

## Kubernetes tier: a third instance with k3s

The Kubernetes results ([`aws-results-k8s-v0.3`](aws-results-k8s-v0.3/README.md)) use a separate box so the two
tiers never share CPU. It reuses the same load generator: every run sends its own gateway address to Locust.

1. **Launch** `drl-k8s-test` exactly as in step 1 (Ubuntu 24.04, **m7i-flex.large**, no key pair, 20 GiB gp3),
   but choose **Select existing security group → `launch-wizard-1`** (the group of the other two boxes, so the
   *All TCP from this security group* rule covers 8080 and 8089) and use this user data, with the load generator's
   private IP:

   ```
   #!/bin/bash
   curl -fsSL https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/k8s-setup.sh | LOADGEN_HOST=<drl-loadgen private ip> bash
   ```

   [`k8s-setup.sh`](../agent/scripts/k8s-setup.sh) installs k3s (Traefik off, HPA every 5 s), applies
   [`apps/image-service/k8s/testbed`](../apps/image-service/k8s/testbed) (the app with the classifier as 1–8 pods of
   0.1 vCPU, Prometheus on NodePort 30090) and the controller. About 10–15 minutes; it ends with
   `k8s setup finished: 6 pods in imgsvc` in `/var/log/drl-setup.log`.
2. **Update the load generator** once (its Locust must accept the target per run):
   `sudo git -C /opt/drl-autoscaler pull && sudo systemctl restart drl-locust` on drl-loadgen.
3. **Calibrate** on drl-k8s-test (5 minutes; also times pod start-up):

   ```
   cd /opt/drl-autoscaler/agent/scripts && CAL_PEAK_USERS=55 ./run_test.sh calibrate
   ```

   It printed `MU_PER_UNIT=3.90` and `POD_STARTUP_S=11.7` on 2026-11-27.
4. **Measured runs** (RL, the cluster's own HPA, static; about 3 hours, in tmux):

   ```
   curl -fsSL https://raw.githubusercontent.com/plaethos99/drl-autoscaler/main/agent/scripts/run_all.sh | MU_PER_UNIT=3.9 REPEATS=5 SHADOW=0 SCENARIOS='paper5min azure_typical' bash
   ```

   The box's `/etc/profile.d` sets `TIER=k8s`, so the policies default to `rl hpa static`; `hpa` runs apply
   [`hpa.yaml`](../apps/image-service/k8s/testbed/hpa.yaml) for the run and delete it afterwards. RL uses
   `model-v0.3` (the controller's default for this tier).

If an earlier `run_all.sh` left its tmux session open, it prints `duplicate session: drl` and starts nothing:
`tmux kill-session -t drl` first (only when no run is in progress).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| EC2 Instance Connect says "Failed to connect" | The security group has no SSH rule from the `ec2-instance-connect` prefix list (step 1 or 2). |
| The browser terminal ignores pasted text | Click inside the black area first and paste with Ctrl+Shift+V or right-click → Paste; or type the command. |
| `setup finished` never appears | Read `sudo tail -50 /var/log/drl-setup.log`. A 26.04 AMI or a disk smaller than 20 GiB are the usual causes. |
| `permission denied … docker.sock` in your terminal | Your login started before setup added you to the `docker` group: use `sudo docker …`, or reconnect. |
| Controller log: "no CPU series for the target container" | cAdvisor cannot label containers on this Docker version. The controller reads cgroup v2 directly; make sure the repo is up to date (`git -C /opt/drl-autoscaler pull`). |
| Calibration prints "no usable MU_PER_UNIT" | No step stayed within the SLA at the calibration allocation. Raise `CAL_UNITS` (default 4) or lower `CAL_PEAK_USERS` (default 40). |
| Signed out after an update | The dashboard picks a new session key at every restart; sign in again (set `DASH_SECRET` in `/etc/drl-dashboard.env` to keep sessions). |
| Dashboard shows "Load generator not reachable" | drl-loadgen is stopped, its private IP changed (re-run step 4), or the *All TCP from this security group* rule is missing. |
| A new instance type is greyed out at launch | The Free Plan only allows free-tier types; m7i-flex.large is the largest. Upgrading the account plan unlocks the 8-vCPU types. |

## Files involved

| File | Role |
|---|---|
| [`agent/scripts/ec2-setup.sh`](../agent/scripts/ec2-setup.sh) | User data for the app instance |
| [`agent/scripts/loadgen-setup.sh`](../agent/scripts/loadgen-setup.sh) | User data for the load generator |
| [`agent/scripts/k8s-setup.sh`](../agent/scripts/k8s-setup.sh) | User data for the Kubernetes instance (k3s, testbed manifests, controller) |
| [`apps/image-service/k8s/testbed/`](../apps/image-service/k8s/testbed) | The k8s testbed: app, Prometheus, the HPA baseline |
| [`agent/scripts/install-dashboard.sh`](../agent/scripts/install-dashboard.sh) | Dashboard service and the load generator's address |
| [`agent/scripts/run_all.sh`](../agent/scripts/run_all.sh) | Calibration plus all measured runs, unattended |
| [`agent/scripts/run_test.sh`](../agent/scripts/run_test.sh) | One 5-minute run (`calibrate`, `rl`, `rl-shadow`, `threshold`, `hpa` (k8s), `static`) |
| [`agent/autoscaler/`](../agent/autoscaler) | The controller (state, policies, guards, Docker adapter, decision log, report) |
| [`agent/dashboard/`](../agent/dashboard) | The demo dashboard and the remote load driver |
| [`loadgen/`](../loadgen) | Locust file and the shared traffic shapes |
