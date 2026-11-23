#!/usr/bin/env python3
"""Turn KRR JSON into a right-sizing report (ntfy) and, optionally, a PR.

Standard library only; runs in a stock python image. Shipped as a ConfigMap with
Flux substitution disabled, but keep the dollar-brace sequence out of this file anyway.
See docs/superpowers/specs/2026-11-23-krr-rightsizing-design.md.
"""

import datetime
import json
import math
import os
import sys
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
    rec_mem_mi: int | None
    floored: bool
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
        rec_mem_mi = round_mem_mi(rec_mem) if rec_mem is not None else None
        floor = floors.get(f"{obj['namespace']}/{obj['container']}")
        floored = rec_mem_mi is not None and floor is not None and rec_mem_mi < floor
        if floored:
            rec_mem_mi = floor

        row = Row(
            namespace=obj["namespace"], workload=obj["name"], kind=obj.get("kind", ""), container=obj["container"],
            release=release,
            cur_cpu_req_m=_cur_m(req.get("cpu")), cur_cpu_lim_m=_cur_m(lim.get("cpu")),
            cur_mem_req_mi=_cur_mi(req.get("memory")), cur_mem_lim_mi=_cur_mi(lim.get("memory")),
            rec_cpu_m=rec_cpu_m, rec_mem_mi=rec_mem_mi, floored=floored, status="ok", reason="",
        )
        if release is None:
            row.status, row.reason = "manual", "not Flux-managed"
        elif row.container != release:
            row.status, row.reason = "manual", "sidecar/other container"
        elif rec_cpu_m is None or rec_mem_mi is None:
            row.status, row.reason = "manual", "no data"
        elif (_differs(row.cur_cpu_req_m, rec_cpu_m, THRESHOLD_CPU_M)
              or _differs(row.cur_mem_req_mi, rec_mem_mi, THRESHOLD_MEM_MI)
              or _differs(row.cur_mem_lim_mi, rec_mem_mi, THRESHOLD_MEM_MI)):
            row.status = "change"
        if floored and not row.reason:
            row.reason = "floor"
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
            f"| {_mi(r.cur_mem_req_mi)}/{_mi(r.cur_mem_lim_mi)} → {_mi(r.rec_mem_mi)} | {r.status} | {r.reason} |")
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
