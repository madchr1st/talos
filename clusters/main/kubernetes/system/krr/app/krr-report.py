#!/usr/bin/env python3
"""Turn KRR JSON into a right-sizing report (ntfy) and, optionally, a PR.

Standard library only; runs in a stock python image. Shipped as a ConfigMap with
Flux substitution disabled, but keep the dollar-brace sequence out of this file anyway.
See docs/superpowers/specs/2026-11-23-krr-rightsizing-design.md.
"""

import datetime
import difflib
import json
import math
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

MI = 2**20
CPU_STEP_M = 5
MEM_STEP_MI = 16
THRESHOLD_PCT = 0.20
THRESHOLD_CPU_M = 10
THRESHOLD_MEM_MI = 32
FLUX_NAME = "helm.toolkit.fluxcd.io/name"
FLUX_NAMESPACE = "helm.toolkit.fluxcd.io/namespace"


@dataclass
class Row:
    namespace: str
    workload: str
    kind: str
    container: str
    release: str | None
    cur_cpu_req_m: int | None
    cur_cpu_lim_m: int | None
    cur_mem_req_mi: int | None
    cur_mem_lim_mi: int | None
    rec_cpu_m: int | None
    rec_mem_mi: int | None  # memory request (peak + buffer)
    rec_mem_lim_mi: int | None  # memory limit = max(request, floor)
    floored: bool
    capped: bool
    status: str  # "change" | "ok" | "manual"
    reason: str


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def parse_mem_mi(s: str) -> int:
    if s.endswith("Gi"):
        return int(s[:-2]) * 1024
    if s.endswith("Mi"):
        return int(s[:-2])
    raise ValueError(f"unsupported memory value: {s!r}")


def round_cpu_m(cores: float) -> int:
    return int(math.ceil(round(cores * 1000, 6) / CPU_STEP_M) * CPU_STEP_M)


def round_mem_mi(bytes_: float) -> int:
    return int(math.ceil(bytes_ / MI / MEM_STEP_MI) * MEM_STEP_MI)


def fmt_mem(mi: int) -> str:
    return f"{mi // 1024}Gi" if mi % 1024 == 0 else f"{mi}Mi"


