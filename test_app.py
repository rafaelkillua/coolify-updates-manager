#!/usr/bin/env python3
"""Tests for app.py. Standard library only, same rule as the app itself.

    python3 -m unittest -v test_app        # or: python3 test_app.py

Nothing here talks to a real registry or a real Docker daemon: the docker API is
a local stub server and the registry is a fake object installed over
``app.registry_digest``/``app.registry_tags``. That keeps the tests runnable
without credentials, which is the mode the app must keep working in.
"""

import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

_TMP = tempfile.mkdtemp(prefix="updates-test-")
os.environ["UPDATES_STATE"] = os.path.join(_TMP, "state.json")
os.environ["COOLIFY_TOKEN"] = ""
os.environ["COOLIFY_API_URL"] = "http://127.0.0.1:9"
os.environ["UPDATES_IGNORE"] = r"^coolify(-|$),^ignoreme$"
os.environ["UPDATES_TIMEOUT"] = "5"

import app  # noqa: E402  (env must be set first: settings are read at import)


# ------------------------------------------------------------------ fixtures --
class FakeRegistry:
    """Registry stand-in for registry_digest/registry_tags."""

    def __init__(self):
        self.digests = {}   # (registry, repo, tag) -> digest | None
        self.tags = {}      # (registry, repo) -> [tag, ...]
        self.digest_calls = []

    def digest(self, registry, repo, tag):
        self.digest_calls.append((registry, repo, tag))
        return self.digests.get((registry, repo, tag))

    def tag_list(self, registry, repo, max_pages=40):
        return list(self.tags.get((registry, repo), []))


