"""Tests for app/krr-report.py. Run: cd clusters/main/kubernetes/system/krr && python3 -m unittest discover -s tests -v"""

import importlib.util
import contextlib
import datetime
import io
import json
import os
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / "app"
FIXTURES = HERE / "fixtures"

_spec = importlib.util.spec_from_file_location("krr_report", APP / "krr-report.py")
kr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kr)

MI = 2**20
FLUX = {"helm.toolkit.fluxcd.io/name": "sonarr", "helm.toolkit.fluxcd.io/namespace": "sonarr"}


def scan(ns="sonarr", name="sonarr", container="sonarr", labels=FLUX, cur_cpu=0.075, cur_mem=200 * MI,
         cur_cpu_lim=1.5, cur_mem_lim=2400 * MI, rec_cpu=0.08, rec_mem=210 * MI, kind="Deployment"):
    return {
        "object": {
            "namespace": ns, "name": name, "container": container, "kind": kind, "labels": labels,
            "allocations": {
                "requests": {"cpu": cur_cpu, "memory": cur_mem},
                "limits": {"cpu": cur_cpu_lim, "memory": cur_mem_lim},
            },
        },
        "recommended": {
            "requests": {"cpu": {"value": rec_cpu, "severity": "OK"}, "memory": {"value": rec_mem, "severity": "OK"}},
            "limits": {"cpu": {"value": None, "severity": "OK"}, "memory": {"value": rec_mem, "severity": "OK"}},
        },
    }


def rows(*scans, floors=None):
    config = {"floors": floors or {}, "full_history_from": "2026-12-02"}
    return kr.build_rows({"scans": list(scans)}, config)


class Units(unittest.TestCase):
    def test_parse_mem_mi(self):
        self.assertEqual(kr.parse_mem_mi("6Gi"), 6144)
        self.assertEqual(kr.parse_mem_mi("512Mi"), 512)

    def test_rounding(self):
        self.assertEqual(kr.round_cpu_m(0.0121), 15)
        self.assertEqual(kr.round_cpu_m(0.010), 10)
        self.assertEqual(kr.round_mem_mi(100 * MI + 1), 112)
        self.assertEqual(kr.fmt_mem(6144), "6Gi")
        self.assertEqual(kr.fmt_mem(112), "112Mi")

    def test_load_config(self):
        config = kr.load_config(str(APP / "krr-config.json"))
        self.assertEqual(config["floors"]["plex/plex"], "6Gi")
        self.assertEqual(config["full_history_from"], "2026-12-02")


class BuildRows(unittest.TestCase):
    def test_floor_applies(self):
        plex = {"helm.toolkit.fluxcd.io/name": "plex", "helm.toolkit.fluxcd.io/namespace": "plex"}
        (r,) = rows(scan("plex", "plex", "plex", plex, cur_mem=1024 * MI, rec_mem=2.2 * 1024 * MI),
                    floors={"plex/plex": "6Gi"})
        self.assertEqual(r.rec_mem_mi, 6144)
        self.assertTrue(r.floored)
        self.assertEqual(r.status, "change")

    def test_floor_and_threshold(self):
        plex = {"helm.toolkit.fluxcd.io/name": "plex", "helm.toolkit.fluxcd.io/namespace": "plex"}
        floors = {"plex/plex": "6Gi"}
        (r,) = rows(scan("plex", "plex", "plex", plex, cur_cpu=0.08, cur_mem=6000 * MI, cur_mem_lim=6000 * MI,
                         rec_mem=5900 * MI), floors=floors)
        self.assertEqual(r.rec_mem_mi, 6144)
        self.assertEqual(r.status, "ok")
        (r,) = rows(scan("plex", "plex", "plex", plex, cur_cpu=0.08, cur_mem=4096 * MI, rec_mem=5900 * MI),
                    floors=floors)
        self.assertEqual(r.status, "change")

    def test_threshold(self):
        def status(cur_cpu, cur_mem, rec_cpu, rec_mem):
            (r,) = rows(scan(cur_cpu=cur_cpu, cur_mem=cur_mem, cur_mem_lim=cur_mem, rec_cpu=rec_cpu, rec_mem=rec_mem))
            return r.status
        self.assertEqual(status(0.075, 200 * MI, 0.08, 210 * MI), "ok")
        self.assertEqual(status(0.075, 200 * MI, 0.02, 200 * MI), "change")
        self.assertEqual(status(0.1, 200 * MI, 0.115, 200 * MI), "ok")
        self.assertEqual(status(0.02, 200 * MI, 0.028, 200 * MI), "ok")

    def test_memory_limit_mismatch_is_change(self):
        # request fits, but the limit (TrueForge default 2400Mi) should become = request
        (r,) = rows(scan(cur_cpu=0.08, cur_mem=208 * MI, cur_mem_lim=2400 * MI, rec_cpu=0.08, rec_mem=200 * MI))
        self.assertEqual(r.status, "change")

    def test_missing_current_is_change(self):
        s = scan()
        s["object"]["allocations"] = {"requests": {}, "limits": {}}
        (r,) = rows(s)
        self.assertEqual(r.status, "change")
        self.assertIsNone(r.cur_cpu_req_m)

    def test_unknown_values(self):
        (r,) = rows(scan(rec_cpu="?"))
        self.assertEqual(r.status, "manual")
        self.assertEqual(r.reason, "no data")
        (r,) = rows(scan(rec_mem=None))
        self.assertEqual(r.reason, "no data")

    def test_sidecar_and_unmanaged(self):
        side, = rows(scan(name="sonarr", container="sonarr-exportarr"))
        self.assertEqual((side.status, side.reason), ("manual", "sidecar/other container"))
        (bare,) = rows(scan("kube-system", "coredns", "coredns", labels={"k8s-app": "kube-dns"}))
        self.assertEqual((bare.status, bare.reason), ("manual", "not Flux-managed"))
        (none_labels,) = rows(scan("kube-system", "coredns", "coredns", labels=None))
        self.assertEqual(none_labels.reason, "not Flux-managed")
        (main,) = rows(scan())
        self.assertEqual(main.release, "sonarr")

    def test_rows_sorted(self):
        result = rows(scan("b", "x", "x"), scan("a", "y", "z"), scan("a", "y", "a"))
        self.assertEqual([(r.namespace, r.container) for r in result], [("a", "a"), ("a", "z"), ("b", "x")])

    @unittest.skipUnless((FIXTURES / "krr-sample.json").exists(), "fixture not captured yet")
    def test_fixture_loads(self):
        krr = json.loads((FIXTURES / "krr-sample.json").read_text())
        result = kr.build_rows(krr, kr.load_config(str(APP / "krr-config.json")))
        self.assertTrue(result)
        self.assertTrue({r.status for r in result} <= {"change", "ok", "manual"})