def _known(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _cur_m(value) -> int | None:
    return int(round(value * 1000)) if _known(value) else None


def _cur_mi(value) -> int | None:
    return int(round(value / MI)) if _known(value) else None


def _rec(recommended: dict, kind: str, resource: str):
    value = (recommended.get(kind) or {}).get(resource)
    if isinstance(value, dict):
        value = value.get("value")
    return value if _known(value) else None


def _differs(cur: int | None, rec: int, abs_min: int) -> bool:
    if cur is None:
        return True
    delta = abs(rec - cur)
    return delta > abs_min and delta > THRESHOLD_PCT * cur


def build_rows(krr: dict, config: dict) -> list[Row]:
    floors = {k: parse_mem_mi(v) for k, v in (config.get("floors") or {}).items()}
    cpu_max_m = config.get("cpu_request_max_m")
    result = []
    for scan in krr.get("scans") or []:
        obj = scan["object"]
        alloc = obj.get("allocations") or {}
        req, lim = alloc.get("requests") or {}, alloc.get("limits") or {}
        labels = obj.get("labels") or {}
        release = labels.get(FLUX_NAME) if labels.get(FLUX_NAMESPACE) else None

        rec_cpu = _rec(scan.get("recommended") or {}, "requests", "cpu")
        rec_mem = _rec(scan.get("recommended") or {}, "requests", "memory")
        rec_cpu_m = round_cpu_m(rec_cpu) if rec_cpu is not None else None
        capped = rec_cpu_m is not None and cpu_max_m is not None and rec_cpu_m > cpu_max_m
        if capped:
            rec_cpu_m = cpu_max_m
        rec_mem_mi = round_mem_mi(rec_mem) if rec_mem is not None else None
        # main workload + container carry the HelmRelease name (TrueForge convention)
        is_main = release is not None and obj["name"] == release and obj["container"] == release
        floor = floors.get(f"{obj['namespace']}/{obj['container']}") if is_main else None
        floored = rec_mem_mi is not None and floor is not None and rec_mem_mi < floor
        rec_mem_lim_mi = floor if floored else rec_mem_mi

        row = Row(
            namespace=obj["namespace"], workload=obj["name"], kind=obj.get("kind", ""), container=obj["container"],
            release=release,
            cur_cpu_req_m=_cur_m(req.get("cpu")), cur_cpu_lim_m=_cur_m(lim.get("cpu")),
            cur_mem_req_mi=_cur_mi(req.get("memory")), cur_mem_lim_mi=_cur_mi(lim.get("memory")),
            rec_cpu_m=rec_cpu_m, rec_mem_mi=rec_mem_mi, rec_mem_lim_mi=rec_mem_lim_mi, floored=floored,
            capped=capped, status="ok", reason="",
        )
        if release is None:
            row.status, row.reason = "manual", "not Flux-managed"
        elif not is_main:
            row.status, row.reason = "manual", "sidecar/other container"
        elif rec_cpu_m is None or rec_mem_mi is None:
            row.status, row.reason = "manual", "no data"
        elif (_differs(row.cur_cpu_req_m, rec_cpu_m, THRESHOLD_CPU_M)
              or _differs(row.cur_mem_req_mi, rec_mem_mi, THRESHOLD_MEM_MI)
              or _differs(row.cur_mem_lim_mi, rec_mem_lim_mi, THRESHOLD_MEM_MI)):
            row.status = "change"
        if not row.reason:
            row.reason = ", ".join(n for n, on in (("cpu cap", capped), ("floor", floored)) if on)
        result.append(row)
    result.sort(key=lambda r: (r.namespace, r.workload, r.container))
    return result


SUMMARY_MAX_BYTES = 4000
TOP_N = 10
TABLE_HEADER = ("| Namespace | Workload | Container | CPU req (cur → rec) | Mem req/limit (cur → rec) | Status | Note |\n"
                "|---|---|---|---|---|---|---|")


def _title(today: datetime.date, config: dict) -> str:
    title = f"KRR report {today.isoformat()}"
    if today < datetime.date.fromisoformat(config["full_history_from"]):
        title += " (partial history)"
    return title


def _m(v: int | None) -> str:
    return "-" if v is None else f"{v}m"


def _mi(v: int | None) -> str:
    return "-" if v is None else fmt_mem(v)


def _delta(r: Row) -> tuple[int, int]:
    return (abs((r.rec_mem_mi or 0) - (r.cur_mem_req_mi or 0)), abs((r.rec_cpu_m or 0) - (r.cur_cpu_req_m or 0)))


def render_summary(rows: list[Row], today: datetime.date, config: dict) -> str:
    sized = [r for r in rows if r.status in ("change", "ok")]
    cur_cpu = sum(r.cur_cpu_req_m or 0 for r in sized)
    rec_cpu = sum(r.rec_cpu_m or 0 for r in sized)
    cur_mem = sum(r.cur_mem_req_mi or 0 for r in sized) / 1024
    rec_mem = sum(r.rec_mem_mi or 0 for r in sized) / 1024
    counts = {s: sum(1 for r in rows if r.status == s) for s in ("change", "ok", "manual")}
    lines = [
        _title(today, config),
        f"CPU requests: {cur_cpu}m -> {rec_cpu}m",
        f"Memory requests: {cur_mem:.1f}Gi -> {rec_mem:.1f}Gi",
        f"change: {counts['change']}, ok: {counts['ok']}, manual: {counts['manual']}",
    ]
    top = sorted((r for r in rows if r.status == "change"), key=_delta, reverse=True)[:TOP_N]
    if top:
        lines.append("Top changes:")
    for r in top:
        line = (f"- {r.namespace}/{r.container}: cpu {_m(r.cur_cpu_req_m)} -> {_m(r.rec_cpu_m)}, "
                f"mem {_mi(r.cur_mem_req_mi)} -> {_mi(r.rec_mem_mi)}")
        if len("\n".join(lines + [line]).encode()) > SUMMARY_MAX_BYTES:
            break
        lines.append(line)
    return "\n".join(lines)


def render_markdown(rows: list[Row], today: datetime.date, config: dict) -> str:
    out = [render_summary(rows, today, config), "", TABLE_HEADER]
    for r in rows:
        out.append(
            f"| {r.namespace} | {r.workload} | {r.container} | {_m(r.cur_cpu_req_m)} → {_m(r.rec_cpu_m)} "
            f"| {_mi(r.cur_mem_req_mi)}/{_mi(r.cur_mem_lim_mi)} → {_mi(r.rec_mem_mi)}/{_mi(r.rec_mem_lim_mi)} "
            f"| {r.status} | {r.reason} |")
    return "\n".join(out) + "\n"


def send_ntfy(url: str, token: str, title: str, summary: str, report_md: str, filename: str) -> None:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/krr", data=report_md.encode(), method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Title": title,
            "X-Filename": filename,
            # header values cannot hold newlines; ntfy expands a literal backslash-n
            "X-Message": summary.replace("\n", "\\n"),
        })
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"ntfy returned HTTP {resp.status}")