class FakeDocker:
    """Stub of the handful of Docker API endpoints app.py uses."""

    def __init__(self):
        self.containers = []
        self.inspects = {}
        self.images = {}
        self.pulls = []          # query strings of POST /images/create
        self.pull_status = 200
        self.pull_body = "{}\n"
        self.down = False
        self.server = None

    def reset(self):
        self.containers, self.inspects, self.images = [], {}, {}
        self.pulls = []
        self.pull_status, self.pull_body = 200, "{}\n"
        self.down = False

    def handler(self):
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if stub.down:
                    return self.send_error(500, "daemon down")
                path = urllib.parse.urlparse(self.path).path
                parts = path.strip("/").split("/")
                body = None
                if parts == ["containers", "json"]:
                    body = stub.containers
                elif len(parts) == 3 and parts[0] == "containers" and parts[2] == "json":
                    body = stub.inspects.get(parts[1])
                elif len(parts) == 3 and parts[0] == "images" and parts[2] == "json":
                    body = stub.images.get(parts[1])
                if body is None:
                    return self.send_error(404, "not found")
                return self._json(200, body)

            def do_POST(self):
                if stub.down:
                    return self.send_error(500, "daemon down")
                path = urllib.parse.urlparse(self.path).path
                if path != "/images/create":
                    return self.send_error(404, "not found")
                stub.pulls.append(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                data = stub.pull_body.encode()
                self.send_response(stub.pull_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    def start(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.url

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


DOCKER = FakeDocker()
REGISTRY = FakeRegistry()
_SAVED = {}


def setUpModule():
    _SAVED["url"] = app.DOCKER_URL
    app.DOCKER_URL = DOCKER.start()


def tearDownModule():
    app.DOCKER_URL = _SAVED["url"]
    DOCKER.stop()


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def install_registry():
    app.registry_digest = REGISTRY.digest
    app.registry_tags = REGISTRY.tag_list


def reset_app():
    """Fresh caches and state between tests, as a process restart would give."""
    install_registry()
    DOCKER.reset()
    REGISTRY.digests, REGISTRY.tags, REGISTRY.digest_calls = {}, {}, []
    for event in (app.CHECKING, app.RESOLVING, app.WARMING, app.JOB_POLLER):
        wait_for(lambda e=event: not e.is_set())
        event.clear()
    with app.LOCK:
        app.STATE.update(rows=[], counts={"UPDATE": 0, "ok": 0, "??": 0}, ignored=0,
                         error=None, pins={}, level="current", resolved_at={},
                         checked_at=None, checked_at_epoch=None, duration=None)
        app.DEPLOY_JOBS.clear()
        app.DEPLOY_LOG.clear()
        app._preview_cache.clear()
        app._versions_cache.clear()
    app._tags_cache.clear()
    app._digest_cache.clear()
    app._exact_cache.clear()
    app._resources_cache.clear()
    if os.path.exists(app.STATE_FILE):
        os.unlink(app.STATE_FILE)


# --------------------------------------------------------------- pure pieces --
class SplitRefTest(unittest.TestCase):
    def test_docker_hub_official_image(self):
        self.assertEqual(app.split_ref("nginx"), ("registry-1.docker.io", "library/nginx", "latest"))

    def test_docker_hub_with_tag(self):
        self.assertEqual(app.split_ref("postgres:16-alpine"),
                         ("registry-1.docker.io", "library/postgres", "16-alpine"))

    def test_docker_hub_user_repo(self):
        self.assertEqual(app.split_ref("grafana/grafana:11.1.0"),
                         ("registry-1.docker.io", "grafana/grafana", "11.1.0"))

    def test_other_registry_with_port(self):
        self.assertEqual(app.split_ref("localhost:5000/team/api:v2.1"),
                         ("localhost:5000", "team/api", "v2.1"))

    def test_ghcr(self):
        self.assertEqual(app.split_ref("ghcr.io/org/app:3.5.1"),
                         ("ghcr.io", "org/app", "3.5.1"))

    def test_digest_reference_has_no_tag(self):
        # a digest reference carries no tag at all: it falls back to latest and
        # docker_pull() refuses to pull it instead of guessing
        self.assertEqual(app.split_ref("nginx@sha256:" + "a" * 64),
                         ("registry-1.docker.io", "library/nginx", "latest"))

    def test_empty(self):
        self.assertEqual(app.split_ref(""), ("registry-1.docker.io", "library/", "latest"))


class TagPolicyTest(unittest.TestCase):
    def test_floating(self):
        for tag in ("", "latest", "stable", "release", "LATEST"):
            self.assertEqual(app.classify_tag(tag), ("floating", ""), tag)

    def test_digest(self):
        self.assertEqual(app.classify_tag("sha256:" + "a" * 64), ("digest", ""))

    def test_major(self):
        self.assertEqual(app.classify_tag("v2"), ("major", "2"))
        self.assertEqual(app.classify_tag("pg16"), ("major", ""))

    def test_minor(self):
        self.assertEqual(app.classify_tag("18.6-alpine"), ("minor", "18.6"))
        self.assertEqual(app.classify_tag("v3.7"), ("minor", "3.7"))
        self.assertEqual(app.classify_tag("1.5-stable"), ("minor", "1.5"))

    def test_exact(self):
        self.assertEqual(app.classify_tag("4.5.3"), ("exact", "4.5.3"))
        self.assertEqual(app.classify_tag("12.4.20261026-211555"), ("exact", "12.4.20261026"))

    def test_custom(self):
        self.assertEqual(app.classify_tag("alpine"), ("custom", ""))
        self.assertEqual(app.classify_tag("slim-bookworm"), ("custom", ""))

    def test_parse_version(self):
        self.assertEqual(app.parse_version("v3.3.0"), "3.3.0")
        self.assertEqual(app.parse_version("V3.3"), "3.3")
        self.assertIsNone(app.parse_version("latest"))
        self.assertIsNone(app.parse_version(None))


class VersionKeyTest(unittest.TestCase):
    def test_numeric_not_lexicographic(self):
        tags = ["3.9.0", "3.10.0", "3.2.1"]
        self.assertEqual(sorted(tags, key=app._version_key), ["3.2.1", "3.9.0", "3.10.0"])

    def test_linuxserver_suffix(self):
        tags = ["1.2.3-ls10", "1.2.3-ls9"]
        self.assertEqual(sorted(tags, key=app._linuxserver_key), ["1.2.3-ls9", "1.2.3-ls10"])


# --------------------------------------------------------------- registry --
class SuggestPinTest(unittest.TestCase):
    def setUp(self):
        reset_app()

    def test_floating_tag_resolves_to_versioned_tag(self):
        REGISTRY.digests[("ghcr.io", "org/app", "latest")] = "sha256:hot"
        REGISTRY.digests[("ghcr.io", "org/app", "3.5.1")] = "sha256:hot"
        REGISTRY.digests[("ghcr.io", "org/app", "3.4.0")] = "sha256:old"
        REGISTRY.tags[("ghcr.io", "org/app")] = [
            "latest", "3.5.1", "3.4.0", "12.4.20261026-211555", "3.6.0-rc1",
        ]
        result = app.suggest_pin("ghcr.io/org/app:latest")
        self.assertEqual(result["digest"], "sha256:hot")
        self.assertEqual(result["matches"][0], "3.5.1")
        # prerelease and dateish tags must not win a probe slot
        self.assertNotIn("3.6.0-rc1", result["matches"])
        self.assertNotIn("12.4.20261026-211555", result["matches"])

    def test_linuxserver_style(self):
        REGISTRY.digests[("registry-1.docker.io", "linuxserver/sonarr", "latest")] = "sha256:lsio"
        REGISTRY.digests[("registry-1.docker.io", "linuxserver/sonarr", "4.0.9.2244-ls239")] = "sha256:lsio"
        REGISTRY.tags[("registry-1.docker.io", "linuxserver/sonarr")] = [
            "latest", "4.0.9.2244-ls239", "4.0.8.1900-ls230",
        ]
        result = app.suggest_pin("linuxserver/sonarr:latest")
        self.assertEqual(result["matches"][0], "4.0.9.2244-ls239")

    def test_missing_tag_reports_error(self):
        result = app.suggest_pin("ghcr.io/org/app:latest")
        self.assertEqual(result["matches"], [])
        self.assertIn("error", result)

    def test_exact_tag_for_digest(self):
        REGISTRY.digests[("ghcr.io", "org/app", "3.5.1")] = "sha256:x"
        REGISTRY.tags[("ghcr.io", "org/app")] = ["3.5.1", "3.4.0"]
        self.assertEqual(app.exact_tag_for_digest("ghcr.io", "org/app", "sha256:x"), "3.5.1")
        self.assertIsNone(app.exact_tag_for_digest("ghcr.io", "org/app", "sha256:none"))


class VersionColumnsTest(unittest.TestCase):
    def setUp(self):
        reset_app()

    def test_running_from_image_label(self):
        row = {"image": "ghcr.io/org/app:latest", "tag": "latest", "version": "3.3.0", "digests": []}
        self.assertEqual(app.version_running(row), "3.3.0")

    def test_running_from_tag_text(self):
        row = {"image": "ghcr.io/org/app:v3.3", "tag": "v3.3", "version": "", "digests": []}
        self.assertEqual(app.version_running(row), "3.3")

    def test_running_from_digest_lookup(self):
        REGISTRY.digests[("ghcr.io", "org/app", "3.5.1")] = "sha256:run"
        REGISTRY.tags[("ghcr.io", "org/app")] = ["3.5.1"]
        row = {"image": "ghcr.io/org/app:latest", "tag": "latest", "version": "",
               "digests": ["sha256:run"]}
        self.assertEqual(app.version_running(row), "3.5.1")

    def test_running_unknown_stays_empty(self):
        row = {"image": "ghcr.io/org/app:latest", "tag": "latest", "version": "", "digests": []}
        self.assertEqual(app.version_running(row), "")

    def test_next_version_uses_newest_release(self):
        row = {"image": "ghcr.io/org/app:latest", "tag": "latest", "version": "", "digests": []}
        with app.LOCK:
            app.STATE["pins"][row["image"]] = {"matches": ["v3.5.1"]}
        self.assertEqual(app.version_after_pull(row), "3.5.1")

    def test_version_facts_are_cached(self):
        row = {"image": "ghcr.io/org/app:latest", "tag": "latest", "version": "3.5.1", "digests": []}
        with app.LOCK:
            app.STATE["pins"][row["image"]] = {"matches": ["3.5.1"]}
        first = app.version_facts(row)
        calls = len(REGISTRY.digest_calls)
        second = app.version_facts(row)
        self.assertEqual(first, second)
        self.assertEqual(len(REGISTRY.digest_calls), calls)


class ResolveLevelTest(unittest.TestCase):
    def setUp(self):
        reset_app()

    def row(self, image, tag, digests, version=""):
        return {"image": image, "tag": tag, "digests": digests, "version": version, "status": "ok"}

    def test_current_has_no_target(self):
        self.assertIsNone(app.resolve_level(self.row("ghcr.io/org/app:3.7.2", "3.7.2", []), "current"))

    def test_minor_target_ok_when_digest_matches(self):
        row = self.row("ghcr.io/org/app:3.7.2", "3.7.2", ["sha256:line"])
        with app.LOCK:
            app.STATE["pins"][row["image"]] = {"matches": ["3.7.2"]}
        REGISTRY.tags[("ghcr.io", "org/app")] = ["3", "3.7", "3.7.2"]
        REGISTRY.digests[("ghcr.io", "org/app", "3.7")] = "sha256:line"
        target = app.resolve_level(row, "minor")
        self.assertEqual((target["target_tag"], target["status"]), ("3.7", "ok"))

    def test_minor_target_is_update_when_running_older_line(self):
        row = self.row("ghcr.io/org/app:3.6.4", "3.6.4", ["sha256:old-line"])
        with app.LOCK:
            app.STATE["pins"][row["image"]] = {"matches": ["3.7.2"]}
        REGISTRY.tags[("ghcr.io", "org/app")] = ["3.7", "3.7.2"]
        REGISTRY.digests[("ghcr.io", "org/app", "3.7")] = "sha256:new-line"
        target = app.resolve_level(row, "minor")
        self.assertEqual((target["target_tag"], target["status"]), ("3.7", "UPDATE"))

    def test_major_uses_bare_major_tag(self):
        row = self.row("ghcr.io/org/app:3.7.2", "3.7.2", ["sha256:x"])
        with app.LOCK:
            app.STATE["pins"][row["image"]] = {"matches": ["3.7.2"]}
        REGISTRY.tags[("ghcr.io", "org/app")] = ["3", "3.7"]
        REGISTRY.digests[("ghcr.io", "org/app", "3")] = "sha256:x"
        self.assertEqual(app.resolve_level(row, "major")["target_tag"], "3")

    def test_suffix_of_the_reference_is_preserved(self):
        row = self.row("registry-1.docker.io/library/postgres:16.4-alpine", "16.4-alpine",
                       ["sha256:pg"])
        with app.LOCK:
            app.STATE["pins"][row["image"]] = {"matches": ["16.4-alpine"]}
        REGISTRY.tags[("registry-1.docker.io", "library/postgres")] = ["16", "16-alpine", "16.4-alpine"]
        REGISTRY.digests[("registry-1.docker.io", "library/postgres", "16.4-alpine")] = "sha256:pg"
        target = app.resolve_level(row, "minor")
        self.assertEqual(target["target_tag"], "16.4-alpine")
        self.assertEqual(target["status"], "ok")

    def test_suffix_falls_back_to_the_line_tag_when_the_patch_tag_is_missing(self):
        row = self.row("registry-1.docker.io/library/postgres:16.4-alpine", "16.4-alpine",
                       ["sha256:line"])
        with app.LOCK:
            app.STATE["pins"][row["image"]] = {"matches": ["16.4-alpine"]}
        REGISTRY.tags[("registry-1.docker.io", "library/postgres")] = ["16-alpine"]
        REGISTRY.digests[("registry-1.docker.io", "library/postgres", "16-alpine")] = "sha256:line"
        target = app.resolve_level(row, "minor")
        self.assertEqual(target["target_tag"], "16.4-alpine")
        self.assertIn("exact instead", target["note"])

    def test_falls_back_to_exact_tag_when_line_is_not_published(self):
        row = self.row("ghcr.io/org/app:3.7.2", "3.7.2", ["sha256:x"])
        with app.LOCK:
            app.STATE["pins"][row["image"]] = {"matches": ["3.7.2"]}
        REGISTRY.tags[("ghcr.io", "org/app")] = ["3.7.2"]
        REGISTRY.digests[("ghcr.io", "org/app", "3.7.2")] = "sha256:x"
        target = app.resolve_level(row, "minor")
        self.assertEqual(target["target_tag"], "3.7.2")
        self.assertIn("exact instead", target["note"])

    def test_unknown_version_degrades_to_note(self):
        row = {"image": "ghcr.io/org/app:latest", "tag": "latest", "digests": [], "status": "??"}
        with app.LOCK:
            app.STATE["pins"][row["image"]] = {"matches": []}
        target = app.resolve_level(row, "minor")
        self.assertEqual(target["note"], "version unknown")


# ------------------------------------------------------------------- docker --
class DockerApiTest(unittest.TestCase):
    def setUp(self):
        reset_app()

    def test_get_reads_a_resource(self):
        DOCKER.containers = [{"Id": "abc", "Names": ["/one"]}]
        self.assertEqual(app.docker_get("/containers/json?all=1"), DOCKER.containers)

    def test_errors_raise(self):
        with self.assertRaises(RuntimeError):
            app.docker_get("/images/nope/json")

    def test_pull_translates_tag_into_query(self):
        ok, message = app.docker_pull("ghcr.io/org/app:3.5.1")
        self.assertTrue(ok, message)
        self.assertEqual(DOCKER.pulls[-1], {"fromImage": ["ghcr.io/org/app"], "tag": ["3.5.1"]})

    def test_pull_uses_docker_hub_name_for_official_images(self):
        app.docker_pull("nginx:1.27")
        self.assertEqual(DOCKER.pulls[-1], {"fromImage": ["library/nginx"], "tag": ["1.27"]})

    def test_digest_pinned_images_are_skipped(self):
        ok, message = app.docker_pull("ghcr.io/org/app@sha256:" + "a" * 64)
        self.assertTrue(ok)
        self.assertEqual(message, "digest pinned, skipped")
        self.assertEqual(DOCKER.pulls, [])

    def test_proxy_denial_is_reported_clearly(self):
        DOCKER.pull_status, DOCKER.pull_body = 403, '{"message":"denied"}'
        ok, message = app.docker_pull("ghcr.io/org/app:latest")
        self.assertFalse(ok)
        self.assertIn("POST /images/create", message)

    def test_pull_errors_inside_the_stream_are_detected(self):
        DOCKER.pull_body = '{"error":"manifest unknown"}\n'
        ok, message = app.docker_pull("ghcr.io/org/app:9.9.9")
        self.assertFalse(ok)
        self.assertIn("manifest unknown", message)


def docker_fixture():
    """Two container shapes plus two that must be hidden by UPDATES_IGNORE."""
    DOCKER.containers = [
        {"Id": "c1", "Names": ["/app-web-1"], "Image": "ghcr.io/org/site:1.4.0",
         "Labels": {"com.docker.compose.project": "proj1", "coolify.resourceName": "web"}},
        {"Id": "c2", "Names": ["/db-postgres-1"], "Image": "sha256:" + "b" * 64,
         "Labels": {"com.docker.compose.project": "proj1"}},
        {"Id": "c3", "Names": ["/coolify-proxy"], "Image": "traefik:v3.1", "Labels": {}},
        {"Id": "c4", "Names": ["/ignoreme"], "Image": "alpine:3.20", "Labels": {}},
    ]
    DOCKER.inspects = {
        "c1": {"Image": "sha256:" + "a" * 64,
               "Config": {"Image": "ghcr.io/org/site:1.4.0", "Labels": {
                   "traefik.http.routers.site.rule": "Host(`site.example.com`)",
                   "traefik.http.routers.site.tls": "true",
                   "traefik.http.routers.site-http.rule": "Host(`site.example.com`)",
               }}},
        "c2": {"Image": "sha256:" + "b" * 64,
               "Config": {"Image": "postgres:16-alpine", "Env": ["COOLIFY_URL=https://db.example.com"]}},
    }
    DOCKER.images = {
        "sha256:" + "a" * 64: {
            "RepoDigests": ["ghcr.io/org/site@sha256:local-site"],
            "Created": "2026-10-01T10:00:00Z",
            "Config": {"Labels": {"org.opencontainers.image.version": "1.4.0"}},
        },
        "sha256:" + "b" * 64: {
            "RepoDigests": ["postgres@sha256:local-pg"],
            "Created": "2026-09-01T10:00:00Z",
            "Config": {"Labels": {}},
        },
    }
    REGISTRY.digests[("ghcr.io", "org/site", "1.4.0")] = "sha256:remote-site"
    REGISTRY.digests[("registry-1.docker.io", "library/postgres", "16-alpine")] = "sha256:local-pg"


class CollectTest(unittest.TestCase):
    def setUp(self):
        reset_app()
        docker_fixture()

    def test_rows_compare_digests_not_version_strings(self):
        rows, counts, ignored = app.collect()
        by_name = {row["container"]: row for row in rows}
        self.assertEqual(ignored, 2)
        self.assertNotIn("coolify-proxy", by_name)
        self.assertNotIn("ignoreme", by_name)
        self.assertEqual(by_name["app-web-1"]["status"], "UPDATE")
        self.assertEqual(by_name["db-postgres-1"]["status"], "ok")
        self.assertEqual(counts, {"UPDATE": 1, "ok": 1, "??": 0})

    def test_image_id_in_container_list_is_replaced_by_the_reference(self):
        rows, _, _ = app.collect()
        by_name = {row["container"]: row for row in rows}
        self.assertEqual(by_name["db-postgres-1"]["image"], "postgres:16-alpine")
        self.assertEqual(by_name["db-postgres-1"]["tag"], "16-alpine")
        # "16-alpine" is a major-line tag; "16.4-alpine" would be a minor one
        self.assertEqual(by_name["db-postgres-1"]["policy"], "major")
        self.assertEqual(by_name["db-postgres-1"]["tag_version"], "16")

    def test_update_rows_sort_first(self):
        rows, _, _ = app.collect()
        self.assertEqual(rows[0]["container"], "app-web-1")

    def test_row_carries_url_version_and_created(self):
        rows, _, _ = app.collect()
        by_name = {row["container"]: row for row in rows}
        self.assertEqual(by_name["app-web-1"]["url"], "https://site.example.com")
        self.assertEqual(by_name["app-web-1"]["version"], "1.4.0")
        self.assertEqual(by_name["app-web-1"]["created"], "2026-10-01")
        # no token: no resource validation, so the uuid stays empty and is explained
        self.assertIsNone(by_name["app-web-1"]["deploy_uuid"])
        self.assertEqual(by_name["app-web-1"]["note"], "uuid not found in coolify")

    def test_container_url_falls_back_to_coolify_env(self):
        rows, _, _ = app.collect()
        by_name = {row["container"]: row for row in rows}
        self.assertEqual(by_name["db-postgres-1"]["url"], "https://db.example.com")

    def test_unreachable_daemon_is_an_error_state_not_a_crash(self):
        DOCKER.down = True
        self.assertTrue(app.run_check())
        self.assertTrue(wait_for(lambda: not app.CHECKING.is_set()))
        self.assertIn("RuntimeError", app.STATE["error"] or "")

    def test_local_build_gets_an_explained_question_mark(self):
        DOCKER.images["sha256:" + "a" * 64]["RepoDigests"] = []
        rows, counts, _ = app.collect()
        row = next(r for r in rows if r["container"] == "app-web-1")
        self.assertEqual(row["status"], "??")
        self.assertEqual(row["note"], "local build / no registry")
        self.assertEqual(counts["??"], 1)


class ContainerUrlTest(unittest.TestCase):
    def test_tls_router_wins_over_plain_router(self):
        inspect = {"Config": {"Labels": {
            "traefik.http.routers.a.rule": "Host(`plain.example.com`)",
            "traefik.http.routers.b.rule": "Host(`secure.example.com`)",
            "traefik.http.routers.b.tls": "true",
        }}}
        self.assertEqual(app.container_url(inspect), "https://secure.example.com")

    def test_no_labels_no_env(self):
        self.assertEqual(app.container_url({"Config": {}}), "")

    def test_placeholder_urls_are_ignored(self):
        inspect = {"Config": {"Env": ["COOLIFY_URL=http://host.docker.internal:8000"]}}
        self.assertEqual(app.container_url(inspect), "")


# --------------------------------------------------------------- http layer --
class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        reset_app()
        docker_fixture()

    def request(self, path, body=None, method=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"{}")
            except ValueError:
                return exc.code, {"raw": raw.decode()}

    def test_healthz(self):
        status, payload = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], app.VERSION)

    def test_index_serves_the_page(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("Coolify Updates Manager", resp.read().decode())
            self.assertEqual(resp.headers["X-Frame-Options"], "DENY")

    def test_state_reports_config_and_deploy_disabled_without_token(self):
        status, payload = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(payload["deploy_enabled"])
        self.assertEqual(payload["levels"], ["current", "major", "minor"])
        self.assertEqual(payload["config"]["workers"], app.WORKERS)
        self.assertNotIn("token", json.dumps(payload["config"]).lower())

    def test_check_answers_202_then_409_while_running(self):
        self.assertEqual(self.request("/api/check", {})[0], 202)
        self.assertEqual(self.request("/api/check", {})[0], 409)
        self.assertTrue(wait_for(lambda: len(app.STATE["rows"]) == 2))
        self.assertFalse(app.CHECKING.is_set())

    def test_level_validation(self):
        status, payload = self.request("/api/level", {"level": "wrong"})
        self.assertEqual(status, 400)
        self.assertIn("current, major, minor", payload["error"])

    def test_level_change_is_persisted(self):
        status, payload = self.request("/api/level", {"level": "current"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["level"], "current")

    def test_deploy_is_503_without_token(self):
        status, payload = self.request("/api/deploy", {"uuid": "a" * 22})
        self.assertEqual(status, 503)
        self.assertIn("COOLIFY_TOKEN", payload["error"])

    def test_deploy_rejects_bad_uuid(self):
        status, payload = self.request("/api/deploy", {"uuid": "not-a-uuid"})
        self.assertEqual(status, 400)
        self.assertIn("uuid", payload["error"])

    def test_deploy_queues_a_job_that_fails_when_coolify_is_unreachable(self):
        # the uuid list cannot be validated without a reachable Coolify, so the
        # request is accepted and the failure surfaces on the job, not the call
        uuid = "a" * 22
        app.COOLIFY_TOKEN = "test-token"
        try:
            status, payload = self.request("/api/deploy", {"uuid": uuid})
            self.assertEqual(status, 200)
            self.assertEqual(payload["results"][0]["status"], 202)
            self.assertTrue(wait_for(lambda: app.DEPLOY_JOBS[uuid]["status"] == "error"))
            self.assertTrue(app.DEPLOY_JOBS[uuid]["message"])
        finally:
            app.COOLIFY_TOKEN = ""
            wait_for(lambda: not app.JOB_POLLER.is_set())

    def test_preview_returns_empty_for_unknown_uuid(self):
        status, payload = self.request("/api/preview", {"uuid": "b" * 22})
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])

    def test_pin_for_unknown_container_is_404(self):
        status, payload = self.request("/api/pin", {"container": "ghost"})
        self.assertEqual(status, 404)

    def test_deploys_endpoint(self):
        status, payload = self.request("/api/deploys")
        self.assertEqual(status, 200)
        self.assertIn("jobs", payload)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.request("/nope")[0], 404)
        self.assertEqual(self.request("/api/nope", {})[0], 404)