CONFIG = {"floors": {}, "full_history_from": "2026-12-02"}


class Report(unittest.TestCase):
    def test_summary_partial_flag(self):
        r = rows(scan())
        first = kr.render_summary(r, datetime.date(2026, 11, 30), CONFIG).splitlines()[0]
        self.assertEqual(first, "KRR report 2026-11-30 (partial history)")
        first = kr.render_summary(r, datetime.date(2026, 12, 2), CONFIG).splitlines()[0]
        self.assertEqual(first, "KRR report 2026-12-02")

    def test_summary_totals_and_counts(self):
        r = rows(scan(cur_mem_lim=200 * MI), scan("radarr", "radarr", "radarr",
                              {"helm.toolkit.fluxcd.io/name": "radarr", "helm.toolkit.fluxcd.io/namespace": "radarr"},
                              rec_cpu=0.02, rec_mem=100 * MI))
        text = kr.render_summary(r, datetime.date(2026, 12, 2), CONFIG)
        self.assertIn("CPU requests: 150m -> 100m", text)
        self.assertIn("change: 1, ok: 1, manual: 0", text)
        self.assertIn("radarr/radarr", text)

    def test_summary_size(self):
        many = [scan(f"ns{i:03}", "app", "app",
                     {"helm.toolkit.fluxcd.io/name": "app", "helm.toolkit.fluxcd.io/namespace": f"ns{i:03}"},
                     rec_cpu=0.5, rec_mem=4000 * MI) for i in range(200)]
        text = kr.render_summary(rows(*many), datetime.date(2026, 12, 2), CONFIG)
        self.assertLessEqual(len(text.encode()), 4000)

    def test_markdown_table(self):
        md = kr.render_markdown(rows(scan(rec_cpu=0.02)), datetime.date(2026, 12, 2), CONFIG)
        self.assertIn("| Namespace | Workload | Container | CPU req (cur → rec) | Mem req/limit (cur → rec) "
                      "| Status | Note |", md)
        self.assertIn("| sonarr | sonarr | sonarr | 75m → 20m | 200Mi/2400Mi → 224Mi | change |", md)


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


class Ntfy(unittest.TestCase):
    def test_send_ntfy_headers(self):
        seen = []
        with mock.patch.object(kr.urllib.request, "urlopen", lambda req, timeout=None: seen.append(req) or FakeResponse()):
            kr.send_ntfy("http://ntfy", "tk", "KRR report", "line1\nline2", "# report", "krr-report-2026-12-02.md")
        (req,) = seen
        self.assertEqual(req.get_method(), "PUT")
        self.assertTrue(req.full_url.endswith("/krr"))
        self.assertEqual(req.get_header("Authorization"), "Bearer tk")
        self.assertTrue(req.get_header("X-filename").endswith(".md"))
        self.assertNotIn("\n", req.get_header("X-message"))
        self.assertEqual(req.data, b"# report")

    def test_main_exits_1_when_ntfy_fails(self):
        def boom(req, timeout=None):
            raise urllib.error.URLError("refused")
        with tempfile.TemporaryDirectory() as d:
            krr_json = pathlib.Path(d, "krr.json")
            krr_json.write_text(json.dumps({"scans": [scan()]}))
            env = {"KRR_JSON": str(krr_json), "KRR_CONFIG": str(APP / "krr-config.json"),
                   "NTFY_URL": "http://ntfy", "NTFY_TOKEN": "tk", "CREATE_PR": "false"}
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), mock.patch.object(kr.urllib.request, "urlopen", boom), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = kr.main()
        self.assertEqual(code, 1)
        self.assertIn("| Namespace | Workload |", out.getvalue())


if __name__ == "__main__":
    unittest.main()
