# KRR right-sizing: weekly in-cluster report, PR mode behind a toggle

Date: 2026-11-23 · Status: draft, awaiting approval (builder check done)

## Intent

The cluster is a single node (`k8s-control-1`, ~10 CPU / ~29 GiB allocatable). Today CPU requests sit at 82 %
and memory requests at 64 %, while limits are 1159 % CPU / 693 % memory. 28 of 34 apps set no `resources:`, so
55 containers run on the TrueForge default (requests 75m / 200Mi, limits 1500m / 2400Mi). Requests therefore do not
reflect real usage: the scheduler thinks the node is nearly full while it is not, and limits protect nothing.

Goal: make every container's requests/limits represent **actual usage plus headroom**, so the limited node is used as
fully as possible, and keep it that way with a **recurring** check driven by Robusta KRR.

## Goals

- Weekly KRR run **inside the cluster** against the existing Prometheus (14 d retention = KRR's default window).
- Weekly report delivered privately via the existing ntfy.
- A **feature toggle** that, once switched on, makes the same job open a GitHub PR that updates `resources:` in the
  affected `helm-release.yaml` files.
- A one-time manual pass applying the first report's recommendations.

## Non-goals

- Auto-merging resource PRs (the existing automerge workflow must not pick them up).
- Covering things that are not a chart's main container in PR mode (sidecars, CNPG clusters, system/kube-system
  charts with their own values layout). They appear in the report, flagged "manual".
- VPA, Goldilocks, Robusta platform/UI, or any SaaS.
- Building/maintaining a custom container image.

## Anti-goals

- Recommendations that OOM-kill apps: memory must keep a buffer above the observed peak.
- Report or PR leaking `clusterenv.yaml` values (IPs, domains). The repo is public; the report is not posted there.
- Churn: weekly PRs that flip values by a few millicores.

## Constraints

- GitOps via Flux; everything lives under `clusters/main/kubernetes/`, conventions per `CLAUDE.md`
  (sorted lists, `${VAR}` for anything sensitive, SOPS for secrets, policy-group netpols).
- Prometheus: `http://kube-prometheus-stack-prometheus.kube-prometheus-stack.svc:9090`, no NetworkPolicy on that
  namespace (no change needed there).
- ntfy: `http://ntfy.ntfy.svc.cluster.local:10222`, `deny-all` default access, token auth (as Alertmanager does).
  Ingress into ntfy is allowed for namespaces labelled `policy-group/ntfy-clients: "true"`.
  **Attachments expire after 3 h** (`NTFY_ATTACHMENT_EXPIRY_DURATION: 3h`), messages are cached 12 h.
- Workloads rendered by Flux carry `helm.toolkit.fluxcd.io/name` / `helm.toolkit.fluxcd.io/namespace` labels, which
  map a workload to its HelmRelease.
