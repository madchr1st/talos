# KRR Right-Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly in-cluster KRR run that reports right-sizing recommendations to ntfy, with a `CREATE_PR` toggle
that later lets the same job open a PR updating `resources:` in TrueForge `helm-release.yaml` files.

**Architecture:** Plain manifests in `clusters/main/kubernetes/system/krr/`. A CronJob runs KRR (official image) as an
initContainer writing JSON to an `emptyDir`; a stock Python container runs `krr-report.py` (standard library only,
shipped via `configMapGenerator`) which classifies, floors, rounds and reports, and in PR mode edits files through the
GitHub REST API.

**Tech Stack:** Flux kustomize-controller, SOPS/age, `robustadev/krr:v1.31.3`, `python:3.13-slim`, Python `unittest`
(no pytest on the workstation), ntfy HTTP API, GitHub REST API.

**Spec:** `docs/superpowers/specs/2026-11-23-krr-rightsizing-design.md`

## Global Constraints

- Always `kubectl --context aarau-talos01` (the current context may be a work cluster).
- Repo is public: no IPs, domains, e-mails in committed files; secrets only in SOPS `*.secret.yaml`.
- Flux substitutes `${...}` everywhere with strict mode; the generated ConfigMap carries the annotation
  `kustomize.toolkit.fluxcd.io/substitute: disabled`, and no manifest outside it may contain an unknown `${VAR}`.
- Keep `kustomization.yaml` resource lists sorted alphabetically.
- Images pinned to exact tags (Renovate updates them): `robustadev/krr:v1.31.3`, `python:3.13-slim` (pin the current
  patch tag, e.g. `3.13.x-slim`).
- Prometheus URL: `http://kube-prometheus-stack-prometheus.kube-prometheus-stack.svc:9090`.
- ntfy URL: `http://ntfy.ntfy.svc.cluster.local:10222`, topic `krr`, Bearer token `NTFY_TOKEN`.
- Sizing: CPU request = KRR p95, CPU limit written as `0` (= none on TrueForge); memory request = limit = KRR value
  (max + 15 %); KRR floors `--cpu-min 10 --mem-min 64`.
- Rounding up: CPU to multiples of 5m; memory to multiples of 16Mi; print memory as `NGi` when divisible by 1Gi,
  else `NMi`.
- Change threshold: `change` only if |rec − cur| > 20 % of cur **and** > 10m (CPU) / 32Mi (memory), for either
  resource; current missing → `change`.
- Memory floors (`namespace/container` → value): `plex/plex` 6Gi, `jellyfin/jellyfin` 6Gi, `immich/immich` 2Gi,
  `kometa/kometa` 2Gi, `paperless-ngx/paperless-ngx` 2Gi, `tdarr/tdarr` 2Gi.
- History is partial until `2026-12-02` (`FULL_HISTORY_FROM`); reports before that date are titled "(partial history)".
- PR mode: branch `krr/rightsizing`, title `chore(resources): KRR right-sizing YYYY-MM-DD`, label `krr`, never the
  `automerge` label.
- Schedule `0 6 * * 1` (UTC). `concurrencyPolicy: Forbid`, `successfulJobsHistoryLimit: 1`,
  `failedJobsHistoryLimit: 2`, `backoffLimit: 1`.
- Work on branch `feat/krr`; deploy checkpoints (Task 1, Task 4) need the branch merged to `main` via PR so Flux
  applies it. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. KRR returns `"?"`/`null` for a value (no data, new pod) → row status `manual` with reason "no data", never a crash
   or a 0 recommendation. (Task 2)
2. A floor applies → recommendation = floor, the report marks it `floor`, and the threshold is judged against the
   floored value. (Task 2)
3. A `helm-release.yaml` whose `resources:` already holds extra keys (`nvidia.com/gpu: 1`, `amd.com/gpu: 0`) keeps
   them after the edit; comments and `${VAR}` lines elsewhere in the file are byte-identical. (Task 5)
4. ntfy unreachable or token rejected → job exits non-zero (Job shows Failed) after printing the full report to
   stdout. (Task 3)
5. An open PR on `krr/rightsizing` already exists → the branch is reset and force-updated, the PR body updated, no
   second PR. (Task 5)

---

### Task 1: Namespace, RBAC, secret, CronJob running KRR only; capture a fixture