# ---------------------------------------------------------------- PR mode

REPO = "madchr1st/talos"
BRANCH = "krr/rightsizing"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def update_resources_block(text: str, cpu_m: int, mem_req_mi: int, mem_lim_mi: int) -> str:
    """Set spec.values.resources (TrueForge layout, 4-space indent) textually; keep everything else."""
    lines = text.split("\n")
    if "  values: {}" in lines:
        lines[lines.index("  values: {}")] = "  values:"
    if "  values:" not in lines:
        raise ValueError("no values block")
    v = lines.index("  values:")
    end_values = next((i for i in range(v + 1, len(lines))
                       if lines[i].strip() and _indent(lines[i]) <= 2), len(lines))
    start = next((i for i in range(v + 1, end_values) if lines[i] == "    resources:"), None)

    sections: dict[str, list[tuple[str, str]]] = {"limits": [], "requests": []}
    extra: list[str] = []
    if start is None:
        start = end = v + 1
    else:
        end = start + 1
        current = None
        while end < end_values and (not lines[end].strip() or _indent(lines[end]) > 4):
            line = lines[end]
            if not line.strip():
                break
            if _indent(line) == 6 and line.strip().endswith(":"):
                current = line.strip()[:-1]
                sections.setdefault(current, [])
            elif _indent(line) >= 8 and current is not None and ":" in line:
                key, _, value = line.strip().partition(":")
                sections[current].append((key, value.strip()))
            else:
                extra.append(line)
            end += 1

    want = {"limits": {"cpu": "0", "memory": fmt_mem(mem_lim_mi)},
            "requests": {"cpu": f"{cpu_m}m", "memory": fmt_mem(mem_req_mi)}}
    block = ["    resources:"]
    for name in ["limits", "requests"] + [n for n in sections if n not in ("limits", "requests")]:
        block.append(f"      {name}:")
        fixed = want.get(name, {})
        for key, value in fixed.items():
            block.append(f"        {key}: {value}")
        for key, value in sections.get(name, []):
            if key not in fixed:
                block.append(f"        {key}: {value}")
    block += extra
    return "\n".join(lines[:start] + block + lines[end:])