- TrueForge charts apply top-level `values.resources` to **every container** of the chart that does not override it
  (verified: sonarr's `exportarr` sidecar renders with the same values). Apps with TrueForge sidecars today:
  prowlarr/radarr/sonarr (exportarr), qbittorrent (gluetun), home-assistant (codeserver).
- **History**: most apps were migrated from K3s on 2026-11-18, so Prometheus has ~5 d of history for them. Reports
  before ~2026-12-02 are flagged as partial; the initial manual pass waits for a full 14 d window.

## Approaches considered

1. **CronJob, KRR image + stock Python image, script in a ConfigMap, report to ntfy** — *chosen*. No custom image,
   Renovate keeps both images current, report stays private, PR mode is the same script talking to the GitHub REST
   API (no git/gh binaries needed).
2. Same job, report as a weekly-updated GitHub issue — nicer tables and history, but the repo is public (usage data
   per app becomes public) and a GitHub token is needed from day one. Rejected.
3. In-cluster GitHub Actions runner (ARC) running KRR — heavy component to operate for the same result. Rejected.

## Sizing policy (KRR `simple` strategy)

- **CPU request** = p95 of usage over 14 d (KRR default). **No CPU limit** (KRR default; CPU is compressible, idle
  CPU stays usable by bursts, only real usage is reserved). On TrueForge charts this is written as `limits.cpu: 0`
  (verified with `helm template` of sonarr 28.1.8: the rendered container then has no CPU limit; omitting the key
  keeps the chart default 1500m). No kustomize patching.
- **Memory request = memory limit** = max over 14 d + 15 % buffer (KRR default).
- Floors: `--cpu-min 10` (m), `--mem-min 64` (Mi) to avoid absurdly small values for idle apps.
- **Per-app memory floors** (user decision, option A): one list in the script's config (ConfigMap, e.g.
  `floors.yaml`), keyed `namespace/container`. The recommended memory never goes below the floor; the report shows
  when a floor was applied. Protects rare peaks (transcodes, ML, OCR, scans) that a 14 d window can miss. Initial
  list (my proposal, from 14 d peaks queried 2026-11-23; adjust freely):

  | Container | Observed peak | Floor |
  |---|---|---|
  | `plex/plex` | 2019Mi | 6Gi (transcode headroom, as hand-set today) |
  | `jellyfin/jellyfin` | 2344Mi | 6Gi (same) |
  | `immich/immich` | 1038Mi | 2Gi |
  | `kometa/kometa` | 1660Mi | 2Gi |
  | `paperless-ngx/paperless-ngx` | 1304Mi | 2Gi |
  | `tdarr/tdarr` | 827Mi | 2Gi |

  Authelia's hand-set 4Gi limit is not protected (peak 131Mi; nothing suggests it needs more).
- **Change threshold** (anti-churn): a container is only listed as "change" in the report / touched in a PR when the
  recommendation differs from the current value by more than 20 % **and** by more than 10m CPU / 32Mi memory.
  Everything else is "ok".
- Values are rounded up (CPU to 5m, memory to 16Mi) so the files stay readable.

## Design

### Placement

`clusters/main/kubernetes/system/krr/` (cluster tooling, sits next to kube-prometheus-stack), added to
`system/kustomization.yaml` in sorted position. Plain manifests, no Helm chart:

```
system/krr/
  ks.yaml
  app/
    kustomization.yaml
    namespace.yaml          # labels: policy-group/ntfy-clients: "true"
    rbac.yaml               # ServiceAccount, read-only ClusterRole (from KRR's in-cluster example) + binding
    configmap.yaml          # krr-report.py (via kustomize configMapGenerator from a .py file is fine too)
    cronjob.yaml
    krr.secret.yaml         # SOPS: NTFY_TOKEN, later GITHUB_TOKEN
```

### CronJob

- Schedule: weekly, Monday 06:00 UTC (no TZ variable exists in `clustersettings.secret.yaml`).
  `concurrencyPolicy: Forbid`, `successfulJobsHistoryLimit: 1`, `failedJobsHistoryLimit: 2`, `backoffLimit: 1`.
- **initContainer `krr`**: `robustadev/krr:v1.31.3` (pinned, Renovate-updated),
  `python krr.py simple -p <prometheus> --cpu-min 10 --mem-min 64 -f json --fileoutput /work/krr.json`
  (exact flag spelling to be checked against `krr simple --help` of the pinned version). Writes to an `emptyDir`.
- **container `report`**: `python:3.x-slim` (pinned), runs `/scripts/krr-report.py` from the ConfigMap, standard
  library only (urllib, json). Reads `/work/krr.json`.
- Own `resources:` set (KRR itself needs ~1Gi memory per its example).

### Report (always)

`krr-report.py` produces:

- **Summary** (ntfy message body, < 4 KB): total current vs. recommended CPU/memory requests for the node, count of
  containers to change, top 10 changes by absolute delta.
- **Full report** (Markdown): one row per container: namespace, workload, container, current req/limit, recommended,
  status `change` / `ok` / `manual` (manual = not mappable to a HelmRelease main container), and for `change` rows
  the HelmRelease it maps to.
- Delivery: POST to ntfy topic `krr` with `Authorization: Bearer $NTFY_TOKEN`; summary as message, full report as
  attachment (`X-Filename: krr-report-YYYY-MM-DD.md`).
- The full report is also printed to stdout, so it survives in the last Job's pod log (`successfulJobsHistoryLimit: 1`)
  for a week, independent of the 3 h ntfy attachment expiry.

