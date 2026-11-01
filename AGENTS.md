# AGENTS.md — notes for contributors (human or agent)

Read this before touching `app.py`. It documents the parts that are easy to break
and the Coolify/Docker behaviours that took real debugging to discover.

## What this is

One-file Python app (standard library only) that:

1. lists every container on a Docker host and compares the digest of the running
   image with the digest the registry reports for its tag;
2. suggests the tag to write in a compose file for a chosen granularity
   (current / major / minor line) and says whether that line is newer;
3. deploys the matching resource on Coolify, with a progress view.

`app.py` is the whole program. There is no framework, no ORM and **no external
dependency** — keep it that way (an `import` that needs pip is a rejected PR).
It serves an inline HTML page; the front end is vanilla JS in the `PAGE` string.

## Layout of app.py

| section | responsibility |
|---|---|
| `docker_*` | Docker API over the unix socket (`GET` for reads, `POST /images/create` to pull) |
| registry helpers | `split_ref`, token dance, `registry_digest`, `registry_tags`, `suggest_pin` |
| version helpers | `parse_version`, `classify_tag`, `version_running`, `version_after_pull` |
| `collect` | builds `STATE["rows"]`, one row per container |
| `resolve_level` | fills `row["target"]` for the selected level |
| `deploy` / `_run_deploy_job` | pull + Coolify deploy + job bookkeeping |
| `poll_deploy_jobs` | job status (Coolify API for apps, container recreation for services) |
| `Handler` | HTTP: `/`, `/healthz`, `/api/state`, `/api/check`, `/api/level`, `/api/preview`, `/api/deploy`, `/api/deploys`, `/api/pin` |
| `PAGE` | the entire front end |

## Core concepts

**Digest, not version string.** "Outdated" means the running image's
`RepoDigests` do not contain the digest the registry returns for the tag today.
Never compare version strings to decide updates.

**Tag policy** (`classify_tag`): `floating` (`latest`, `stable`, `release`, no
tag), `major` (`v2`, `pg16`), `minor` (`18.6-alpine`, `v3.7`), `exact` (`4.5.3`),
`digest`, `custom`. Shown in the *in use* column.

**Levels** (`current`, `major`, `minor`): the target column answers "which tag do
I write in my compose to pin at this granularity". Targets come from the **newest
release the registry offers**, not from what is running — otherwise pinning to
the current line would always look up to date. If the project publishes only full
versions, the target falls back to the exact tag and the cell says so.

**Pins** (`suggest_pin`): finds the versioned tag that shares the digest of a
floating tag. Expensive (tag list + a handful of `HEAD`s), so results are cached
in `STATE["pins"]` and persisted.

## Caches — and the rules around them

| cache | TTL | invalidated by |
|---|---|---|
| `_tags_cache` (tag list per repo) | `UPDATES_TAGS_TTL` (6 h) | process restart |
| `_digest_cache` (manifest digest) | `UPDATES_DIGEST_TTL` (15 min) | process restart |
| `STATE["pins"]` (exact tag per image) | none (persisted) | `schema` bump, `?force` on preview |
| `_versions_cache` (running/next per image) | `UPDATES_VERSIONS_TTL` (5 min) | deploy, `schema` bump |
| `_preview_cache` (deploy dialog per selection) | `UPDATES_PREVIEW_TTL` (5 min) | check, deploy, `{"force": true}` |

Rules:

- **Never block an HTTP handler on registry work.** Anything slow (level resolve,
  version warm, deploy) runs in a thread and reports progress through
  `STATE`/`RESOLVING`/`WARMING`/`DEPLOY_JOBS`. The UI polls.
- `run_check()` saves state and clears `CHECKING` **before** the slow parts, so
  the table is usable while targets and versions are still being resolved.
- Caches are keyed by content (`image + tag + digests`), so a recreated container
  invalidates itself.

## Concurrency

- `LOCK` guards `STATE`, `DEPLOY_JOBS` and the caches. Do not call anything that
  can block (network, docker) while holding it.
- `SAVE_LOCK` serialises `save_state()`, which writes a temp file per thread and
  `os.replace`s it. Skipping that causes `could not write state file` races.
- If you change the row shape (add/rename a field), **bump `STATE["schema"]`**.
  On load, rows from an older schema are dropped and a check runs automatically —
  otherwise the UI serves rows missing the new field.
- Published state is a file (`/data/state.json` by default): treat it as a cache,
  never as the source of truth.

## Dependencies: why both the socket and the token

Do not "simplify" this away — the two credentials cover disjoint halves of the
problem, and dropping either one silently degrades the product.

| need | socket | token |
|---|---|---|
| digest of the **running** image (the whole point of the `UPDATE`/`ok` column) | yes — the Coolify API has no container or image data | no |
| image version label, creation date | yes | no |
| container → resource mapping and Traefik `Host(...)` | yes (labels) | partly (`applications[].fqdn`) |
| validating that a uuid is a real resource | no | yes (`/applications`, `/services`, `/databases`) |
| pulling the image before a redeploy | yes (`POST /images/create`) | no |
| triggering and following a deploy | no | yes |

Consequences to keep in mind:

- **Without the socket** there is nothing to compare against. The honest output is
  "tag drift" (digest changed since the last observation), never `UPDATE`/`ok` based
  on the running container. Version columns must stay empty, not be guessed.
- **Without the token** the panel is read-only: no resource validation (uuids come
  from labels only), no deploy, no progress. Keep that mode working — it is how the
  app is tested without a Coolify instance.