class GitHub:
    def __init__(self, token: str, repo: str = REPO):
        self.token, self.repo = token, repo
        self.paths: set[str] = set()

    def _req(self, method: str, path: str, body=None, allow_404: bool = False):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "krr-report"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"https://api.github.com{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if allow_404 and e.code == 404:
                return None
            raise RuntimeError(f"GitHub {method} {path}: HTTP {e.code} {e.read()[:300]!r}") from e
        return json.loads(raw) if raw else {}

    def find_helm_releases(self) -> dict[tuple[str, str], str]:
        tree = self._req("GET", f"/repos/{self.repo}/git/trees/main?recursive=1")
        self.paths = {t["path"] for t in tree["tree"] if t["type"] == "blob"}
        found = {}
        for path in sorted(p for p in self.paths if p.endswith("/helm-release.yaml")):
            text = self.get_text(path)
            meta = text.split("\nspec:")[0]
            name = next((l.split(":", 1)[1].strip() for l in meta.splitlines() if l.startswith("  name:")), None)
            ns = next((l.split(":", 1)[1].strip() for l in meta.splitlines() if l.startswith("  namespace:")), None)
            if name and ns:
                found[(name, ns)] = path
        return found

    def get_text(self, path: str) -> str:
        """Read a file on main via raw.githubusercontent.com (not counted against the API rate limit)."""
        url = f"https://raw.githubusercontent.com/{self.repo}/main/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "krr-report"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode()

    def open_or_update_pr(self, changes: dict[str, str], title: str, body: str) -> str:
        r = f"/repos/{self.repo}"
        main_sha = self._req("GET", f"{r}/git/ref/heads/main")["object"]["sha"]
        # One commit on top of main via the Git Data API; the branch jumps straight to it. Resetting the branch
        # to main first would leave the PR without commits for a moment, and GitHub closes such PRs.
        base_tree = self._req("GET", f"{r}/git/commits/{main_sha}")["tree"]["sha"]
        tree = self._req("POST", f"{r}/git/trees", {"base_tree": base_tree, "tree": [
            {"path": path, "mode": "100644", "type": "blob", "content": text}
            for path, text in sorted(changes.items())]})["sha"]
        commit = self._req("POST", f"{r}/git/commits", {
            "message": title, "tree": tree, "parents": [main_sha]})["sha"]
        if self._req("GET", f"{r}/git/ref/heads/{BRANCH}", allow_404=True):
            self._req("PATCH", f"{r}/git/refs/heads/{BRANCH}", {"sha": commit, "force": True})
        else:
            self._req("POST", f"{r}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit})
        owner = self.repo.split("/")[0]
        prs = self._req("GET", f"{r}/pulls?state=open&head={owner}:{BRANCH}")
        if prs:
            number, url = prs[0]["number"], prs[0]["html_url"]
            self._req("PATCH", f"{r}/pulls/{number}", {"title": title, "body": body})
        else:
            pr = self._req("POST", f"{r}/pulls", {"title": title, "head": BRANCH, "base": "main", "body": body})
            number, url = pr["number"], pr["html_url"]
        self._req("POST", f"{r}/issues/{number}/labels", {"labels": ["krr"]})
        return url


def plan_pr(rows: list[Row], gh) -> tuple[dict[str, str], list[Row]]:
    releases = gh.find_helm_releases()
    changes = {}
    for row in rows:
        if row.status != "change":
            continue
        path = releases.get((row.release, row.namespace))
        if path is None:
            row.status, row.reason = "manual", "HelmRelease file not found"
            continue
        text = gh.get_text(path)
        if "name: truecharts" not in text:
            row.status, row.reason = "manual", "not a TrueForge chart"
            continue
        sec = path.rsplit("/", 1)[0] + "/values-sec.yaml"
        if sec in gh.paths and any(l.startswith("resources:") for l in gh.get_text(sec).splitlines()):
            row.status, row.reason = "manual", "resources in values-sec.yaml"
            continue
        try:
            changes[path] = update_resources_block(text, row.rec_cpu_m, row.rec_mem_mi, row.rec_mem_lim_mi)
        except ValueError as e:
            row.status, row.reason = "manual", str(e)
    return changes, rows


def run_pr_mode(rows: list[Row], gh, mode: str, today: datetime.date) -> str | None:
    changes, rows = plan_pr(rows, gh)
    if mode == "dry-run":
        for path, new in sorted(changes.items()):
            old = gh.get_text(path)
            print("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}")))
        return None
    if not changes:
        return None
    body = "Automated by the krr CronJob (system/krr). Review before merging; not auto-merged.\n\n"
    body += TABLE_HEADER + "\n" + "\n".join(
        f"| {r.namespace} | {r.workload} | {r.container} | {_m(r.cur_cpu_req_m)} → {_m(r.rec_cpu_m)} "
        f"| {_mi(r.cur_mem_req_mi)}/{_mi(r.cur_mem_lim_mi)} → {_mi(r.rec_mem_mi)}/{_mi(r.rec_mem_lim_mi)} "
        f"| {r.status} | {r.reason} |"
        for r in rows if r.status == "change")
    return gh.open_or_update_pr(changes, f"chore(resources): KRR right-sizing {today.isoformat()}", body)


def main() -> int:
    config = load_config(os.environ.get("KRR_CONFIG", "/scripts/krr-config.json"))
    with open(os.environ.get("KRR_JSON", "/work/krr.json")) as f:
        rows = build_rows(json.load(f), config)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    report = render_markdown(rows, today, config)
    print(report, flush=True)

    failed = False
    try:
        summary = render_summary(rows, today, config)
        send_ntfy(os.environ["NTFY_URL"], os.environ["NTFY_TOKEN"], summary.splitlines()[0],
                  "\n".join(summary.splitlines()[1:]), report, f"krr-report-{today.isoformat()}.md")
    except Exception as e:  # report already printed; keep going so PR mode still runs
        print(f"ERROR: ntfy delivery failed: {e}", file=sys.stderr)
        failed = True

    mode = os.environ.get("CREATE_PR", "false")
    if mode in ("dry-run", "true"):
        try:
            token = os.environ.get("GITHUB_TOKEN", "")
            if mode == "true" and not token:
                raise RuntimeError("CREATE_PR=true needs GITHUB_TOKEN in the krr secret")
            url = run_pr_mode(rows, GitHub(token), mode, today)
            if url:
                print(f"PR: {url}")
        except Exception as e:
            print(f"ERROR: PR mode failed: {e}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