# ------------------------------------------------------------- state on disk --
class StateFileTest(unittest.TestCase):
    def setUp(self):
        reset_app()

    def test_roundtrip(self):
        app.STATE["rows"] = [{"container": "one", "status": "ok"}]
        app.STATE["level"] = "minor"
        app.save_state()
        with app.LOCK:
            app.STATE["rows"] = []
            app.STATE["level"] = "current"
        self.assertFalse(app.load_state())
        self.assertEqual(app.STATE["rows"], [{"container": "one", "status": "ok"}])
        self.assertEqual(app.STATE["level"], "minor")

    def test_rows_from_an_older_schema_are_dropped(self):
        with open(app.STATE_FILE, "w") as handle:
            json.dump({"schema": app.STATE["schema"] - 1,
                       "rows": [{"container": "old"}],
                       "pins": {"x": {"matches": ["1.0.0"]}}}, handle)
        self.assertTrue(app.load_state())
        self.assertEqual(app.STATE["rows"], [])
        self.assertEqual(app.STATE["pins"], {})

    def test_unknown_level_falls_back_to_current(self):
        with open(app.STATE_FILE, "w") as handle:
            json.dump({"schema": app.STATE["schema"], "level": "banana"}, handle)
        app.load_state()
        self.assertEqual(app.STATE["level"], "current")

    def test_unwritable_state_file_only_warns(self):
        original = app.STATE_FILE
        app.STATE_FILE = "/proc/definitely/not/writable/state.json"
        try:
            app.save_state()   # must not raise
        finally:
            app.STATE_FILE = original


