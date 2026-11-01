#!/usr/bin/env python3
"""coolify-updates-manager - Docker image update dashboard with one-click deploy.

Talks to the Docker daemon through its API (unix socket, read-only) and to the
registries to compare the digest of the running image with the digest the tag
points to right now.

It also answers a second question: if I pin my compose file to a major or a minor
version line, which tag should I write, and does that tag differ from what I am
running? That is the "level" selector in the UI (current tag / major / minor).

If a Coolify API token is configured, each row gets a button that triggers
POST /api/v1/deploy?uuid=<uuid>.

No third-party dependencies: Python 3.11+ standard library only.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.6.0"
APP_NAME = "coolify-updates-manager"

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
# NOTE: never read COOLIFY_URL here. Coolify reserves that name and injects the
# public URL of the app itself into it, which would make deploy calls loop back
# into this dashboard.
COOLIFY_API_URL = os.environ.get("COOLIFY_API_URL", "http://host.docker.internal:8000").rstrip("/")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN", "").strip()
BIND = os.environ.get("UPDATES_BIND", "0.0.0.0")
PORT = int(os.environ.get("UPDATES_PORT", "8799"))
STATE_FILE = os.environ.get("UPDATES_STATE", "/data/state.json")
WORKERS = int(os.environ.get("UPDATES_WORKERS", "8"))
CHECK_TIMEOUT = int(os.environ.get("UPDATES_TIMEOUT", "60"))
TAGS_TTL = int(os.environ.get("UPDATES_TAGS_TTL", "21600"))      # registry tag list
DIGEST_TTL = int(os.environ.get("UPDATES_DIGEST_TTL", "900"))    # manifest digest
PREVIEW_TTL = int(os.environ.get("UPDATES_PREVIEW_TTL", "300"))  # deploy dialog preview
VERSIONS_TTL = int(os.environ.get("UPDATES_VERSIONS_TTL", "300"))  # running/next per image

DEPLOY_HINT = "" if COOLIFY_TOKEN else "deploy disabled: COOLIFY_TOKEN is not set"

LEVELS = ("current", "major", "minor")
LEVEL_LABELS = {
    "current": "current tag",
    "major": "major line",
    "minor": "minor line",
}

MANIFEST_ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
PRERELEASE = re.compile(
    r"canary|nightly|develop|preview|testing|test|edge|alpha|beta|rc|dev|slim|"
    r"distroless|windowsservercore|nanoserver|munge|baseimage|mod|arm|amd64",
    re.I,
)
FLOATING_TAGS = {
    "latest", "stable", "release", "edge", "nightly", "main", "master", "develop",
    "dev", "rolling", "current", "prod", "production",
}


# ------------------------------------------------------------------ docker --
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout=CHECK_TIMEOUT):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.sock_path)
        self.sock = sock


def docker_get(path):
    conn = UnixHTTPConnection(DOCKER_SOCK)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"docker {path} -> {resp.status}: {body[:200]!r}")
        return json.loads(body)
    finally:
        conn.close()


def docker_post(path, timeout=900):
    """POST to the Docker API and return (status, body)."""
    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=timeout)
    try:
        conn.request("POST", path, headers={"Content-Length": "0"})
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def docker_pull(ref):
    """Pull one image through the Docker API. Returns (ok, message).

    Coolify's "deploy" for services recreates containers without pulling
    (StartService defaults pullLatestImages to false on the API path), so the
    panel pulls first and then asks Coolify to recreate.
    """
    if not ref or "@" in ref:
        return True, "digest pinned, skipped"
    registry, repo, tag = split_ref(ref)
    name = repo if registry == "registry-1.docker.io" else f"{registry}/{repo}"
    query = urllib.parse.urlencode({"fromImage": name, "tag": tag})
    try:
        status, body = docker_post(f"/images/create?{query}")
    except Exception as exc:  # noqa: BLE001
        return False, f"pull failed: {type(exc).__name__}: {exc}"
    if status >= 400:
        return False, f"pull failed: HTTP {status}: {body[:200]}"
    errors = [line for line in body.splitlines() if "\"error\"" in line]
    if errors:
        return False, f"pull failed: {errors[0][:200]}"
    return True, "pulled"


def is_image_id(ref):
    return bool(re.fullmatch(r"(sha256:)?[0-9a-f]{12,64}", ref or ""))


# ---------------------------------------------------------------- registry --
def split_ref(ref):
    """ghcr.io/org/app:tag -> (registry_host, repository, tag)"""
    ref = (ref or "").split("@")[0]
    first = ref.split("/")[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, ref.split("/", 1)[1]
    else:
        registry, path = "docker.io", ref
    last = path.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = path.rsplit(":", 1)
    else:
        repo, tag = path, "latest"
    if registry == "docker.io":
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = "library/" + repo
    return registry, repo, tag


def _token_from(challenge_headers):
    header = challenge_headers.get("WWW-Authenticate", "")
    realm = re.search(r'realm="([^"]+)"', header)
    if not realm:
        return None
    query = {}
    service = re.search(r'service="([^"]+)"', header)
    scope = re.search(r'scope="([^"]+)"', header)
    if service:
        query["service"] = service.group(1)
    if scope:
        query["scope"] = scope.group(1)
    with urllib.request.urlopen(f"{realm.group(1)}?{urllib.parse.urlencode(query)}", timeout=25) as resp:
        data = json.load(resp)
    return data.get("token") or data.get("access_token")


def registry_request(url, headers=None, method="GET"):
    headers = dict(headers or {})
    for _ in range(2):
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            return urllib.request.urlopen(request, timeout=CHECK_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            token = _token_from(exc.headers)
            if not token:
                raise
            headers["Authorization"] = f"Bearer {token}"
    raise RuntimeError("registry auth loop")


_tags_cache = {}
_digest_cache = {}
CACHE_LOCK = threading.Lock()


def registry_digest(registry, repo, tag):
    key = (registry, repo, tag)
    with CACHE_LOCK:
        hit = _digest_cache.get(key)
        if hit and time.time() - hit[0] < DIGEST_TTL:
            return hit[1]
    try:
        resp = registry_request(
            f"https://{registry}/v2/{repo}/manifests/{tag}",
            headers={"Accept": MANIFEST_ACCEPT},
            method="HEAD",
        )
        digest = resp.headers.get("Docker-Content-Digest")
    except Exception:
        digest = None
    with CACHE_LOCK:
        _digest_cache[key] = (time.time(), digest)
    return digest


def registry_tags(registry, repo, max_pages=40):
    key = (registry, repo)
    with CACHE_LOCK:
        hit = _tags_cache.get(key)
        if hit and time.time() - hit[0] < TAGS_TTL:
            return hit[1]
    tags, url = [], f"https://{registry}/v2/{repo}/tags/list?n=1000"
    for _ in range(max_pages):
        resp = registry_request(url)
        tags += json.load(resp).get("tags") or []
        link = resp.headers.get("Link", "")
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not nxt:
            break
        url = nxt.group(1)
        if url.startswith("/"):
            url = f"https://{registry}{url}"
    with CACHE_LOCK:
        _tags_cache[key] = (time.time(), tags)
    return tags


# ------------------------------------------------------- versions and tags --
VERSION_AT_START = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")


def parse_version(text):
    """Leading numeric version of a tag or label: 'v3.3.0' -> '3.3.0'."""
    if not text:
        return None
    match = VERSION_AT_START.match(str(text).strip())
    return match.group(1) if match else None


def classify_tag(tag):
    """Return (policy, version) for a tag as written in a compose file."""
    if not tag:
        return "floating", ""
    if tag.startswith("sha256:"):
        return "digest", ""
    if tag.lower() in FLOATING_TAGS:
        return "floating", ""
    version = parse_version(tag)
    if not version:
        if re.fullmatch(r"[a-z]+-?\d+", tag, re.I):   # pg16, alpine3
            return "major", parse_version(tag) or ""
        return "custom", ""
    parts = version.split(".")
    if len(parts) == 1:
        return "major", version
    if len(parts) == 2:
        return "minor", version
    return "exact", version


def _version_key(tag):
    numbers = [int(x) for x in re.findall(r"\d+", tag)[:4]]
    return (numbers + [0, 0, 0, 0])[:4]


def _linuxserver_key(tag):
    match = re.search(r"-ls(\d+)$", tag)
    return _version_key(tag) + [int(match.group(1)) if match else 0]


def suggest_pin(ref):
    """Versioned tag that currently resolves to the same digest as this tag."""
    registry, repo, tag = split_ref(ref)
    digest = registry_digest(registry, repo, tag)
    result = {"image": ref, "floating_tag": tag, "digest": digest, "matches": [], "tags": 0}
    if not digest:
        result["error"] = "tag not found in the registry"
        return result

    tags = [t for t in registry_tags(registry, repo) if not PRERELEASE.search(t)]
    result["tags"] = len(tags)

    def probe(candidates, limit=6):
        for candidate in candidates[:limit]:
            if registry_digest(registry, repo, candidate) == digest:
                return candidate
        return None

    def scoped(candidates):
        prefix = re.match(r"^[vV]?(\d+(?:\.\d+)*)", tag)
        if not prefix:
            return candidates
        narrowed = [t for t in candidates if re.match(r"^[vV]?" + re.escape(prefix.group(1)) + r"(\D|$)", t)]
        return narrowed or candidates

    # release-looking tags first (v3.5.1, 2.10.12, 1.5-stable) so dated or
    # per-commit tags (12.4.20261026-211555) do not take the free slots
    release_like = re.compile(r"^[vV]?\d+(\.\d+){0,2}([-_][A-Za-z][A-Za-z0-9.]*)?$")
    dateish = re.compile(r"\d{6,}")   # 2026102610 style build stamps rank above real releases
    primary = scoped([t for t in tags if release_like.match(t) and not dateish.search(t)])
    primary.sort(key=_version_key, reverse=True)
    hit = probe(primary, limit=8)
    if not hit:  # LinuxServer images use <appversion>-ls<N>
        lsio = scoped([t for t in tags if re.fullmatch(r"v?\d+(\.\d+)*\.\d+-ls\d+", t)])
        lsio.sort(key=_linuxserver_key, reverse=True)
        hit = probe(lsio)
    if not hit:
        wide = scoped([t for t in tags if re.fullmatch(r"[vV]?\d+(\.\d+)*(-[A-Za-z0-9._]+)?", t)])
        wide.sort(key=_version_key, reverse=True)
        hit = probe(wide)  
    if hit:
        base = hit.split("-ls")[0]
        alternatives = [t for t in tags if t != hit and t.startswith(base)]
        result["matches"] = [hit] + alternatives[:3]
    return result


_exact_cache = {}


def exact_tag_for_digest(registry, repo, digest):
    """Which versioned tag carries this digest (reverse lookup, cached)."""
    if not digest:
        return None
    key = (registry, repo, digest)
    with CACHE_LOCK:
        hit = _exact_cache.get(key)
        if hit and time.time() - hit[0] < TAGS_TTL:
            return hit[1]
    result = None
    try:
        tags = [t for t in registry_tags(registry, repo) if not PRERELEASE.search(t)]
        release_like = re.compile(r"^[vV]?\d+(\.\d+){0,2}([-_][A-Za-z][A-Za-z0-9.]*)?$")
        dateish = re.compile(r"\d{6,}")
        pools = [
            sorted([t for t in tags if release_like.match(t) and not dateish.search(t)],
                   key=_version_key, reverse=True),
            sorted([t for t in tags if re.fullmatch(r"v?\d+(\.\d+)*\.\d+-ls\d+", t)],
                   key=_linuxserver_key, reverse=True),
            sorted([t for t in tags if re.fullmatch(r"[vV]?\d+(\.\d+)*(-[A-Za-z0-9._]+)?", t)],
                   key=_version_key, reverse=True),
        ]
        for pool in pools:
            for candidate in pool[:8]:
                if registry_digest(registry, repo, candidate) == digest:
                    result = candidate
                    break
            if result:
                break
    except Exception:
        result = None
    with CACHE_LOCK:
        _exact_cache[key] = (time.time(), result)
    return result


def version_running(row):
    """Version the container is running today (label, tag text, or digest lookup)."""
    label = parse_version(row.get("version"))
    if label:
        return label
    tag_version = parse_version(row.get("tag") or "")
    if tag_version and tag_version.count(".") >= 1:
        return tag_version
    registry, repo, _ = split_ref(row["image"])
    for digest in (row.get("digests") or [])[:2]:
        tag = exact_tag_for_digest(registry, repo, digest)
        if tag:
            return parse_version(tag) or tag_version
    return tag_version or ""


def version_after_pull(row):
    """Version the current tag will give on the next deploy.

    Deploying does not rewrite the compose tag, it re-pulls it, so this is the
    release the tag points to right now (not the level target).
    """
    exact = newest_release(row)
    if exact:
        return parse_version(exact) or exact
    return ""


_versions_cache = {}
WARMING = threading.Event()
WARM = {"total": 0, "done": 0}


def _versions_key(row):
    return (row["image"], row.get("tag") or "", tuple((row.get("digests") or [])[:2]))


def version_facts(row):
    """Running version and next version for one row, cached for VERSIONS_TTL.

    Resolving these can cost a dozen registry requests (digest lookups), so the
    result is reused across dialog opens and across selections.
    """
    key = _versions_key(row)
    now = time.time()
    with LOCK:
        hit = _versions_cache.get(key)
        if hit and now - hit[0] < VERSIONS_TTL:
            return hit[1]
    facts = {"running": version_running(row), "next": version_after_pull(row)}
    with LOCK:
        _versions_cache[key] = (now, facts)
    return facts


def warm_versions():
    """Resolve versions for every row in the background, right after a check.

    Keeps the deploy dialog instant: by the time someone clicks, the expensive
    lookups are already cached for VERSIONS_TTL.
    """
    if WARMING.is_set():
        return False
    WARMING.set()

    def work():
        try:
            rows = [r for r in list(STATE["rows"]) if r.get("digests") or r.get("remote_digest")]
            with LOCK:
                WARM.update(total=len(rows), done=0)

            def one(row):
                try:
                    newest_release(row)
                    version_facts(row)
                except Exception:
                    pass
                with LOCK:
                    WARM["done"] += 1

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                list(pool.map(one, rows))
        finally:
            WARMING.clear()

    threading.Thread(target=work, daemon=True).start()
    return True


def newest_release(row):
    """Exact tag the image's channel points to today (e.g. v3.5.1 for :latest).

    Cached in STATE['pins'] and persisted, so switching levels repeatedly does
    not re-hit the registry.
    """
    image = row["image"]
    with LOCK:
        pin = STATE["pins"].get(image)
    if pin is None:
        try:
            pin = suggest_pin(image)
        except Exception:
            pin = {"matches": []}
        with LOCK:
            STATE["pins"][image] = pin
        save_state()
    matches = (pin or {}).get("matches") or []
    return matches[0] if matches else None


def resolve_level(row, level):
    """Tag to write in the compose file to pin at `level`, and whether it is newer.

    The target comes from the newest release the registry offers, not from what is
    running: picking "minor" means "pin me to the newest 3.5 tag", so the compare
    that follows tells whether the running image is already at that line.
    """
    if level == "current":
        return None

    registry, repo, tag = split_ref(row["image"])
    exact_tag = newest_release(row)
    version = parse_version(exact_tag) if exact_tag else None
    if not version:
        version = parse_version(row.get("version")) or parse_version(tag)
    if not version:
        return {"level": level, "status": row.get("status", "??"), "note": "version unknown"}

    parts = version.split(".")
    wanted = parts[0] if level == "major" or len(parts) < 2 else ".".join(parts[:2])
    reference = exact_tag or tag
    suffix_match = re.match(r"^[vV]?\d+(?:\.\d+)*([-_][A-Za-z0-9._-]+)$", reference)
    suffix = suffix_match.group(1) if suffix_match else ""

    available = set(registry_tags(registry, repo))
    candidates = []
    for prefix in ("v", ""):
        for tail in ((suffix, "") if suffix else ("",)):
            candidates.append(f"{prefix}{wanted}{tail}")
    target = next((c for c in candidates if c in available), None)
    note = ""
    if not target:
        if not exact_tag:
            return {"level": level, "target_tag": None, "target_version": version, "wanted": wanted,
                    "status": "??", "note": f"no {wanted} tag published"}
        # project publishes only full versions: point at the exact tag instead
        target = exact_tag
        note = f"no {wanted} tag; exact instead"

    target_digest = registry_digest(registry, repo, target)
    if not target_digest:
        status = "??"
    elif target_digest in (row.get("digests") or []):
        status = "ok"
    else:
        status = "UPDATE"
    return {
        "level": level,
        "target_tag": target,
        "target_version": version,
        "wanted": wanted,
        "status": status,
        "note": note,
    }


# --------------------------------------------------------------- collector --
STATE = {
    "checked_at": None,
    "checked_at_epoch": None,
    "duration": None,
    "rows": [],
    "counts": {"UPDATE": 0, "ok": 0, "??": 0},
    "error": None,
    "pins": {},
    "level": "current",
    "resolved_at": {},
}
CHECKING = threading.Event()
RESOLVING = threading.Event()
DEPLOY_LOG = {}
LOCK = threading.Lock()


def load_state():
    try:
        with open(STATE_FILE) as handle:
            disk = json.load(handle)
        STATE.update({k: v for k, v in disk.items() if k in STATE})
    except Exception:
        pass
    if STATE.get("level") not in LEVELS:
        STATE["level"] = "current"


def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(STATE, handle)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"warn: could not write state file: {exc}", flush=True)


def _image_facts(image_id):
    try:
        image = docker_get(f"/images/{image_id}/json") if image_id else {}
    except Exception:
        image = {}
    labels = (image.get("Config") or {}).get("Labels") or {}
    return {
        "digests": [d.split("@")[-1] for d in (image.get("RepoDigests") or [])],
        "version": labels.get("org.opencontainers.image.version") or "",
        "created": (image.get("Created") or "")[:10],
    }


def collect():
    containers = docker_get("/containers/json?all=1")
    prepared, image_ids = [], {}
    for container in containers:
        name = (container.get("Names") or ["/?"])[0].lstrip("/")
        labels = container.get("Labels") or {}
        ref = container.get("Image") or ""
        try:
            inspect = docker_get(f"/containers/{container['Id']}/json")
        except Exception:
            inspect = {}
        config_image = (inspect.get("Config") or {}).get("Image") or ""
        image_id = inspect.get("Image") or ""
        if is_image_id(ref) and config_image and not is_image_id(config_image):
            ref = config_image
        prepared.append({
            "container": name,
            "ref": ref,
            "project": labels.get("com.docker.compose.project", ""),
            "resource": labels.get("coolify.resourceName", ""),
            "type": labels.get("coolify.type", ""),
        })
        image_ids.setdefault(ref, image_id)

    facts = {ref: _image_facts(image_id) for ref, image_id in image_ids.items()}
    known = coolify_resources()

    def check(item):
        registry, repo, tag = split_ref(item["ref"])
        remote = registry_digest(registry, repo, tag)
        local = facts.get(item["ref"], {}).get("digests") or []
        if not remote or not local:
            status = "??"
        elif remote in local:
            status = "ok"
        else:
            status = "UPDATE"
        deploy_uuid = resolve_deploy_uuid(item["container"], item["project"], known)
        if item["project"] == "source":
            note = "coolify internal stack"
        elif not item["project"]:
            note = "no coolify label"
        elif status == "??":
            note = "local build / no registry"
        elif not deploy_uuid:
            note = "uuid not found in coolify"
        else:
            note = ""
        policy, tag_version = classify_tag(tag)
        facts_for_image = facts.get(item["ref"], {})
        return {
            "status": status,
            "container": item["container"],
            "image": item["ref"],
            "tag": tag,
            "policy": policy,
            "tag_version": tag_version,
            "version": facts_for_image.get("version", ""),
            "created": facts_for_image.get("created", ""),
            "remote_digest": remote or "",
            "digests": local,
            "project": item["project"],
            "deploy_uuid": deploy_uuid,
            "resource": item["resource"],
            "type": item["type"],
            "note": note,
        }

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = list(pool.map(check, prepared))

    counts = {"UPDATE": 0, "ok": 0, "??": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    rows.sort(key=lambda r: (0 if r["status"] == "UPDATE" else 1 if r["status"] == "??" else 2, r["container"]))
    return rows, counts


def resolve_rows(level):
    """Fill row['target'] for the given level using cached registry data."""
    if level == "current":
        for row in STATE["rows"]:
            row.pop("target", None)
        STATE["resolved_at"].pop(level, None)
        return

    def work(row):
        try:
            return resolve_level(row, level)
        except Exception as exc:  # noqa: BLE001
            return {"level": level, "status": "??", "note": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        targets = list(pool.map(work, STATE["rows"]))
    for row, target in zip(STATE["rows"], targets):
        row["target"] = target
    STATE["resolved_at"][level] = time.strftime("%Y-%m-%d %H:%M:%S")


def _finish_check(rows, counts, started):
    STATE.update(
        checked_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        checked_at_epoch=int(time.time()),
        duration=round(time.time() - started, 1),
        rows=rows,
        counts=counts,
        error=None,
    )
    with LOCK:
        _preview_cache.clear()   # rows changed, any cached preview is stale
    save_state()                 # persist before the slow parts start


def run_check():
    if CHECKING.is_set():
        return False
    CHECKING.set()

    def work():
        started = time.time()
        try:
            rows, counts = collect()
            _finish_check(rows, counts, started)
            CHECKING.clear()     # the table is usable now
            # slow parts run in the background so the button never blocks on them
            start_resolve()
            warm_versions()
        except Exception as exc:  # noqa: BLE001
            STATE.update(error=f"{type(exc).__name__}: {exc}")
            save_state()
            CHECKING.clear()

    threading.Thread(target=work, daemon=True).start()
    return True


def start_resolve():
    """Fill the target column for the selected level, in the background."""
    level = STATE["level"]
    if level == "current" or not STATE["rows"] or RESOLVING.is_set():
        return False
    RESOLVING.set()

    def work():
        try:
            resolve_rows(level)
        except Exception as exc:  # noqa: BLE001
            STATE.update(error=f"{type(exc).__name__}: {exc}")
        save_state()
        RESOLVING.clear()

    threading.Thread(target=work, daemon=True).start()
    return True


def set_level(level):
    if level not in LEVELS or level == STATE["level"]:
        return False
    STATE["level"] = level
    save_state()
    if level == "current":
        resolve_rows("current")
        return True
    start_resolve()
    return True


# ------------------------------------------------------------------ deploy --
DEPLOY_JOBS = {}
TERMINAL_JOB = {"finished", "failed", "cancelled", "error", "timeout"}
COOLIFY_STATUS = {
    "queued": "queued",
    "in_progress": "in progress",
    "finished": "finished",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _job_start(uuid, known):
    rows = [r for r in STATE["rows"] if (r.get("deploy_uuid") or "") == uuid]
    return {
        "uuid": uuid,
        "resource": (rows[0].get("resource") if rows else "") or uuid,
        "containers": [r["container"] for r in rows],
        "images": sorted({r["image"] for r in rows if r.get("image") and r.get("status") != "??"}),
        "kind": known.get(uuid) or "?",
        "started": time.time(),
        "started_at": time.strftime("%H:%M:%S"),
        "status": "pulling",
        "message": "",
        "pulled": [],
        "deployment_uuid": None,
        "deployment_url": None,
        "polled": 0,
    }


def deploy(uuids):
    """Register one job per resource and run it in the background."""
    known = coolify_resources()
    results = []
    for uuid in uuids:
        job = _job_start(uuid, known)
        entry = {"uuid": uuid, "resource": job["resource"], "ok": True, "status": 202, "message": "queued"}
        if not COOLIFY_TOKEN:
            job.update(status="error", message=DEPLOY_HINT)
            entry.update(ok=False, status=0, message=DEPLOY_HINT)
        elif known and uuid not in known:
            message = "uuid is not a resource in this Coolify instance"
            job.update(status="error", message=message)
            entry.update(ok=False, status=0, message=message)
        with LOCK:
            DEPLOY_JOBS[uuid] = job
            DEPLOY_LOG[uuid] = {"at": job["started_at"], "ok": entry["ok"], "message": entry["message"][:200]}
        if entry["ok"]:
            threading.Thread(target=_run_deploy_job, args=(job,), daemon=True).start()
        results.append(entry)
    return results


def _run_deploy_job(job):
    """Pull the images, then ask Coolify to redeploy the resource."""
    uuid = job["uuid"]
    # Coolify's service deploy recreates containers without pulling, so pull here
    if job.get("kind") != "application":
        job["message"] = f"pulling {len(job.get('images') or [])} image(s)"
        for ref in job.get("images") or []:
            ok, message = docker_pull(ref)
            with LOCK:
                job["pulled"].append({"image": ref, "ok": ok, "message": message})
            if not ok:
                job.update(status="error", message=message)
                return
    job["status"] = "submitting"
    request = urllib.request.Request(
        f"{COOLIFY_API_URL}/api/v1/deploy?uuid={urllib.parse.quote(uuid)}&force=false",
        method="POST",
        headers={"Authorization": f"Bearer {COOLIFY_TOKEN}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                first = (json.loads(body).get("deployments") or [{}])[0]
            except Exception:
                first = {}
            job["deployment_uuid"] = first.get("deployment_uuid")
            job["deployment_url"] = first.get("deployment_url")
            job["message"] = (first.get("message") or body)[:300]
            job["status"] = "queued" if job["deployment_uuid"] else "in progress"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        job.update(status="error", message=f"HTTP {exc.code}: {body[:300]}")
    except Exception as exc:  # noqa: BLE001
        job.update(status="error", message=f"{type(exc).__name__}: {exc}")
    with LOCK:
        # a deploy may change what the tags resolve to
        _preview_cache.clear()
        _versions_cache.clear()


def _container_created_after(resource, since, old_names):
    """True when a container of `resource` was (re)created after `since`."""
    try:
        containers = docker_get("/containers/json?all=1")
    except Exception:
        return False
    for container in containers:
        labels = container.get("Labels") or {}
        if labels.get("coolify.resourceName") != resource:
            continue
        name = (container.get("Names") or [""])[0].lstrip("/")
        created = (container.get("Created") or 0)
        if created >= since - 1 or name not in old_names:
            return True
    return False


def poll_deploy_jobs(force=False):
    """Refresh the status of running deploy jobs (Coolify API or container recreation)."""
    now = time.time()
    for job in list(DEPLOY_JOBS.values()):
        if job.get("status") in TERMINAL_JOB:
            continue
        if job.get("status") in ("pulling", "submitting"):
            continue
        if not force and now - job.get("polled", 0) < 3:
            continue
        job["polled"] = now
        if job.get("deployment_uuid"):
            request = urllib.request.Request(
                f"{COOLIFY_API_URL}/api/v1/deployments/{job['deployment_uuid']}",
                headers={"Authorization": f"Bearer {COOLIFY_TOKEN}", "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as resp:
                    dep = json.load(resp)
                raw = (dep.get("status") or "").lower()
                job["status"] = COOLIFY_STATUS.get(raw, raw or job["status"])
                job["deployment_url"] = dep.get("deployment_url") or job.get("deployment_url")
                if dep.get("finished_at"):
                    job["finished_at"] = dep["finished_at"]
            except Exception:
                pass
        else:
            # services have no deployment id: watch for a recreated container
            if _container_created_after(job["resource"], job["started"], set(job["containers"])):
                job["status"] = "finished"
                job["message"] = job.get("message") or "container recreated"
            elif now - job["started"] > 900:
                job["status"] = "timeout"
                job["message"] = "no new container after 15 min, check Coolify"
        # once a deploy ends, re-read the containers so the table shows the new version
        if job.get("status") in TERMINAL_JOB and not job.get("refresh_queued"):
            job["refresh_queued"] = True
            run_check()
    return list(DEPLOY_JOBS.values())


def deploy_jobs():
    jobs = sorted(poll_deploy_jobs(), key=lambda j: j.get("started", 0), reverse=True)
    ui = os.environ.get("COOLIFY_UI_URL", "").rstrip("/")
    out = []
    for job in jobs[:20]:
        item = {k: v for k, v in job.items() if k not in ("started", "polled")}
        item["elapsed"] = int(time.time() - job.get("started", time.time()))
        item["link"] = f"{ui}{job['deployment_url']}" if ui and job.get("deployment_url") else None
        out.append(item)
    return out


_resources_cache = {}
CANDIDATE_UUID = re.compile(r"\b[a-z0-9]{20,32}\b")


def coolify_resources():
    """UUIDs of applications, services and databases that Coolify knows about.

    Container labels can point at a project that no longer exists (an app that was
    recreated), so every deploy candidate is validated against this list.
    """
    if not COOLIFY_TOKEN:
        return {}
    now = time.time()
    with LOCK:
        hit = _resources_cache.get("data")
        if hit and now - hit[0] < TAGS_TTL:
            return hit[1]
    found, ok = {}, 0
    for kind in ("applications", "services", "databases"):
        request = urllib.request.Request(
            f"{COOLIFY_API_URL}/api/v1/{kind}",
            headers={"Authorization": f"Bearer {COOLIFY_TOKEN}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                payload = json.load(resp)
            ok += 1
            for item in payload or []:
                if isinstance(item, dict) and item.get("uuid"):
                    found[item["uuid"]] = kind[:-1]   # application / service / database
        except Exception:
            continue
    if ok:
        with LOCK:
            _resources_cache["data"] = (now, found)
    return found


def resolve_deploy_uuid(container_name, project_label, known):
    """Find the real Coolify resource uuid for a container."""
    if project_label in known:
        return project_label
    # container names are <service>-<resource-uuid>-<timestamp>
    for candidate in CANDIDATE_UUID.findall(container_name or ""):
        if candidate in known:
            return candidate
    return None


def deployable(row):
    return bool(COOLIFY_TOKEN) and bool(row.get("deploy_uuid"))


_preview_cache = {}


def preview(uuids, force=False):
    """What each selected resource will become after a deploy.

    Cached per selection for PREVIEW_TTL seconds: reopening the dialog or
    re-clicking Update Selected does not resolve versions again. `force`
    bypasses the cache (the Refresh button in the dialog).
    """
    key = ",".join(sorted(uuids))
    now = time.time()
    with LOCK:
        hit = _preview_cache.get(key)
        if hit and not force and now - hit[0] < PREVIEW_TTL:
            payload = dict(hit[1])
            payload["cached"] = True
            payload["age"] = round(now - hit[0])
            return payload
    items = {}
    for row in STATE["rows"]:
        uuid = row.get("deploy_uuid") or ""
        if uuid not in uuids:
            continue
        entry = items.setdefault(uuid, {
            "uuid": uuid,
            "resource": row.get("resource") or uuid,
            "state": row.get("status"),
            "containers": [],
        })
        facts = version_facts(row)
        entry["containers"].append({
            "container": row["container"],
            "image": row["image"],
            "status": row.get("status"),
            "running": facts["running"],
            "next": facts["next"],
        })
    payload = {"items": [items[k] for k in uuids if k in items], "cached": False, "age": 0, "ttl": PREVIEW_TTL}
    with LOCK:
        _preview_cache[key] = (time.time(), payload)
    return payload


def config_view():
    return {
        "docker_sock": DOCKER_SOCK,
        "state_file": STATE_FILE,
        "coolify_api_url": COOLIFY_API_URL,
        "deploy_enabled": bool(COOLIFY_TOKEN),
        "workers": WORKERS,
        "timeout": CHECK_TIMEOUT,
        "tags_ttl": TAGS_TTL,
        "digest_ttl": DIGEST_TTL,
        "preview_ttl": PREVIEW_TTL,
        "versions_ttl": VERSIONS_TTL,
        "bind": f"{BIND}:{PORT}",
    }


# -------------------------------------------------------------------- http --
PAGE = r"""<!doctype html>
<html lang="en" style="color-scheme:dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0d1117">
<title>Coolify Updates Manager</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#9ba6b2;--ok:#3fb950;--upd:#d29922;--link:#58a6ff}
  *{box-sizing:border-box}
  html{overscroll-behavior:none}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
       font-variant-numeric:tabular-nums}
  a{color:var(--link)}
  :focus-visible{outline:2px solid var(--link);outline-offset:2px;border-radius:4px}
  .skip{position:absolute;left:-9999px;top:0;background:var(--card);border:1px solid var(--line);padding:8px 12px;z-index:60}
  .skip:focus{left:8px;top:8px}
  header{position:sticky;top:0;background:#0d1117f2;backdrop-filter:blur(6px);border-bottom:1px solid var(--line);
         padding:14px 20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:5}
  h1{font-size:15px;margin:0;font-weight:600;text-wrap:balance}
  .dim{color:var(--dim)}
  .tag{display:inline-block;padding:0 6px;border-radius:4px;font-size:11px;letter-spacing:.03em;text-transform:uppercase;
       border:1px solid var(--line);color:var(--dim)}
  .tag.floating{color:var(--upd);border-color:#d2992255}
  .tag.major{color:#79c0ff;border-color:#79c0ff55}
  .tag.minor{color:#a5d6ff;border-color:#a5d6ff44}
  .tag.exact{color:var(--ok);border-color:#3fb95055}
  .tag.digest,.tag.custom{color:var(--dim)}
  button{background:#21262d;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 12px;font:inherit;
         cursor:pointer;touch-action:manipulation;-webkit-tap-highlight-color:#ffffff1a}
  button:hover:not(:disabled){background:#30363d;border-color:#484f58}
  button:active:not(:disabled){background:#282e36}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.primary{background:#1f6feb;border-color:#1f6feb}
  button.primary:hover:not(:disabled){background:#388bfd;border-color:#388bfd}
  select{background:#21262d;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit;
         touch-action:manipulation}
  main{padding:16px 20px 70px;scroll-margin-top:70px}
  #banner{display:none;margin:0 20px 14px;padding:8px 12px;border:1px solid #d2992255;background:#d299221a;
          color:var(--upd);border-radius:6px;font-size:13px}
  table{border-collapse:collapse;width:100%}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
  th{color:var(--dim);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  /* the sticky offset belongs to the page header, so it only applies to the main list */
  main th{position:sticky;top:57px;background:var(--bg);z-index:1}
  tr.upd td{background:#d299220d}
  code{color:#a5d6ff;overflow-wrap:anywhere}
  .sub{display:block;color:var(--dim);font-size:12px;overflow-wrap:anywhere}
  .pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:12px;border:1px solid}
  .p-UPDATE{color:var(--upd);border-color:#d2992255;background:#d299221a}
  .p-ok{color:var(--ok);border-color:#3fb95055;background:#3fb9501a}
  .p-\?\?{color:var(--dim);border-color:var(--line);background:var(--card)}
  .pill.ok{color:var(--ok);border-color:#3fb95055;background:#3fb9501a}
  .pill.err{color:#f85149;border-color:#f8514955;background:#f851491a}
  .pill.upd{color:var(--upd);border-color:#d2992255;background:#d299221a}
  .row-btn{padding:3px 10px;font-size:12px;min-width:96px}
  #deployAll{min-width:196px}
  .spin{display:inline-block;width:11px;height:11px;margin-right:7px;vertical-align:-1px;
        border:2px solid currentColor;border-right-color:transparent;border-radius:50%;
        animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  #toast{position:fixed;right:16px;bottom:16px;max-width:640px;background:var(--card);border:1px solid var(--line);
         border-radius:8px;padding:12px 14px;font-size:13px;display:none;white-space:pre-wrap;z-index:40;
         overflow-wrap:anywhere}
  #toast.err{border-color:#f85149}
  dialog{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:0;
         max-width:920px;width:calc(100% - 40px);max-height:85vh;overflow:hidden;overscroll-behavior:contain}
  dialog::backdrop{background:#0d1117cc}
  dialog .box{display:flex;flex-direction:column;min-height:0;max-height:calc(85vh - 2px)}
  dialog h2{margin:0;padding:14px 18px;font-size:14px;border-bottom:1px solid var(--line);font-weight:600;
            text-wrap:balance}
  dialog h3{margin:18px 18px 6px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--dim)}
  dialog .scroll{overflow:auto;min-height:0;overscroll-behavior:contain}
  dialog .scroll>div{padding-bottom:8px}
  dialog table{width:100%}
  dialog td{padding:8px 18px}
  dialog th{position:sticky;top:0;background:var(--card);z-index:1;padding:8px 18px;border-bottom:1px solid var(--line)}
  dialog .foot{display:flex;gap:10px;justify-content:flex-end;align-items:center;padding:14px 18px;
         border-top:1px solid var(--line)}
  dialog .foot .hint{margin-right:auto;color:var(--dim);font-size:12px}
  .doc{padding:0 18px;color:var(--dim);font-size:13px}
  .doc p{margin:6px 0}
  .doc dt{color:var(--fg);margin-top:10px}
  .doc dd{margin:2px 0 0 0}
  .doc code{color:#a5d6ff}
  .arrow{color:var(--dim);padding:0 6px}
  .warn{color:var(--upd)}
  input[type=search]{background:var(--card);border:1px solid var(--line);color:var(--fg);border-radius:6px;
         padding:6px 10px;font:inherit;min-width:200px}
  input[type=search]:focus-visible{border-color:var(--link)}
  label.chk{display:flex;gap:6px;align-items:center;color:var(--dim);cursor:pointer}
  footer{padding:14px 20px 30px;color:var(--dim);font-size:12px;border-top:1px solid var(--line)}
  footer b{color:var(--fg);font-weight:500}
  @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}.spin{display:none}}
  /* the header wraps on narrow screens: stop pinning both bars so the sticky
     column headers cannot overlap the focused row */
  @media (max-width:900px){header{position:static}main th{position:static}}
</style></head><body>
<a class="skip" href="#main">Skip to content</a>
<header>
  <h1>Coolify Updates Manager</h1>
  <span class="dim" id="meta">Loading…</span>
  <button id="check" type="button">Check Now</button>
  <label class="chk">Compare against
    <select id="level" name="level">
      <option value="current">current tag</option>
      <option value="major">major line</option>
      <option value="minor">minor line</option>
    </select>
  </label>
  <button class="primary" id="deployAll" type="button" disabled>Update Selected</button>
  <input type="search" id="q" name="q" autocomplete="off" spellcheck="false"
         placeholder="Filter by container, image or resource…" aria-label="Filter rows">
  <label class="chk"><input type="checkbox" id="onlyUpd" name="outdated" checked> Outdated only</label>
  <button id="deploys" type="button" aria-haspopup="dialog" aria-controls="progressDialog">Deploys</button>
  <button id="help" type="button" aria-haspopup="dialog" aria-controls="helpDialog">? Help</button>
</header>
<div id="banner" role="status"></div>
<main id="main"><table>
  <caption class="dim" style="text-align:left;padding:0 0 8px">
    One row per container. <b>In use</b> is the tag policy and version you are running;
    <b>Target</b> is the tag to write in the compose file for the selected comparison level.
  </caption>
  <thead><tr>
    <th style="width:28px"><input type="checkbox" id="all" aria-label="Select all visible rows"></th>
    <th scope="col">Resource</th><th scope="col">Image</th><th scope="col">In Use</th>
    <th scope="col" id="targetHead">Target</th><th scope="col">State</th><th scope="col">Actions</th>
  </tr></thead>
  <tbody id="rows"><tr><td colspan="7" class="dim">Loading…</td></tr></tbody>
</table></main>
<footer id="config"></footer>

<dialog id="deployDialog" aria-labelledby="mTitle">
  <div class="box">
    <h2 id="mTitle">Deploy Resources</h2>
    <div class="scroll"><table>
      <thead><tr><th scope="col">Resource</th><th scope="col">Image</th><th scope="col">Running → After Deploy</th></tr></thead>
      <tbody id="mRows"></tbody></table></div>
    <div class="foot">
      <span class="hint" id="mHint"></span>
      <button id="mRefresh" type="button">Refresh</button>
      <button id="mCancel" type="button">Cancel</button>
      <button class="primary" id="mGo" type="button">Deploy</button>
    </div>
  </div>
</dialog>

<dialog id="progressDialog" aria-labelledby="jobTitle">
  <div class="box">
    <h2 id="jobTitle">Deploy Progress</h2>
    <div class="scroll"><table>
      <thead><tr><th scope="col">Resource</th><th scope="col">State</th><th scope="col">Elapsed</th><th scope="col">Detail</th></tr></thead>
      <tbody id="jRows"><tr><td colspan="4" class="dim">Loading…</td></tr></tbody></table></div>
    <div class="foot">
      <span class="hint" id="jHint">Updates every 3s while a deploy is running.</span>
      <button id="jobClose" type="button" class="primary">Close</button>
    </div>
  </div>
</dialog>

<dialog id="helpDialog" aria-labelledby="helpTitle">
  <div class="box">
    <h2 id="helpTitle">Help</h2>
    <div class="scroll">
      <h3>Columns</h3>
      <dl class="doc">
        <dt>Resource</dt><dd>The Coolify resource that owns the container (labels <code>coolify.resourceName</code>
          and <code>com.docker.compose.project</code>). The container name sits right below it.</dd>
        <dt>Image</dt><dd>The image reference the container was created with.</dd>
        <dt>In Use</dt><dd>Tag policy plus the version reported by the image label. Policies:
          <b>floating</b> (<code>latest</code>, <code>stable</code>, <code>release</code>, or no tag — moves on its own),
          <b>major</b> (<code>v2</code>, <code>pg16</code>), <b>minor</b> (<code>18.6-alpine</code>, <code>v3.7</code>),
          <b>exact</b> (<code>4.5.3</code>), <b>digest</b>, <b>custom</b>.</dd>
        <dt>Target</dt><dd>Only shown when the comparison level is not <i>current tag</i>: the tag to write in your
          compose to pin at that level, and the version that line currently resolves to.</dd>
        <dt>State</dt><dd><b>UPDATE</b> — what you run is not what the target points to. <b>ok</b> — you are already
          on it. <b>??</b> — unknown: local build, no registry, or no versioned tag published.</dd>
      </dl>

      <h3>Actions</h3>
      <dl class="doc">
        <dt>Check Now</dt><dd>Re-reads the Docker daemon and asks each registry for the current digest of the tag.
          Nothing is pulled and nothing changes.</dd>
        <dt>update <span class="dim">(one row)</span></dt><dd>Deploys that resource right away —
          <code>POST /api/v1/deploy?uuid=…</code>. Coolify re-pulls the tag written in your compose and recreates
          the container. <b>Your compose file is not edited.</b></dd>
        <dt>exact tag <span class="dim">(one row)</span></dt><dd>Read-only: which versioned tag the floating tag points
          to today, so you can copy it into the compose.</dd>
        <dt>Update Selected</dt><dd>Same deploy, for every resource you checked. A dialog lists each container with the
          running version and the version it will be on after the pull; nothing is deployed before you confirm.</dd>
        <dt>Deploys <span class="dim">(header)</span></dt><dd>Progress of the deploys you triggered: Coolify
          deployment status for applications (queued → in progress → finished/failed, with a link) and container
          recreation for services. Refreshes every 3s while something is running.</dd>
      </dl>

      <h3>Compare against</h3>
      <div class="doc">
        <p>Changes what <b>Target</b> resolves to, and therefore what counts as <code>UPDATE</code>:</p>
        <dl>
          <dt>current tag</dt><dd>Compares the running image with the tag exactly as written in your compose
            (e.g. <code>:latest</code>).</dd>
          <dt>major line</dt><dd>Newest release inside the current major (e.g. <code>v3</code>), for pinning
            <code>v3</code> in the compose.</dd>
          <dt>minor line</dt><dd>Newest release inside the current minor (e.g. <code>v3.5</code>), the usual balance
            between stability and patches.</dd>
        </dl>
        <p>The target always comes from the newest release the registry offers, not from what you are running. If the
          project publishes only full versions, the target falls back to the exact tag and the cell says so.</p>
      </div>

      <h3>Good to Know</h3>
      <div class="doc">
        <p>Deploying never rewrites the compose. To pin a version, take it from <b>Target</b> or from
          <b>exact tag</b> and edit the compose yourself.</p>
        <p>Docker Hub limits anonymous manifest requests (100 per 6&nbsp;h per IP). A full check spends one request
          per Hub image; the footer shows the worker count and cache TTLs in use.</p>
        <p>Rows with <code>??</code> are images built locally or projects that publish a single channel with no
          versioned tags — there is nothing to compare against.</p>
      </div>
    </div>
    <div class="foot">
      <span class="hint">Everything here is also valid as a plain API call — see <code>/api/state</code>.</span>
      <button id="helpClose" type="button" class="primary">Close</button>
    </div>
  </div>
</dialog>

<div id="toast" role="status" aria-live="polite"></div>
<script>
let rows = [], state = {}, sel = new Set(), lastFocus = null, previewBusy = false;
const PREVIEW_TTL = 300;
const $ = s => document.querySelector(s);
const esc = s => (s ?? "").toString().replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const LEVEL_NAMES = {current: "current tag", major: "major line", minor: "minor line"};
const num = new Intl.NumberFormat();
const fmtTime = (epoch, fallback) => epoch
  ? new Intl.DateTimeFormat(undefined, {dateStyle: "short", timeStyle: "medium"}).format(new Date(epoch * 1000))
  : (fallback || "—");
function syncUrl() {
  const p = new URLSearchParams();
  if (state.level && state.level !== "current") p.set("level", state.level);
  if ($("#q").value.trim()) p.set("q", $("#q").value.trim());
  if (!$("#onlyUpd").checked) p.set("outdated", "0");
  const qs = p.toString();
  history.replaceState(null, "", qs ? "?" + qs : location.pathname);
}
function openDialog(el) {
  lastFocus = document.activeElement;
  el.showModal();
}
function closeDialog(el) {
  el.close();
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}
function toast(msg, err) {
  const t = $("#toast");
  t.textContent = msg; t.className = err ? "err" : "";
  t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => { t.style.display = "none"; }, 12000);
}
function canDeploy(r) {
  return Boolean(state.deploy_enabled && r.deploy_uuid);
}
function rowStatus(r) {
  if (state.level !== "current" && r.target) return r.target.status;
  return r.status;
}
function targetCell(r) {
  if (state.level === "current") return '<span class="dim">-</span>';
  if (!r.target) return '<span class="dim">pending</span>';
  if (!r.target.target_tag) return `<span class="dim">${esc(r.target.note || "n/a")}</span>`;
  const version = r.target.target_version ? `<span class="sub">${esc(r.target.target_version)}</span>` : "";
  return `<code translate="no">${esc(r.target.target_tag)}</code>${version}`;
}
function render() {
  const q = $("#q").value.toLowerCase(), only = $("#onlyUpd").checked;
  const list = rows.filter(r => {
    if (only && rowStatus(r) !== "UPDATE") return false;
    if (!q) return true;
    return (r.container + " " + r.image + " " + (r.resource || "")).toLowerCase().includes(q);
  });
  $("#rows").innerHTML = list.length ? list.map(r => {
    const can = canDeploy(r), id = esc(r.container), st = rowStatus(r);
    const running = r.version || r.created || "";
    return `<tr class="${st === "UPDATE" ? "upd" : ""}">
      <td>${can ? `<input type="checkbox" data-c="${id}" ${sel.has(r.container) ? "checked" : ""}
             aria-label="Select ${id}">` : ""}</td>
      <td>${can ? `<code translate="no">${esc(r.resource || r.project)}</code>` : `<span class="dim">${esc(r.resource || "—")}</span>`}
          <span class="sub">${id}</span></td>
      <td><code translate="no">${esc(r.image)}</code></td>
      <td><span class="tag ${esc(r.policy)}">${esc(r.policy)}</span>
          <span class="sub">${esc(running) || "—"}</span></td>
      <td>${targetCell(r)}</td>
      <td><span class="pill p-${esc(st)}">${esc(st)}</span></td>
      <td>${can
        ? `<button class="row-btn" type="button" data-deploy="${esc(r.deploy_uuid)}"
             title="Deploy now: re-pull the compose tag and recreate the container (the compose file is not edited)">Update</button>
           <button class="row-btn" type="button" data-pin="${id}"
             title="Which versioned tag the floating tag points to today">Exact Tag</button>`
        : `<span class="dim">${esc(r.note || "not deployable")}</span>`}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="dim">No rows match the current filter.</td></tr>`;
  const outdated = rows.filter(r => rowStatus(r) === "UPDATE").length;
  $("#targetHead").textContent = state.level === "current" ? "Target" : "Target (" + LEVEL_NAMES[state.level] + ")";
  $("#meta").textContent = `${num.format(outdated)} of ${num.format(rows.length)} outdated · checked ${fmtTime(state.checked_at_epoch, state.checked_at)} (${state.duration ?? "?"}s)`;
  setDeployAllState();
}
function setDeployAllState() {
  const btn = $("#deployAll");
  if (previewBusy) {
    // only rewrite once: re-rendering on every state poll restarts the spinner
    if (btn.dataset.busy !== "1") {
      btn.dataset.busy = "1";
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.innerHTML = `<span class="spin" aria-hidden="true"></span>Resolving…`;
    }
    return;
  }
  if (btn.dataset.busy === "1") {
    delete btn.dataset.busy;
    btn.removeAttribute("aria-busy");
  }
  btn.disabled = sel.size === 0 || !state.deploy_enabled;
  btn.textContent = sel.size ? `Update Selected (${sel.size})` : "Update Selected";
}
function renderConfig() {
  const c = state.config || {};
  $("#config").innerHTML =
    `<b>config</b> &nbsp; docker=<code>${esc(c.docker_sock)}</code> ` +
    `&nbsp; coolify=<code>${esc(c.coolify_api_url || "-")}</code> ` +
    `&nbsp; deploy=<b>${c.deploy_enabled ? "enabled" : "disabled"}</b> ` +
    `&nbsp; state=<code>${esc(c.state_file)}</code> ` +
    `&nbsp; workers=${c.workers} timeout=${c.timeout}s ttl tags=${c.tags_ttl}s digest=${c.digest_ttl}s ` +
    `preview=${c.preview_ttl}s versions=${c.versions_ttl}s ` +
    `&nbsp; ${state.warming ? `warming versions ${(state.warm || {}).done || 0}/${(state.warm || {}).total || 0}` : "versions warm"} ` +
    `&nbsp; v${esc(state.version)}`;
}
async function fetchState() {
  try {
    state = await (await fetch("api/state")).json();
    rows = state.rows || [];
    if ($("#level").value !== state.level) $("#level").value = state.level;
    if (state.deploy_hint) { $("#banner").textContent = state.deploy_hint; $("#banner").style.display = "block"; }
    render(); renderConfig(); syncUrl();
  } catch (e) { toast("Could not read state: " + e, true); }
  const busy = state.checking || state.resolving;
  if (state.checking) { $("#check").disabled = true; $("#check").textContent = "Checking…"; }
  else { $("#check").disabled = false; $("#check").textContent = "Check Now"; }
  if (busy) setTimeout(fetchState, 2500);
}
const progressDialog = $("#progressDialog");
let progressTimer = null;
const TERMINAL = ["finished", "failed", "cancelled", "error", "timeout"];

function jobClass(status) {
  const st = (status || "").toLowerCase();
  if (st === "finished") return "ok";
  if (st === "failed" || st === "cancelled" || st === "error" || st === "timeout") return "err";
  return "upd";
}
function renderJobs(jobs) {
  $("#jRows").innerHTML = jobs.length ? jobs.map(j => {
    const detail = j.message ? esc(j.message) : "";
    const link = j.link ? ` <a href="${esc(j.link)}" target="_blank" rel="noreferrer noopener">open in Coolify</a>` : "";
    return `<tr>
      <td><code translate="no">${esc(j.resource)}</code><span class="sub">${esc(j.kind)} · started ${esc(j.started_at)}</span></td>
      <td><span class="pill ${jobClass(j.status)}">${esc(j.status)}</span></td>
      <td>${num.format(j.elapsed || 0)}s</td>
      <td class="dim">${detail}${link}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="4" class="dim">No deploys in this session yet.</td></tr>`;
  const active = jobs.filter(j => !TERMINAL.includes((j.status || "").toLowerCase())).length;
  $("#jHint").textContent = active
    ? `${active} deploy(s) still running — refreshing every 3s.`
    : "Updates every 3s while a deploy is running.";
}
async function refreshJobs() {
  clearTimeout(progressTimer);
  try {
    const data = await (await fetch("api/deploys")).json();
    const jobs = data.jobs || [];
    renderJobs(jobs);
    const active = jobs.some(j => !TERMINAL.includes((j.status || "").toLowerCase()));
    if (active) progressTimer = setTimeout(refreshJobs, 3000);
  } catch (e) {
    toast("Could not read deploy progress: " + e, true);
    progressTimer = setTimeout(refreshJobs, 6000);
  }
}
function openProgress() {
  openDialog(progressDialog);
  refreshJobs();
}
$("#deploys").onclick = openProgress;
$("#jobClose").onclick = () => { clearTimeout(progressTimer); closeDialog(progressDialog); };
progressDialog.addEventListener("close", () => clearTimeout(progressTimer));

async function doDeploy(uuids, btn) {
  toast("Deploying " + uuids.length + " resource(s)…");
  if (btn) {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.innerHTML = `<span class="spin" aria-hidden="true"></span>`;
  }
  const res = await fetch("api/deploy", {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify({uuids})});
  const data = await res.json();
  const results = data.results || [];
  const failed = results.filter(x => !x.ok);
  const ok = results.length - failed.length;
  toast(failed.length
    ? `Deploy failed for ${failed.length} of ${results.length} — see the Deploy Progress dialog.\n\n` +
      failed.map(x => `${x.resource || x.uuid}: ${x.message}`).join("\n")
    : `Deploy triggered for ${ok} resource(s). Follow it in Deploy Progress.`,
    failed.length > 0);
  if (btn) {
    btn.removeAttribute("aria-busy");
    btn.disabled = false;
    btn.textContent = "Update";
  }
  openProgress();
}
$("#check").onclick = async () => { $("#check").disabled = true; await fetch("api/check", {method: "POST"}); setTimeout(fetchState, 1200); };
$("#level").onchange = async e => {
  await fetch("api/level", {method: "POST", headers: {"Content-Type": "application/json"},
                            body: JSON.stringify({level: e.target.value})});
  setTimeout(fetchState, 800);
};
$("#q").oninput = () => { render(); syncUrl(); };
$("#onlyUpd").onchange = () => { render(); syncUrl(); };
$("#all").onchange = e => {
  sel.clear();
  if (e.target.checked) document.querySelectorAll("[data-c]").forEach(c => sel.add(c.dataset.c));
  render();
};
document.addEventListener("change", e => {
  const name = e.target.dataset && e.target.dataset.c;
  if (!name) return;
  e.target.checked ? sel.add(name) : sel.delete(name);
  setDeployAllState();
});
document.addEventListener("click", async e => {
  const deployBtn = e.target.closest("[data-deploy]");
  if (deployBtn) return doDeploy([deployBtn.dataset.deploy], deployBtn);
  const pinBtn = e.target.closest("[data-pin]");
  if (!pinBtn) return;
  const name = pinBtn.dataset.pin;
  pinBtn.disabled = true; pinBtn.textContent = "…";
  try {
    const res = await fetch("api/pin", {method: "POST", headers: {"Content-Type": "application/json"},
                                        body: JSON.stringify({container: name})});
    const pin = await res.json();
    toast(pin.error
      ? `${name}: ${pin.error}. Try Check Now first — pin only works on a container from the last check.`
      : `${pin.image}\n\nExact tag for the current digest: ${pin.matches.length ? pin.matches.join("  |  ") : "none (project publishes channels only)"}\nDigest: ${pin.digest}\n\nCopy that tag into your compose if you want to pin it.`);
  } catch (e) { toast("Could not resolve the exact tag: " + e, true); }
  finally { pinBtn.disabled = false; pinBtn.textContent = "Exact Tag"; }
});

const deployDialog = $("#deployDialog"), helpDialog = $("#helpDialog");
$("#help").onclick = () => openDialog(helpDialog);
$("#helpClose").onclick = () => closeDialog(helpDialog);
helpDialog.addEventListener("close", () => { if (lastFocus && lastFocus.focus) lastFocus.focus(); });
deployDialog.addEventListener("close", () => { if (lastFocus && lastFocus.focus) lastFocus.focus(); });
function fillDeployDialog(data) {
  const items = data.items || [];
  $("#mRows").innerHTML = items.map(it => it.containers.map(c => {
    const from = c.running || "unknown";
    const to = c.next || "unknown";
    const same = from && to && from === to;
    return `<tr>
      <td><code translate="no">${esc(it.resource)}</code><span class="sub">${esc(c.container)}</span></td>
      <td><code translate="no">${esc(c.image)}</code></td>
      <td>${esc(from)}<span class="arrow">→</span><b>${esc(to)}</b>
          ${same ? '<span class="warn"> (same version, container recreated only)</span>' : ''}</td>
    </tr>`;
  }).join("")).join("");
  const n = items.length;
  $("#mTitle").textContent = `Deploy ${n} Resource${n > 1 ? "s" : ""}?`;
  $("#mGo").textContent = `Deploy ${n} Resource${n > 1 ? "s" : ""}`;
  const when = data.cached
    ? `Preview reused from ${data.age}s ago (kept for ${data.ttl || PREVIEW_TTL}s).`
    : `Preview computed now (kept for ${data.ttl || PREVIEW_TTL}s).`;
  $("#mHint").textContent = `${when} Resources with several containers deploy as a whole; ` +
    "Coolify re-pulls the compose tag and the compose file is not changed.";
  deployDialog.dataset.uuids = JSON.stringify(items.map(i => i.uuid));
}
function openDeployDialog(data) {
  fillDeployDialog(data);
  openDialog(deployDialog);
  $("#mGo").focus();
}
$("#mRefresh").onclick = async () => {
  const uuids = JSON.parse(deployDialog.dataset.uuids || "[]");
  if (!uuids.length) return;
  const btn = $("#mRefresh");
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  btn.innerHTML = `<span class="spin" aria-hidden="true"></span>Refresh`;
  try {
    const res = await fetch("api/preview", {method: "POST", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify({uuids, force: true})});
    const data = await res.json();
    if (data.items && data.items.length) fillDeployDialog(data);
    else toast("Nothing to deploy from the current selection.", true);
  } catch (e) {
    toast("Could not refresh the preview: " + e, true);
  } finally {
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
    btn.textContent = "Refresh";
  }
};
$("#mCancel").onclick = () => closeDialog(deployDialog);
$("#mGo").onclick = () => {
  const uuids = JSON.parse(deployDialog.dataset.uuids || "[]");
  closeDialog(deployDialog);
  doDeploy(uuids);
};
$("#deployAll").onclick = async () => {
  if (previewBusy) return;
  const uuids = [...new Set(rows.filter(r => sel.has(r.container) && canDeploy(r)).map(r => r.deploy_uuid))];
  if (!uuids.length) return;
  previewBusy = true;
  setDeployAllState();
  try {
    const res = await fetch("api/preview", {method: "POST", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify({uuids})});
    const data = await res.json();
    if (!data.items || !data.items.length) {
      toast("Nothing to deploy from the current selection.", true);
    } else {
      openDeployDialog(data);
    }
  } catch (e) {
    toast("Could not build the preview: " + e, true);
  } finally {
    previewBusy = false;
    setDeployAllState();
  }
};

// state in the URL: ?level=minor&q=seerr&outdated=0
const params = new URLSearchParams(location.search);
if (params.get("q")) $("#q").value = params.get("q");
if (params.get("outdated") === "0") $("#onlyUpd").checked = false;
const initialLevel = params.get("level");
if (initialLevel && ["current", "major", "minor"].includes(initialLevel)) {
  $("#level").value = initialLevel;
}
fetchState().then(() => {
  if (initialLevel && state.level !== initialLevel) {
    fetch("api/level", {method: "POST", headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({level: initialLevel})}).then(() => setTimeout(fetchState, 800));
  }
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _fail(self, code, message):
        self._send(code, json.dumps({"error": message}))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            return self._send(200, json.dumps({"ok": True, "version": VERSION, "checking": CHECKING.is_set()}))
        if path == "/":
            return self._send(200, PAGE, "text/html")
        if path == "/api/state":
            payload = dict(
                STATE,
                checking=CHECKING.is_set(),
                resolving=RESOLVING.is_set(),
                warming=WARMING.is_set(),
                warm=dict(WARM),
                deploy_enabled=bool(COOLIFY_TOKEN),
                deploy_hint=DEPLOY_HINT,
                deploy_log=DEPLOY_LOG,
                levels=list(LEVELS),
                config=config_view(),
                version=VERSION,
            )
            return self._send(200, json.dumps(payload))
        if path == "/api/deploys":
            try:
                return self._send(200, json.dumps({"jobs": deploy_jobs()}))
            except Exception as exc:  # noqa: BLE001
                return self._fail(502, f"{type(exc).__name__}: {exc}")
        return self._fail(404, "not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}

        if path == "/api/check":
            started = run_check()
            return self._send(202 if started else 409, json.dumps({"started": started}))

        if path == "/api/level":
            level = (body.get("level") or "").strip()
            if level not in LEVELS:
                return self._fail(400, f"level must be one of {', '.join(LEVELS)}")
            changed = set_level(level)
            return self._send(200, json.dumps({"level": STATE["level"], "changed": changed}))

        if path in ("/api/deploy", "/api/preview"):
            uuids = body.get("uuids") or ([body["uuid"]] if body.get("uuid") else [])
            uuids = [u for u in uuids if isinstance(u, str) and re.fullmatch(r"[a-z0-9]{20,32}", u)]
            if not uuids:
                return self._fail(400, "provide a valid uuid list")
            if path == "/api/preview":
                try:
                    return self._send(200, json.dumps(preview(uuids, force=bool(body.get("force")))))
                except Exception as exc:  # noqa: BLE001
                    return self._fail(502, f"{type(exc).__name__}: {exc}")
            if not COOLIFY_TOKEN:
                return self._fail(503, DEPLOY_HINT)
            return self._send(200, json.dumps({"results": deploy(uuids)}))

        if path == "/api/pin":
            name = body.get("container") or ""
            row = next((r for r in STATE["rows"] if r["container"] == name), None)
            if not row:
                return self._fail(404, "container not found in the last check result")
            with LOCK:
                cached = STATE["pins"].get(row["image"])
            if cached and cached.get("digest"):
                return self._send(200, json.dumps(cached))
            try:
                pin = suggest_pin(row["image"])
            except Exception as exc:  # noqa: BLE001
                return self._fail(502, f"{type(exc).__name__}: {exc}")
            with LOCK:
                STATE["pins"][row["image"]] = pin
            save_state()
            return self._send(200, json.dumps(pin))

        return self._fail(404, "not found")


if __name__ == "__main__":
    load_state()
    if STATE["rows"]:
        warm_versions()   # cached rows from a previous run: warm the version data
    print(f"{APP_NAME} {VERSION} listening on http://{BIND}:{PORT} | coolify={COOLIFY_API_URL or '-'} "
          f"| state={STATE_FILE} | level={STATE['level']}", flush=True)
    if DEPLOY_HINT:
        print(f"warn: {DEPLOY_HINT}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