**Files:**
- Create: `clusters/main/kubernetes/system/krr/ks.yaml`
- Create: `clusters/main/kubernetes/system/krr/app/kustomization.yaml`
- Create: `clusters/main/kubernetes/system/krr/app/namespace.yaml`
- Create: `clusters/main/kubernetes/system/krr/app/rbac.yaml`
- Create: `clusters/main/kubernetes/system/krr/app/krr.secret.yaml` (SOPS)
- Create: `clusters/main/kubernetes/system/krr/app/cronjob.yaml`
- Modify: `clusters/main/kubernetes/system/kustomization.yaml` (add `- krr/ks.yaml` between `kube-prometheus-stack`
  and `kubernetes-reflector`)
- Create: `clusters/main/kubernetes/system/krr/tests/fixtures/krr-sample.json`

**Interfaces:**
- Produces: namespace `krr` (label `policy-group/ntfy-clients: "true"`), ServiceAccount `krr`, Secret `krr` with key
  `NTFY_TOKEN` (later `GITHUB_TOKEN`), CronJob `krr` with initContainer `krr` writing `/work/krr.json`, `emptyDir`
  volume `work` mounted at `/work`. Fixture file used by Tasks 2–3.

- [ ] **Step 1: ntfy user** — the user runs (already handed over):
  `ntfy user add --role=user krr`, `ntfy access krr krr write-only`, `ntfy token add --label=krr-cronjob krr` in the
  ntfy pod. Wait for the user to confirm the token is ready; never ask them to paste it in chat.
- [ ] **Step 2: Write the manifests.** `ks.yaml` like `system/volsync/ks.yaml` (name `krr`, path
  `clusters/main/kubernetes/system/krr/app`). `rbac.yaml`: ServiceAccount + ClusterRole `krr` (rules copied from KRR's
  `docs/krr-in-cluster/krr-in-cluster-job.yaml`, read-only) + ClusterRoleBinding. `cronjob.yaml`: Global Constraints
  schedule/limits; initContainer `krr` = `python krr.py simple -p <prometheus> --cpu-min 10 --mem-min 64 -f json
  --fileoutput /work/krr.json`, resources requests 100m/1Gi, limit memory 1Gi; main container `report` for now
  `python:3.13-slim` running `cat /work/krr.json`; `restartPolicy: Never`, `runAsNonRoot`. `krr.secret.yaml` with
  `stringData.NTFY_TOKEN`, then `sops --encrypt --in-place` it (user provides the token value locally).
- [ ] **Step 3: Validate render.** Run: `kubectl kustomize clusters/main/kubernetes/system/krr/app >/dev/null && echo ok`
  Expected: `ok` (the secret renders encrypted; that is fine).
- [ ] **Step 4: Commit, push branch, PR, merge** (user merges). Then
  `flux --context aarau-talos01 reconcile kustomization krr --with-source`. Expected: Ready=True.
- [ ] **Step 5: Run once.** `kubectl --context aarau-talos01 -n krr create job --from=cronjob/krr krr-manual-1`, wait
  for Complete, then `kubectl ... -n krr logs job/krr-manual-1 -c report > /tmp/krr.json` (use the scratchpad).
  Expected: JSON with top-level `scans`, each scan has `object.{namespace,name,kind,container,labels,allocations}`
  and `recommended.{requests,limits}.{cpu,memory}.value`.
- [ ] **Step 6: Sanitize and commit fixture.** Strip `object.annotations`, `object.pods`, `config` and any value that
  looks like a domain/IP; keep ~10 scans covering: a TrueForge main container (e.g. sonarr), its exportarr sidecar,
  plex, a CNPG `postgres` container, a kube-system workload without Flux labels, one with `"?"` values if present (else
  hand-edit one to `"?"`). `grep -E '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' fixture` must print nothing. Commit.

### Task 2: Recommendation model (pure functions)

**Files:**
- Create: `clusters/main/kubernetes/system/krr/app/krr-report.py`
- Create: `clusters/main/kubernetes/system/krr/app/krr-config.json`
- Test: `clusters/main/kubernetes/system/krr/tests/test_krr_report.py`