# ---------------------------------------------------------------- preview --
class PreviewTest(unittest.TestCase):
    def setUp(self):
        reset_app()
        app.STATE["rows"] = [
            {"container": "web-1", "image": "ghcr.io/org/site:1.4.0", "status": "UPDATE",
             "deploy_uuid": "u" * 22, "resource": "web", "tag": "1.4.0", "digests": [], "version": "1.4.0"},
            {"container": "web-2", "image": "ghcr.io/org/site:1.4.0", "status": "UPDATE",
             "deploy_uuid": "u" * 22, "resource": "web", "tag": "1.4.0", "digests": [], "version": "1.4.0"},
            {"container": "db-1", "image": "postgres:16-alpine", "status": "ok",
             "deploy_uuid": "v" * 22, "resource": "db", "tag": "16-alpine", "digests": [], "version": ""},
        ]
        with app.LOCK:
            app.STATE["pins"]["ghcr.io/org/site:1.4.0"] = {"matches": ["1.4.0"]}
            app.STATE["pins"]["postgres:16-alpine"] = {"matches": ["16.4-alpine"]}
        REGISTRY.tags[("registry-1.docker.io", "library/postgres")] = ["16.4-alpine"]

    def test_containers_are_grouped_by_resource(self):
        payload = app.preview(["u" * 22, "v" * 22])
        self.assertEqual([item["uuid"] for item in payload["items"]], ["u" * 22, "v" * 22])
        web = payload["items"][0]
        self.assertEqual([c["container"] for c in web["containers"]], ["web-1", "web-2"])
        self.assertEqual(web["containers"][0]["running"], "1.4.0")
        self.assertFalse(payload["cached"])

    def test_second_call_hits_the_cache(self):
        app.preview(["u" * 22])
        cached = app.preview(["u" * 22])
        self.assertTrue(cached["cached"])
        forced = app.preview(["u" * 22], force=True)
        self.assertFalse(forced["cached"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
