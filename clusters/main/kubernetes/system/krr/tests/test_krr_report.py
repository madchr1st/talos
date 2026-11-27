"""Tests for app/krr-report.py. Run: cd clusters/main/kubernetes/system/krr && python3 -B -m unittest discover -s tests -v"""

import sys

sys.dont_write_bytecode = True  # no __pycache__ in app/: clustertool genconfig would add it to the kustomization

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


def rows(*scans, floors=None, cpu_max_m=None):
    config = {"floors": floors or {}, "full_history_from": "2026-12-05"}
    if cpu_max_m:
        config["cpu_request_max_m"] = cpu_max_m
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
        self.assertEqual(config["full_history_from"], "2026-12-05")
        self.assertEqual(config["cpu_request_max_m"], 1000)


class BuildRows(unittest.TestCase):
    def test_floor_applies(self):
        plex = {"helm.toolkit.fluxcd.io/name": "plex", "helm.toolkit.fluxcd.io/namespace": "plex"}
        (r,) = rows(scan("plex", "plex", "plex", plex, cur_mem=1024 * MI, rec_mem=2.2 * 1024 * MI),
                    floors={"plex/plex": "6Gi"})
        self.assertEqual(r.rec_mem_mi, 2256)  # request stays at the peak
        self.assertEqual(r.rec_mem_lim_mi, 6144)  # floor only raises the limit
        self.assertTrue(r.floored)
        self.assertEqual((r.status, r.reason), ("change", "floor"))

    def test_floor_and_threshold(self):
        plex = {"helm.toolkit.fluxcd.io/name": "plex", "helm.toolkit.fluxcd.io/namespace": "plex"}
        floors = {"plex/plex": "6Gi"}
        (r,) = rows(scan("plex", "plex", "plex", plex, cur_cpu=0.08, cur_mem=6000 * MI, cur_mem_lim=6000 * MI,
                         rec_mem=5900 * MI), floors=floors)
        self.assertEqual((r.rec_mem_mi, r.rec_mem_lim_mi), (5904, 6144))
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

    def test_cpu_cap(self):
        (r,) = rows(scan(rec_cpu=5.5), cpu_max_m=1000)
        self.assertEqual(r.rec_cpu_m, 1000)
        self.assertTrue(r.capped)
        self.assertEqual(r.reason, "cpu cap")

    def test_no_floor_limit_equals_request(self):
        (r,) = rows(scan(rec_mem=210 * MI))
        self.assertEqual((r.rec_mem_mi, r.rec_mem_lim_mi), (224, 224))

    def test_secondary_workload_same_container_name(self):
        immich = {"helm.toolkit.fluxcd.io/name": "immich", "helm.toolkit.fluxcd.io/namespace": "immich"}
        result = rows(scan("immich", "immich", "immich", immich),
                      scan("immich", "immich-microservices", "immich", immich), floors={"immich/immich": "2Gi"})
        main, micro = result
        self.assertEqual(main.workload, "immich")
        self.assertNotEqual(main.status, "manual")
        self.assertEqual((micro.status, micro.reason), ("manual", "sidecar/other container"))
        self.assertFalse(micro.floored)

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


CONFIG = {"floors": {}, "full_history_from": "2026-12-05"}


class Report(unittest.TestCase):
    def test_summary_partial_flag(self):
        r = rows(scan())
        first = kr.render_summary(r, datetime.date(2026, 11, 30), CONFIG).splitlines()[0]
        self.assertEqual(first, "KRR report 2026-11-30 (partial history)")
        first = kr.render_summary(r, datetime.date(2026, 12, 5), CONFIG).splitlines()[0]
        self.assertEqual(first, "KRR report 2026-12-05")

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
        self.assertIn("| sonarr | sonarr | sonarr | 75m → 20m | 200Mi/2400Mi → 224Mi/224Mi | change |", md)


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


def strip_resources(text):
    """Lines of a helm-release.yaml without the values.resources block."""
    out, skip = [], False
    for line in text.splitlines():
        if line == "    resources:":
            skip = True
            continue
        if skip and (line.startswith("      ") or not line.strip()):
            continue
        skip = False
        out.append(line)
    return out