**Interfaces:**
- Consumes: fixture schema from Task 1.
- Produces (in `krr-report.py`, importable via `importlib` since the filename has a dash):
  - `@dataclass Row: namespace: str, workload: str, kind: str, container: str, release: str | None,
    cur_cpu_req_m: int | None, cur_cpu_lim_m: int | None, cur_mem_req_mi: int | None, cur_mem_lim_mi: int | None,
    rec_cpu_m: int | None, rec_mem_mi: int | None, floored: bool, status: str  # "change" | "ok" | "manual",
    reason: str`
  - `load_config(path: str) -> dict` — keys `floors` (`{"ns/container": "6Gi"}`), `full_history_from` (ISO date)
  - `parse_mem_mi(s: str) -> int` (`"6Gi"`→6144, `"512Mi"`→512)
  - `round_cpu_m(cores: float) -> int`, `round_mem_mi(bytes_: float) -> int`, `fmt_mem(mi: int) -> str`
  - `build_rows(krr: dict, config: dict) -> list[Row]` sorted by namespace, workload, container
- `release` = `labels["helm.toolkit.fluxcd.io/name"]` when `labels["helm.toolkit.fluxcd.io/namespace"]` is present.
  Status rules: no Flux labels → `manual` "not Flux-managed"; container != release → `manual` "sidecar/other
  container"; rec value `"?"`/None → `manual` "no data"; else threshold rule from Global Constraints → `change`/`ok`.

- [ ] **Step 1: Write failing tests** in `test_krr_report.py` (`unittest`):
  - `test_parse_mem_mi`: `parse_mem_mi("6Gi")==6144`, `parse_mem_mi("512Mi")==512`.
  - `test_rounding`: `round_cpu_m(0.0121)==15`, `round_cpu_m(0.010)==10`, `round_mem_mi(100*2**20+1)==112`,
    `fmt_mem(6144)=="6Gi"`, `fmt_mem(112)=="112Mi"`.
  - `test_floor_applies`: synthetic scan plex/plex, rec mem 2.2 GiB → `rec_mem_mi==6144`, `floored is True`,
    status `change` when current is 1Gi.
  - `test_floor_and_threshold`: current mem 6000Mi, rec 5900Mi, floor 6Gi → `rec_mem_mi==6144`,
    status `ok` (|6144−6000| < 20 %); and current 4096Mi → `change`.
  - `test_threshold`: cur 75m/200Mi, rec 80m/210Mi → `ok`; rec 20m/200Mi → `change`; cur 100m, rec 115m (15 %) → `ok`;
    cur 20m rec 28m (40 % but 8m) → `ok`.
  - `test_unknown_values`: rec cpu `"?"` → `manual`, reason `"no data"`, no exception.
  - `test_sidecar_and_unmanaged`: fixture sonarr-exportarr → `manual` "sidecar/other container"; kube-system row
    without Flux labels → `manual` "not Flux-managed"; sonarr main → `release=="sonarr"`.
  - `test_fixture_loads`: `build_rows(json.load(fixture), config)` returns rows, all statuses in the three values.
- [ ] **Step 2: Run, expect failures.**
  Run: `cd clusters/main/kubernetes/system/krr && python3 -m unittest discover -s tests -v`
  Expected: errors (module/functions missing).
- [ ] **Step 3: Implement** the functions above. `krr-config.json` = floors from Global Constraints +
  `"full_history_from": "2026-12-02"`. Script must not contain the two-character sequence `${`.
- [ ] **Step 4: Run tests.** Same command. Expected: all PASS.
- [ ] **Step 5: Commit.**

### Task 3: Report rendering, ntfy delivery, `main()`

**Files:**
- Modify: `clusters/main/kubernetes/system/krr/app/krr-report.py`
- Test: `clusters/main/kubernetes/system/krr/tests/test_krr_report.py`

**Interfaces:**
- Consumes: `Row`, `build_rows`, `load_config` (Task 2).
- Produces:
  - `render_summary(rows: list[Row], today: date, config: dict) -> str` — ≤ 4000 bytes: title line
    `KRR report YYYY-MM-DD` (+ ` (partial history)` if `today < full_history_from`), totals of current vs recommended
    requests over rows with status `change`/`ok` (CPU in m, memory in Gi with 1 decimal), counts per status, top 10
    `change` rows by |Δmemory| then |ΔCPU|.
  - `render_markdown(rows: list[Row], today: date, config: dict) -> str` — summary on top, then one Markdown table:
    `| Namespace | Workload | Container | CPU req (cur → rec) | Mem req/limit (cur → rec) | Status | Note |`; `Note`
    holds reason or `floor`.
  - `send_ntfy(url: str, token: str, title: str, summary: str, report_md: str, filename: str) -> None` — one `PUT`
    to `{url}/krr` with body = report, headers `Authorization: Bearer`, `X-Title`, `X-Filename`, `X-Message`
    (summary with newlines encoded as literal `\n`, which ntfy expands); raises on HTTP status ≥ 300.
  - `main() -> int` — env `KRR_JSON` (default `/work/krr.json`), `KRR_CONFIG` (default `/scripts/krr-config.json`),
    `NTFY_URL`, `NTFY_TOKEN`, `CREATE_PR` (`false` | `dry-run` | `true`, default `false`). Order: build rows → print
    markdown to stdout → send ntfy → PR mode (Task 5); PR mode runs even if ntfy failed, then any error → exit 1.