### Mapping container → file

For each recommendation: read the workload's `helm.toolkit.fluxcd.io/name` and `/namespace` labels (KRR JSON
contains the workload; the script queries the API server with the ServiceAccount token if labels are not in the
JSON). Map to the repo file whose HelmRelease has that name+namespace. It is a "main container" when the container
name equals the HelmRelease name (TrueForge convention) — only those get `change`; all others `manual`.
Workloads without the Flux labels → `manual`.

Sidecars inherit the top-level values (see constraints). *My call*: accept that for the five affected apps (the
sidecars over-reserve a little) rather than editing nested per-container keys; the report lists them so they can be
tuned by hand if it ever matters.

### PR mode (toggle, off by default)

- Toggle: env `CREATE_PR: "false"` on the report container in `cronjob.yaml`; switching it on is a one-line Git
  change. Requires `GITHUB_TOKEN` in `krr.secret.yaml` (fine-grained PAT, only this repo, permissions *Contents:
  read/write* and *Pull requests: read/write*). With `CREATE_PR=false` the token is not needed.
- When on, for all `change` rows:
  1. GitHub API: get `main` SHA, create/reset branch `krr/rightsizing` (one rolling branch; an open PR is updated,
     not duplicated).
  2. For each affected `helm-release.yaml`: fetch content, set `spec.values.resources` to
     `requests: {cpu, memory}`, `limits: {memory}` (remove `limits.cpu`), keep any other keys under `resources`
     (e.g. `nvidia.com/gpu`). Edit textually so comments and formatting outside the block are preserved; if the file
     has no `resources:` block, insert one under `values:`.
  3. Commit via the Contents API, open/update PR "chore(resources): KRR right-sizing YYYY-MM-DD" with the report
     table as body, label `krr`.
- The automerge workflow uses pascalgn/automerge-action, which only merges PRs carrying its merge label, so the
  `krr` PR is not auto-merged. Verify during the build.
- Files with a `values-sec.yaml` that also sets `resources` are skipped (`manual`): the script cannot read SOPS.

### ntfy access (manual, one-time)

ntfy users/ACLs live in its `user.db`, not in Git. Create a `krr` user with write-only access to topic `krr`, issue
a token for it (goes into `krr.secret.yaml`), and grant the user's own account read access to `krr`. Documented as a
step in the plan, executed with `kubectl exec` into the ntfy pod.

### One-time initial pass

After the first report with a full 14 d window (~2026-12-02), apply its `change` rows by hand (with Claude) in one PR per layer, plus the `manual` rows
that matter (CNPG clusters, kube-prometheus-stack, traefik, cilium, etc. via their chart-specific keys). Watch for
OOMKills / restarts for a week before trusting the cycle.

## Left to the builder

- Exact KRR CLI flags for v1.31.3 and the JSON schema field names.
- Python image tag; rounding helpers; the textual YAML edit implementation.
- Whether the ConfigMap is generated from a `.py` file via `configMapGenerator` (preferred for readability).

## Decisions log

- Approach 1 (CronJob + ntfy report, PR toggle) — user.
- Memory floors as one central list (option A) — user. Floor values above — my proposal.
- No CPU limit via `limits.cpu: 0`, no kustomize patching — user constraint, mechanism verified.
- Sidecars inherit main values — my call.
- UTC schedule, Monday 06:00 — my call.
- 2026-11-23, after the first real run (requests as specified would have been 16.7 cores / 45.8 GiB on a
  10-core / 28.7 GiB node) — user decisions:
  - CPU request capped at 1000m per container (`cpu_request_max_m`), still no CPU limit.
  - Memory floors raise only the **limit**; request = peak + 15 %, limit = max(request, floor).
  - `full_history_from` = 2026-12-05 (14 d after the GPU went in on 2026-11-21; earlier data has CPU transcodes).
- Floors and "main container" apply only when workload **and** container are named like the HelmRelease
  (immich has three workloads with a container named `immich`).
- Files are read via raw.githubusercontent.com (no API rate limit); `values: {}` is expanded; files without a
  `values:` block are `manual`.
- Known gap: CNPG Postgres pods are not scanned by KRR (no Deployment/StatefulSet owner).