class UpdateResources(unittest.TestCase):
    gpu = (FIXTURES / "helm-release-gpu.yaml").read_text()
    plain = (FIXTURES / "helm-release-noresources.yaml").read_text()

    def test_update_keeps_gpu_and_rest(self):
        new = kr.update_resources_block(self.gpu, 150, 1024, 6144)
        self.assertIn("    resources:\n"
                      "      limits:\n"
                      "        cpu: 0\n"
                      "        memory: 6Gi\n"
                      "        nvidia.com/gpu: 1\n"
                      "      requests:\n"
                      "        cpu: 150m\n"
                      "        memory: 1Gi\n", new)
        self.assertEqual(strip_resources(new), strip_resources(self.gpu))

    def test_update_inserts_block(self):
        new = kr.update_resources_block(self.plain, 20, 224, 224)
        lines = new.splitlines()
        self.assertEqual(lines.count("    resources:"), 1)
        i = lines.index("  values:")
        self.assertEqual(lines[i + 1:i + 8], ["    resources:", "      limits:", "        cpu: 0",
                                              "        memory: 224Mi", "      requests:", "        cpu: 20m",
                                              "        memory: 224Mi"])
        self.assertEqual(strip_resources(new), strip_resources(self.plain))
        self.assertIn("{DOMAIN_0}", new)

    def test_update_empty_values(self):
        text = self.plain.replace("  values:\n", "  values: {}\n  removed:\n", 1)
        new = kr.update_resources_block(text, 20, 224, 224)
        self.assertIn("  values:\n    resources:\n      limits:\n", new)
        self.assertNotIn("values: {}", new)

    def test_update_without_values_raises(self):
        with self.assertRaises(ValueError):
            kr.update_resources_block("spec:\n  chart: {}\n", 20, 224, 224)

    def test_update_idempotent(self):
        once = kr.update_resources_block(self.gpu, 150, 1024, 6144)
        self.assertEqual(kr.update_resources_block(once, 150, 1024, 6144), once)


class FakeGitHub:
    def __init__(self, files):
        self.files = files
        self.paths = set(files)

    def find_helm_releases(self):
        path = "apps/sonarr/app/helm-release.yaml"
        return {("sonarr", "sonarr"): path} if path in self.files else {}

    def get_text(self, path):
        return self.files[path]


class PlanPr(unittest.TestCase):
    plain = (FIXTURES / "helm-release-noresources.yaml").read_text()

    def test_plan_pr_changes_truecharts(self):
        gh = FakeGitHub({"apps/sonarr/app/helm-release.yaml": self.plain})
        changes, result = kr.plan_pr(rows(scan(rec_cpu=0.02)), gh)
        self.assertEqual(list(changes), ["apps/sonarr/app/helm-release.yaml"])
        self.assertIn("        cpu: 20m", changes["apps/sonarr/app/helm-release.yaml"])
        self.assertEqual(result[0].status, "change")

    def test_plan_pr_skips_non_truecharts(self):
        other = self.plain.replace("name: truecharts", "name: prometheus-community")
        gh = FakeGitHub({"apps/sonarr/app/helm-release.yaml": other})
        changes, result = kr.plan_pr(rows(scan(rec_cpu=0.02)), gh)
        self.assertEqual(changes, {})
        self.assertEqual((result[0].status, result[0].reason), ("manual", "not a TrueForge chart"))

    def test_plan_pr_skips_resources_in_values_sec(self):
        gh = FakeGitHub({"apps/sonarr/app/helm-release.yaml": self.plain,
                         "apps/sonarr/app/values-sec.yaml": "resources:\n    limits: ENC[...]\n"})
        changes, result = kr.plan_pr(rows(scan(rec_cpu=0.02)), gh)
        self.assertEqual(changes, {})
        self.assertEqual(result[0].reason, "resources in values-sec.yaml")

    def test_plan_pr_no_values_block(self):
        text = "\n".join(l for l in self.plain.splitlines() if l != "  values:")
        gh = FakeGitHub({"apps/sonarr/app/helm-release.yaml": text})
        changes, result = kr.plan_pr(rows(scan(rec_cpu=0.02)), gh)
        self.assertEqual((changes, result[0].reason), ({}, "no values block"))

    def test_plan_pr_missing_file(self):
        gh = FakeGitHub({})
        changes, result = kr.plan_pr(rows(scan(rec_cpu=0.02)), gh)
        self.assertEqual((changes, result[0].status), ({}, "manual"))


class RecordingGitHub(kr.GitHub):
    def __init__(self, responses):
        super().__init__("tk")
        self.calls, self.bodies, self.responses = [], [], responses

    def _req(self, method, path, body=None, allow_404=False):
        self.calls.append((method, path.split("?")[0]))
        self.bodies.append((method, path.split("?")[0], body))
        for (m, prefix), resp in self.responses.items():
            if m == method and path.startswith(prefix):
                return resp
        return {}


GIT_DATA = {
    ("GET", "/repos/madchr1st/talos/git/commits/abc"): {"tree": {"sha": "maintree"}},
    ("POST", "/repos/madchr1st/talos/git/trees"): {"sha": "newtree"},
    ("POST", "/repos/madchr1st/talos/git/commits"): {"sha": "newcommit"},
}