- [ ] **Step 1: Write failing tests:**
  - `test_summary_partial_flag`: `today=date(2026,11,30)` → first line endswith `(partial history)`;
    `date(2026,12,2)` → not.
  - `test_summary_size`: 200 synthetic `change` rows → `len(render_summary(...).encode()) <= 4000`.
  - `test_markdown_table`: contains header row above and a row for sonarr with `→`.
  - `test_send_ntfy_headers`: patch `urllib.request.urlopen` with a fake capturing the `Request`; assert method `PUT`,
    URL endswith `/krr`, headers `Authorization == "Bearer tk"`, `X-filename` (urllib capitalizes) ends `.md`,
    `"\n"` not in `X-message`.
  - `test_main_exits_1_when_ntfy_fails`: fake urlopen raising `URLError`, fixture as `KRR_JSON`, capture stdout →
    return value 1 and stdout contains the table header.
- [ ] **Step 2: Run, expect failures.** Command from Task 2. Expected: failures for new tests.
- [ ] **Step 3: Implement.** `if __name__ == "__main__": sys.exit(main())`.
- [ ] **Step 4: Run tests.** Expected: all PASS.
- [ ] **Step 5: Commit.**

### Task 4: Wire the script into the CronJob; first real report

**Files:**
- Modify: `clusters/main/kubernetes/system/krr/app/kustomization.yaml` (configMapGenerator `krr-report` from
  `krr-report.py` and `krr-config.json`, `generatorOptions.annotations: {kustomize.toolkit.fluxcd.io/substitute:
  disabled}`)
- Modify: `clusters/main/kubernetes/system/krr/app/cronjob.yaml` (report container: `python /scripts/krr-report.py`,
  ConfigMap volume at `/scripts`, env `NTFY_URL`, `NTFY_TOKEN` from Secret `krr`, `CREATE_PR: "false"`, resources
  requests 10m/64Mi limit memory 128Mi)
- Modify: `CLAUDE.md` (short "Right-sizing (KRR)" section: where floors live, the `CREATE_PR` toggle and
  `GITHUB_TOKEN` requirement, manual run command)

**Interfaces:**
- Consumes: `main()` (Task 3), Secret/CronJob (Task 1).

- [ ] **Step 1: Edit manifests.** Verify render: `kubectl kustomize clusters/main/kubernetes/system/krr/app | grep -c
  'substitute: disabled'` → `1`; the CronJob references the hashed ConfigMap name.
- [ ] **Step 2: Unit tests still pass** (Task 2 command).
- [ ] **Step 3: Commit, PR, user merges, reconcile** (as Task 1 Step 4).
- [ ] **Step 4: Run once.** `create job --from=cronjob/krr krr-manual-2`. Expected: Job Complete; the user confirms a
  ntfy notification in topic `krr` titled `KRR report 2026-… (partial history)` with a `.md` attachment that opens
  as the table; `kubectl logs job/krr-manual-2 -c report` shows the same table.
- [ ] **Step 5: Clean up** `kubectl ... -n krr delete job krr-manual-1 krr-manual-2`.

### Task 5: PR mode (toggle stays off)

**Files:**
- Modify: `clusters/main/kubernetes/system/krr/app/krr-report.py`
- Test: `clusters/main/kubernetes/system/krr/tests/test_krr_report.py`
- Create: `clusters/main/kubernetes/system/krr/tests/fixtures/helm-release-gpu.yaml` (copy of
  `apps/plex/app/helm-release.yaml`), `helm-release-noresources.yaml` (copy of `apps/sonarr/app/helm-release.yaml`)

