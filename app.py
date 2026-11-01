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

VERSION = "2.0.0"
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
        if item["project"] == "source":
            note = "coolify internal stack"
        elif not item["project"]:
            note = "no coolify label"
        elif status == "??":
            note = "local build / no registry"
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


def run_check():
    if CHECKING.is_set():
        return False
    CHECKING.set()

    def work():
        started = time.time()
        try:
            rows, counts = collect()
            STATE.update(
                checked_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                duration=round(time.time() - started, 1),
                rows=rows,
                counts=counts,
                error=None,
            )
            if STATE["level"] != "current":
                resolve_rows(STATE["level"])
        except Exception as exc:  # noqa: BLE001
            STATE.update(error=f"{type(exc).__name__}: {exc}")
        save_state()
        CHECKING.clear()

    threading.Thread(target=work, daemon=True).start()
    return True


def set_level(level):
    if level not in LEVELS or level == STATE["level"]:
        return False
    if RESOLVING.is_set():
        return False
    STATE["level"] = level
    save_state()
    if level == "current" or not STATE["rows"]:
        return True
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


# ------------------------------------------------------------------ deploy --
def deploy(uuids):
    results = []
    for uuid in uuids:
        entry = {"uuid": uuid, "ok": False, "status": 0, "message": ""}
        if not COOLIFY_TOKEN:
            entry["message"] = DEPLOY_HINT
            results.append(entry)
            continue
        url = f"{COOLIFY_API_URL}/api/v1/deploy?uuid={urllib.parse.quote(uuid)}&force=false"
        request = urllib.request.Request(url, method="POST", headers={
            "Authorization": f"Bearer {COOLIFY_TOKEN}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=90) as resp:
                entry.update(ok=True, status=resp.status, message=resp.read().decode("utf-8", "replace")[:400])
        except urllib.error.HTTPError as exc:
            entry.update(status=exc.code, message=exc.read().decode("utf-8", "replace")[:400])
        except Exception as exc:  # noqa: BLE001
            entry["message"] = f"{type(exc).__name__}: {exc}"
        with LOCK:
            DEPLOY_LOG[uuid] = {"at": time.strftime("%H:%M:%S"), "ok": entry["ok"], "message": entry["message"][:200]}
        results.append(entry)
    return results


def deployable(row):
    project = row.get("project") or ""
    return bool(COOLIFY_TOKEN) and bool(re.fullmatch(r"[a-z0-9]{20,32}", project)) and project != "source"


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
        "bind": f"{BIND}:{PORT}",
    }


# -------------------------------------------------------------------- http --
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>coolify updates manager</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;--ok:#3fb950;--upd:#d29922}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
  header{position:sticky;top:0;background:#0d1117ee;backdrop-filter:blur(6px);border-bottom:1px solid var(--line);
         padding:14px 20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:5}
  h1{font-size:15px;margin:0;font-weight:600}
  .dim{color:var(--dim)}
  .tag{display:inline-block;padding:0 6px;border-radius:4px;font-size:11px;letter-spacing:.03em;text-transform:uppercase;
       border:1px solid var(--line);color:var(--dim)}
  .tag.floating{color:#d29922;border-color:#d2992255}
  .tag.major{color:#79c0ff;border-color:#79c0ff55}
  .tag.minor{color:#a5d6ff;border-color:#a5d6ff44}
  .tag.exact{color:#3fb950;border-color:#3fb95055}
  .tag.digest,.tag.custom{color:#8b949e}
  button{background:#21262d;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 12px;font:inherit;cursor:pointer}
  button:hover:not(:disabled){background:#30363d}
  button:disabled{opacity:.45;cursor:default}
  button.primary{background:#1f6feb;border-color:#1f6feb}
  button.primary:hover:not(:disabled){background:#388bfd}
  select{background:#21262d;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
  main{padding:16px 20px 70px}
  #banner{display:none;margin:0 20px 14px;padding:8px 12px;border:1px solid #d2992255;background:#d299221a;
          color:var(--upd);border-radius:6px;font-size:13px}
  table{border-collapse:collapse;width:100%}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
  th{color:var(--dim);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.04em;
     position:sticky;top:57px;background:var(--bg)}
  tr.upd td{background:#d299220d}
  code{color:#a5d6ff}
  .sub{display:block;color:var(--dim);font-size:12px}
  .pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:12px;border:1px solid}
  .p-UPDATE{color:var(--upd);border-color:#d2992255;background:#d299221a}
  .p-ok{color:var(--ok);border-color:#3fb95055;background:#3fb9501a}
  .p-\?\?{color:var(--dim);border-color:#30363d;background:#161b22}
  .row-btn{padding:3px 10px;font-size:12px}
  #toast{position:fixed;right:16px;bottom:16px;max-width:640px;background:var(--card);border:1px solid var(--line);
         border-radius:8px;padding:12px 14px;font-size:13px;display:none;white-space:pre-wrap;z-index:20}
  #toast.err{border-color:#f85149}
  input[type=search]{background:var(--card);border:1px solid var(--line);color:var(--fg);border-radius:6px;
         padding:6px 10px;font:inherit;min-width:200px}
  label.chk{display:flex;gap:6px;align-items:center;color:var(--dim);cursor:pointer}
  footer{padding:14px 20px 30px;color:var(--dim);font-size:12px;border-top:1px solid var(--line)}
  footer b{color:#e6edf3;font-weight:500}
</style></head><body>
<header>
  <h1>coolify updates manager</h1>
  <span class="dim" id="meta">loading...</span>
  <button id="check">Check now</button>
  <label class="chk">compare against
    <select id="level">
      <option value="current">current tag</option>
      <option value="major">major line</option>
      <option value="minor">minor line</option>
    </select>
  </label>
  <button class="primary" id="deployAll" disabled>Update selected</button>
  <input type="search" id="q" placeholder="filter container / image / resource">
  <label class="chk"><input type="checkbox" id="onlyUpd" checked> outdated only</label>
</header>
<div id="banner"></div>
<main><table>
  <thead><tr>
    <th style="width:28px"><input type="checkbox" id="all"></th>
    <th>resource</th><th>image</th><th>in use</th><th id="targetHead">target</th><th>state</th><th>actions</th>
  </tr></thead>
  <tbody id="rows"><tr><td colspan="7" class="dim">loading...</td></tr></tbody>
</table></main>
<footer id="config"></footer>
<div id="toast"></div>
<script>
let rows = [], state = {}, sel = new Set();
const $ = s => document.querySelector(s);
const esc = s => (s ?? "").toString().replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const LEVEL_NAMES = {current: "current tag", major: "major line", minor: "minor line"};
function toast(msg, err) {
  const t = $("#toast");
  t.textContent = msg; t.style.display = "block"; t.className = err ? "err" : "";
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 12000);
}
function canDeploy(r) {
  return state.deploy_enabled && /^[a-z0-9]{20,32}$/.test(r.project || "") && r.project !== "source";
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
  return `<code>${esc(r.target.target_tag)}</code>${version}`;
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
      <td>${can ? `<input type="checkbox" data-c="${id}" ${sel.has(r.container) ? "checked" : ""}>` : ""}</td>
      <td>${can ? `<code>${esc(r.resource || r.project)}</code>` : `<span class="dim">${esc(r.resource || "-")}</span>`}
          <span class="sub">${id}</span></td>
      <td><code>${esc(r.image)}</code></td>
      <td><span class="tag ${esc(r.policy)}">${esc(r.policy)}</span>
          <span class="sub">${esc(running) || "-"}</span></td>
      <td>${targetCell(r)}</td>
      <td><span class="pill p-${esc(st)}">${esc(st)}</span></td>
      <td>${can
        ? `<button class="row-btn" data-deploy="${esc(r.project)}">update</button>
           <button class="row-btn" data-pin="${id}">exact tag</button>`
        : `<span class="dim">${esc(r.note || "not deployable")}</span>`}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="dim">nothing here.</td></tr>`;
  const outdated = rows.filter(r => rowStatus(r) === "UPDATE").length;
  $("#targetHead").textContent = state.level === "current" ? "target" : "target (" + LEVEL_NAMES[state.level] + ")";
  $("#meta").textContent = `${outdated} of ${rows.length} outdated - checked ${state.checked_at || "-"} (${state.duration ?? "?"}s)`;
  $("#deployAll").disabled = sel.size === 0 || !state.deploy_enabled;
  $("#deployAll").textContent = sel.size ? `Update selected (${sel.size})` : "Update selected";
}
function renderConfig() {
  const c = state.config || {};
  $("#config").innerHTML =
    `<b>config</b> &nbsp; docker=<code>${esc(c.docker_sock)}</code> ` +
    `&nbsp; coolify=<code>${esc(c.coolify_api_url || "-")}</code> ` +
    `&nbsp; deploy=<b>${c.deploy_enabled ? "enabled" : "disabled"}</b> ` +
    `&nbsp; state=<code>${esc(c.state_file)}</code> ` +
    `&nbsp; workers=${c.workers} timeout=${c.timeout}s ttl tags=${c.tags_ttl}s digest=${c.digest_ttl}s ` +
    `&nbsp; v${esc(state.version)}`;
}
async function fetchState() {
  try {
    state = await (await fetch("api/state")).json();
    rows = state.rows || [];
    if ($("#level").value !== state.level) $("#level").value = state.level;
    if (state.deploy_hint) { $("#banner").textContent = state.deploy_hint; $("#banner").style.display = "block"; }
    render(); renderConfig();
  } catch (e) { toast("could not read state: " + e, true); }
  const busy = state.checking || state.resolving;
  if (state.checking) { $("#check").disabled = true; $("#check").textContent = "checking..."; }
  else { $("#check").disabled = false; $("#check").textContent = "Check now"; }
  if (busy) setTimeout(fetchState, 2500);
}
async function doDeploy(uuids) {
  toast("triggering deploy: " + uuids.join(", "));
  const res = await fetch("api/deploy", {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify({uuids})});
  const data = await res.json();
  const failed = (data.results || []).filter(x => !x.ok);
  toast((data.results || []).map(x => `${x.uuid}: ${x.ok ? "ok" : "failed"} (${x.status}) ${x.message}`).join("\n\n"),
        failed.length > 0);
}
$("#check").onclick = async () => { $("#check").disabled = true; await fetch("api/check", {method: "POST"}); setTimeout(fetchState, 1200); };
$("#level").onchange = async e => {
  await fetch("api/level", {method: "POST", headers: {"Content-Type": "application/json"},
                            body: JSON.stringify({level: e.target.value})});
  setTimeout(fetchState, 800);
};
$("#q").oninput = render;
$("#onlyUpd").onchange = render;
$("#all").onchange = e => {
  sel.clear();
  if (e.target.checked) document.querySelectorAll("[data-c]").forEach(c => sel.add(c.dataset.c));
  render();
};
document.addEventListener("change", e => {
  const name = e.target.dataset && e.target.dataset.c;
  if (!name) return;
  e.target.checked ? sel.add(name) : sel.delete(name);
  $("#deployAll").disabled = sel.size === 0 || !state.deploy_enabled;
  $("#deployAll").textContent = sel.size ? `Update selected (${sel.size})` : "Update selected";
});
document.addEventListener("click", async e => {
  const deployBtn = e.target.closest("[data-deploy]");
  if (deployBtn) return doDeploy([deployBtn.dataset.deploy]);
  const pinBtn = e.target.closest("[data-pin]");
  if (!pinBtn) return;
  const name = pinBtn.dataset.pin;
  pinBtn.disabled = true; pinBtn.textContent = "...";
  try {
    const res = await fetch("api/pin", {method: "POST", headers: {"Content-Type": "application/json"},
                                        body: JSON.stringify({container: name})});
    const pin = await res.json();
    toast(pin.error ? `${name}: ${pin.error}`
                    : `${name}\n${pin.image}\n\nexact tag for the current digest: ${pin.matches.length ? pin.matches.join("  |  ") : "none (project publishes channels only)"}\n(digest ${pin.digest})`);
  } finally { pinBtn.disabled = false; pinBtn.textContent = "exact tag"; }
});
$("#deployAll").onclick = () => {
  const uuids = [...new Set(rows.filter(r => sel.has(r.container) && canDeploy(r)).map(r => r.project))];
  if (uuids.length && confirm("Deploy " + uuids.length + " resource(s) in Coolify?\n\n" + uuids.join("\n"))) doDeploy(uuids);
};
fetchState();
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
                deploy_enabled=bool(COOLIFY_TOKEN),
                deploy_hint=DEPLOY_HINT,
                deploy_log=DEPLOY_LOG,
                levels=list(LEVELS),
                config=config_view(),
                version=VERSION,
            )
            return self._send(200, json.dumps(payload))
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

        if path == "/api/deploy":
            if not COOLIFY_TOKEN:
                return self._fail(503, DEPLOY_HINT)
            uuids = body.get("uuids") or ([body["uuid"]] if body.get("uuid") else [])
            uuids = [u for u in uuids if isinstance(u, str) and re.fullmatch(r"[a-z0-9]{20,32}", u)]
            if not uuids:
                return self._fail(400, "provide a valid uuid list")
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
    print(f"{APP_NAME} {VERSION} listening on http://{BIND}:{PORT} | coolify={COOLIFY_API_URL or '-'} "
          f"| state={STATE_FILE} | level={STATE['level']}", flush=True)
    if DEPLOY_HINT:
        print(f"warn: {DEPLOY_HINT}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