class GitHubApi(unittest.TestCase):
    def test_get_text_uses_raw_not_api(self):
        seen = []

        class Raw(FakeResponse):
            def read(self):
                return b"hello"
        with mock.patch.object(kr.urllib.request, "urlopen", lambda req, timeout=None: seen.append(req) or Raw()):
            self.assertEqual(kr.GitHub("").get_text("a/b.yaml"), "hello")
        self.assertEqual(seen[0].full_url, "https://raw.githubusercontent.com/madchr1st/talos/main/a/b.yaml")

    def test_open_or_update_pr_existing(self):
        gh = RecordingGitHub({
            ("GET", "/repos/madchr1st/talos/git/ref/heads/main"): {"object": {"sha": "abc"}},
            ("GET", "/repos/madchr1st/talos/git/ref/heads/krr/rightsizing"): {"object": {"sha": "old"}},
            ("GET", "/repos/madchr1st/talos/pulls"): [{"number": 7, "html_url": "https://pr/7"}],
            **GIT_DATA,
        })
        url = gh.open_or_update_pr({"a/helm-release.yaml": "x"}, "title", "body")
        self.assertEqual(url, "https://pr/7")
        self.assertNotIn(("POST", "/repos/madchr1st/talos/pulls"), gh.calls)
        self.assertIn(("PATCH", "/repos/madchr1st/talos/pulls/7"), gh.calls)
        # the branch jumps straight to the new commit, never to main (GitHub would close the PR)
        (ref,) = [b for m, p, b in gh.bodies if p == "/repos/madchr1st/talos/git/refs/heads/krr/rightsizing"
                  and m == "PATCH"]
        self.assertEqual(ref, {"sha": "newcommit", "force": True})
        (commit,) = [b for m, p, b in gh.bodies if (m, p) == ("POST", "/repos/madchr1st/talos/git/commits")]
        self.assertEqual((commit["parents"], commit["tree"]), (["abc"], "newtree"))
        (tree,) = [b for m, p, b in gh.bodies if (m, p) == ("POST", "/repos/madchr1st/talos/git/trees")]
        self.assertEqual(tree["base_tree"], "maintree")
        self.assertEqual(tree["tree"], [{"path": "a/helm-release.yaml", "mode": "100644", "type": "blob",
                                         "content": "x"}])
        self.assertNotIn("PUT", {m for m, _ in gh.calls})

    def test_open_or_update_pr_new(self):
        gh = RecordingGitHub({
            ("GET", "/repos/madchr1st/talos/git/ref/heads/main"): {"object": {"sha": "abc"}},
            ("GET", "/repos/madchr1st/talos/git/ref/heads/krr/rightsizing"): None,
            ("GET", "/repos/madchr1st/talos/pulls"): [],
            ("POST", "/repos/madchr1st/talos/pulls"): {"number": 8, "html_url": "https://pr/8"},
            **GIT_DATA,
        })
        self.assertEqual(gh.open_or_update_pr({"a/helm-release.yaml": "x"}, "t", "b"), "https://pr/8")
        (ref,) = [b for m, p, b in gh.bodies if (m, p) == ("POST", "/repos/madchr1st/talos/git/refs")]
        self.assertEqual(ref, {"ref": "refs/heads/krr/rightsizing", "sha": "newcommit"})
        self.assertIn(("POST", "/repos/madchr1st/talos/issues/8/labels"), gh.calls)

    def test_dry_run_no_writes(self):
        plain = (FIXTURES / "helm-release-noresources.yaml").read_text()
        gh = RecordingGitHub({})
        gh.find_helm_releases = lambda: {("sonarr", "sonarr"): "apps/sonarr/app/helm-release.yaml"}
        gh.paths = {"apps/sonarr/app/helm-release.yaml"}
        gh.get_text = lambda path: plain
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kr.run_pr_mode(rows(scan(rec_cpu=0.02)), gh, "dry-run", datetime.date(2026, 12, 2))
        self.assertEqual({m for m, _ in gh.calls} - {"GET"}, set())
        self.assertIn("+        cpu: 20m", out.getvalue())

    def test_main_true_without_token(self):
        with tempfile.TemporaryDirectory() as d:
            krr_json = pathlib.Path(d, "krr.json")
            krr_json.write_text(json.dumps({"scans": [scan()]}))
            env = {"KRR_JSON": str(krr_json), "KRR_CONFIG": str(APP / "krr-config.json"),
                   "NTFY_URL": "http://ntfy", "NTFY_TOKEN": "tk", "CREATE_PR": "true", "GITHUB_TOKEN": ""}
            err = io.StringIO()
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(kr.urllib.request, "urlopen", lambda req, timeout=None: FakeResponse()), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = kr.main()
        self.assertEqual(code, 1)
        self.assertIn("GITHUB_TOKEN", err.getvalue())


if __name__ == "__main__":
    unittest.main()