**Interfaces:**
- Consumes: `Row` (Task 2), `main()` hook (Task 3).
- Produces:
  - `update_resources_block(text: str, cpu_m: int, mem_mi: int) -> str` — inside `spec.values` (4-space indent level
    used by this repo), set `resources.requests.cpu`, `resources.requests.memory`, `resources.limits.cpu: 0`,
    `resources.limits.memory`; keep all other keys under `resources`; insert a `resources:` block directly after the
    `  values:` line if absent. Line-based edit, no YAML library; everything outside the block unchanged.
  - `class GitHub(token: str, repo: str = "madchr1st/talos")` with `find_helm_releases() -> dict[tuple[str, str],
    str]` ((name, namespace) → path, via `git/trees/main?recursive=1` filtered to `*/helm-release.yaml` and parsing
    `metadata.name`/`namespace` from each file), `get_file(path) -> tuple[str, str]` (text, sha), and
    `open_or_update_pr(changes: dict[str, str], title: str, body: str) -> str` (path → new text; resets branch
    `krr/rightsizing` to `main`, one commit per file via Contents API, creates or updates the PR, adds label `krr`,
    returns PR URL).
  - `plan_pr(rows: list[Row], gh: GitHub) -> tuple[dict[str, str], list[Row]]` — for `change` rows: map
    `(release, namespace)` → path; skip (→ `manual`, reason) when the file's chart `sourceRef.name` is not
    `truecharts`, or a sibling `values-sec.yaml` has a top-level `resources:` key.
  - In `main()`: `CREATE_PR=dry-run` prints a unified diff (`difflib`) per file and makes no write calls;
    `true` requires `GITHUB_TOKEN` (exit 1 with message if missing).

- [ ] **Step 1: Write failing tests:**
  - `test_update_keeps_gpu_and_rest`: on `helm-release-gpu.yaml` with (150, 6144) → result has `cpu: 150m`,
    `memory: 6Gi` under requests, `cpu: 0` and `memory: 6Gi` under limits, still `nvidia.com/gpu: 1`; all lines
    outside the `resources:` block equal the input's (compare with the block removed).
  - `test_update_inserts_block`: on `helm-release-noresources.yaml` → exactly one `    resources:` line, directly
    after `  values:`; `${DOMAIN_0}` lines unchanged.
  - `test_update_idempotent`: applying twice gives the same text.
  - `test_plan_pr_skips_non_truecharts`: fake `GitHub` returning a file with `name: prometheus-community` → row
    becomes `manual`.
  - `test_open_or_update_pr_existing`: fake HTTP layer where list-PRs returns one open PR → no POST to `/pulls`, one
    PATCH to that PR.
  - `test_dry_run_no_writes`: fake HTTP layer records methods → only `GET`s.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** with `urllib.request` + `json`; GitHub HTTP goes through one method `_req(method, path,
  body=None)` so tests fake it.
- [ ] **Step 4: Run tests.** Expected: all PASS.
- [ ] **Step 5: Local dry run (no token needed).** In `clusters/main/kubernetes/system/krr`:
  `KRR_JSON=tests/fixtures/krr-sample.json KRR_CONFIG=app/krr-config.json CREATE_PR=dry-run NTFY_URL=http://127.0.0.1:9 NTFY_TOKEN=x python3 app/krr-report.py`.
  Main must run PR mode before propagating the ntfy failure, so diffs print and the exit code is 1. Expected:
  plausible diffs for sonarr and plex (GPU key kept). The unauthenticated `GET`s against the public repo are fine.
- [ ] **Step 6: Commit, PR, user merges.** `CREATE_PR` stays `"false"`. Document in `CLAUDE.md` how to switch on:
  create a fine-grained PAT (this repo only; Contents + Pull requests read/write), add `GITHUB_TOKEN` to
  `krr.secret.yaml`, set `CREATE_PR: "true"`.

### Task 6: Initial manual right-sizing pass (on or after 2026-12-02)

**Files:** `helm-release.yaml` files listed as `change` in that week's report; chart-specific keys for `manual` rows
that matter (CNPG clusters, kube-prometheus-stack, traefik, cilium).

- [ ] **Step 1:** Read the Monday report (pod log of the last Job). Apply `change` rows (with `limits.cpu: 0`),
  one PR per layer (`apps`, `networking`, `system`/`kube-system`).
- [ ] **Step 2:** After merge: `kubectl --context aarau-talos01 describe node k8s-control-1 | grep -A6 Allocated`.
  Expected: CPU requests well below the current 82 %.
- [ ] **Step 3:** For 7 days watch for `OOMKilled` (`kubectl get pods -A` restarts, Alertmanager in ntfy `alerts`).
  Raise floors in `krr-config.json` for any app that was killed.