- **Services need both at once**: the token triggers the redeploy, but Coolify's API
  deploy does not pull, so without the socket the container comes back on the old
  image and the version never changes.
- Token abilities are `read` + `deploy`; `write` is not required and asking for it
  would be wrong. If an endpoint starts returning `403 Missing required permissions`,
  check the ability named in the message instead of widening the token.

## Coolify integration — the sharp edges

Everything below was learned the hard way; re-verify only if the Coolify version
changes materially.

- **`COOLIFY_URL` is reserved.** Coolify injects the public URL of the app itself
  into that name, so reading it as the API base makes deploy calls loop back into
  the dashboard (`401 unauthorized`). Use `COOLIFY_API_URL`.
- **Token abilities:** `POST /api/v1/deploy` requires the **`deploy`** ability
  (not `write`); listing resources requires `read`. A missing ability returns
  `403 Missing required permissions: deploy`.
- **Service deploys from the API do not pull.** `DeployController` calls
  `StartService::run($resource)` whose `pullLatestImages` defaults to `false`
  (only the Livewire button passes `true`). Result: containers are recreated with
  the cached image and the version does not change. Applications are fine: their
  deployment job runs `docker compose pull --ignore-buildable`. That is why this
  app pulls the image itself (`POST /images/create`) for non-application
  resources before asking Coolify to redeploy.
- **Deployment status:** `GET /api/v1/deployments/{uuid}` works for applications.
  Services return no deployment uuid, so progress for them is inferred by
  watching for a recreated container (new container name or newer `Created`).
- **Resource uuid is not always the compose project label.** Containers can carry
  a stale `com.docker.compose.project` (an app that was recreated keeps the old
  project name on running containers). Always validate candidates against
  `/api/v1/applications`, `/services`, `/databases`, and fall back to the uuid
  embedded in the container name (`<service>-<uuid>-<timestamp>`).
- **Domains for compose applications** come from `docker_compose_domains`
  (`PATCH /api/v1/applications/{uuid}` with
  `[{"name": "<compose service>", "domain": "https://..."}]`). Setting only
  `SERVICE_FQDN_*` may leave the first deploy without Traefik labels.
- **`PATCH` does not normalise `git_repository`** the way creation does: send
  `owner/repo`, not a full URL, or the clone tries
  `https://github.com/https://github.com/...`.
- **Creating an application via the API** needs `destination_uuid` when the
  server has more than one destination, and the value is stored verbatim.

## Docker API

- Reads: `/containers/json?all=1`, `/containers/{id}/json`, `/images/{id}/json`.
- Write: `POST /images/create?fromImage=&tag=` (pull). Nothing else.
- The socket is mounted `:ro`, **but that flag does not restrict API calls** —
  only the file. Treat the mount as full daemon access and say so in docs.
- `/containers/json` may report the image **id** instead of the reference;
  prefer `.Config.Image` from the inspect and fall back to `RepoTags`.
- Container → public address: parse `traefik.http.routers.*.rule` for
  `Host(\`...\`)`, preferring the router with `.tls=true`; otherwise use the
  `COOLIFY_URL` env var injected into the container.

## Front end

- Plain JS, no build step, no bundler. Everything lives in the `PAGE` string.
- Two dialogs: deploy confirmation (preview of running → next) and progress.
  Both are native `<dialog>` (focus handling and `Esc` come for free).
- A single row click deploys immediately; multiple rows go through the
  confirmation dialog. Keep that product decision.
- The table refreshes itself every 15 s and whenever the server reports work in
  progress; selections survive re-renders (`sel` holds container names).
- UI changes must follow the Vercel Web Interface Guidelines; the skill is
  installed at `.agents/skills/web-design-guidelines` (it fetches the live rules
  from the upstream repository). Concretely: visible `:focus-visible`, `aria-label`
  on controls, `aria-live` for toasts, `…` instead of `...`, `Intl.*` for dates and
  numbers, no sticky header overlapping sticky column headers, `prefers-reduced-motion`.
- Strings are English, Title Case for buttons and table headers.

## Adding a field end to end

1. `collect()` — compute it and put it in the row dict.
2. Bump `STATE["schema"]` so old cached rows are dropped.
3. `render()` in `PAGE` — read `r.field`, escape with `esc()`.
4. If it belongs in the deploy dialog, add it to `preview()`.
5. Document it in the *Columns* section of the in-app help dialog and in `README.md`.

## Testing without production access

```bash
docker compose up -d --build          # read-only mode, no token needed
curl -s localhost:8799/healthz
curl -s localhost:8799/api/state | head -c 400
curl -s -X POST localhost:8799/api/check
```

Without `COOLIFY_TOKEN`, deploy buttons are hidden and `/api/deploy` answers
`503`; that mode is enough to exercise collection, policies, levels and pins.
When testing against a real instance, prefer a disposable application over one
that serves traffic, and remember a deploy recreates containers.

## Security model

There is no built-in authentication: anyone who can reach the port can trigger
deploys. Do not add auth half-heartedly — the intended deployment is behind a
private network, VPN or an authenticating proxy. The token is read from the
environment only and is never sent to the browser; do not add an endpoint that
returns it or logs it.

The blast radius is the union of both credentials: the token can deploy anything
in its team, and the socket is effectively full access to the Docker daemon of
the host it runs on. That is why the app is expected to run on the same host as
the workloads it watches, and not exposed to the internet.
