# coolify-updates-manager

[![tests](https://github.com/rafaelkillua/coolify-updates-manager/actions/workflows/tests.yml/badge.svg)](https://github.com/rafaelkillua/coolify-updates-manager/actions/workflows/tests.yml)

Dashboard that answers two questions about your Docker hosts:

1. **Is the image I am running still what its tag points to?** It compares the
   digest of the running image with the digest the registry reports for that tag.
2. **If I pin my compose to a major or minor line, which tag should I write?**
   A selector in the header switches the target between `current tag`, `major`
   line and `minor` line, and the listing tells you the exact tag to use and
   whether your container is already on it.

When a Coolify API token is configured, each row also gets an **update** button:
the panel pulls the image through the Docker API (the tag may have moved) and then
calls `POST /api/v1/deploy?uuid=<uuid>&force=false`.

> Why pull first: for **services**, Coolify 4.5.x calls `StartService::run($service)`
> with `pullLatestImages = false` on the API path (only the UI button passes `true`),
> so a deploy from the API recreates the containers with the cached image and the
> version does not change. Applications are fine — their deployment job runs
> `docker compose pull`. The panel pulls for non-application resources and skips it
> for images pinned by digest or built locally.

Single-file Python app (standard library only), one Dockerfile, one compose file.

## Why it needs two credentials

The panel answers questions that live in two different places. Neither source can
answer the other's half.

| question | comes from | why not the other source |
|---|---|---|
| Is the running image the one the tag points to today? | **Docker socket** | the digest of the running image only exists in the Docker daemon; the Coolify API exposes resource *configuration*, never container digests |
| Which tag should I pin, and is the line newer? | registry (public, no credentials) | — |
| Which Coolify resource owns this container, and may I redeploy it? | **Coolify token** | the socket knows the container, not the resource lifecycle (deploy, status, validation) |
| Does the redeploy actually pull a new image? | socket (pull) **and** token (deploy) | Coolify's API deploy for services recreates containers without pulling |

### Docker socket

Used for:

- enumerating containers with their image reference and `RepoDigests` — the
  "running" side of every comparison;
- image labels (`org.opencontainers.image.version`) and creation date shown in the
  *in use* column;
- container labels: `coolify.resourceName` / `com.docker.compose.project` to map a
  container to a resource, and Traefik `Host(...)` for the address behind the
  resource link;
- `POST /images/create` to pull the image before asking Coolify to redeploy.

Without it there is nothing to compare against: the state column cannot say
`UPDATE` or `ok`, the version column is empty, and service deploys would recreate
containers from the cached image. The panel degrades to a tag monitor.

### Coolify token

Used for:

- `GET /applications`, `/services`, `/databases`: the authoritative resource list,
  used to **validate** the uuid found in container labels (containers can keep a
  stale project label after their application is recreated);
- `POST /deploy`: the action behind the update button;
- `GET /deployments/{uuid}`: deploy progress for applications;
- `GET /services/{uuid}`: image references and domains of service containers.

Abilities: **`read` + `deploy`**. `write` is not used. With no token the dashboard
runs read-only (updates are reported, buttons hidden, `/api/deploy` answers `503`).

## The listing

| column | meaning |
|---|---|
| **resource** | Coolify resource name (`coolify.resourceName`); the container name is below it |
| **image** | image reference exactly as the container was created |
| **in use** | tag policy badge — `floating` (`latest`, `stable`, `release`, no tag), `major` (`v2`, `pg16`), `minor` (`18.6-alpine`, `v3.7`), `exact` (`4.5.3`), `digest`, `custom` — plus the version reported by the image label |
| **target** | only when level ≠ `current tag`: the tag to write in the compose file, and the version that line resolves to |
| **state** | `UPDATE` (running image is not what the target points to), `ok`, `??` (no registry, local build, or unknown version) |

`exact tag` (per row) answers a third thing: *which versioned tag is the floating
tag pointing at right now?* Useful when you want to freeze instead of pin by line.

The level selector works from the **newest release the registry offers**, not
from what you are running: choosing `minor` returns `v3.5` for a project whose
newest release is `v3.5.1`, and the row shows `UPDATE` if your container is still
on `v3.4.1`. If the project publishes only full versions, the target falls back
to the exact tag and says so.

## Actions

The **? Help** button in the header opens an in-app explanation of every column, action and the
comparison levels — the same content as below, for whoever is looking at the dashboard.

| action | what it does |
|---|---|
| **Check now** | Re-reads the Docker daemon and asks each registry for the current digest of the tag. Pulls nothing, changes nothing. |
| **update** (one row) | Deploys that resource immediately — `POST /api/v1/deploy?uuid=…`. Coolify re-pulls the tag written in your compose and recreates the container. The compose file is not edited. |
| **exact tag** (one row) | Shows which versioned tag the floating tag points to today, so you can copy it into the compose. Read-only. |
| **compare against** | Switches what the *target* column resolves to (`current tag` / newest `major` line / newest `minor` line). This is what decides `UPDATE` vs `ok`. |
| **Update selected** | Same deploy, for the resources you checked. Opens a dialog listing every container with the running version and the version it will be on after the pull, and waits for confirmation. |
| **Deploys** | Progress of what you triggered: Coolify deployment status for applications (queued → in progress → finished/failed, with a link into Coolify) and container recreation for services, refreshed every 3 s while something runs. |

The deploy never rewrites your compose. If you want the tag pinned, take the value from the *target* column (or from `exact tag`) and edit the compose yourself.

## Environment variables

Everything is optional; the defaults give you a read-only dashboard.

| variable | default | required for | description |
|---|---|---|---|
| `COOLIFY_TOKEN` | — | deploy buttons | Coolify API token with `read` + `deploy`. Empty = read-only mode |
| `COOLIFY_API_URL` | `http://host.docker.internal:8000` | deploy buttons | Coolify API base URL as seen **from inside the container** |
| `COOLIFY_UI_URL` | — | links in the progress dialog | Coolify UI base (e.g. `https://coolify.example.com`) used to build "open in Coolify" links |
| `DOCKER_SOCK` | `/var/run/docker.sock` | always | path to the Docker socket *inside the container* — the only source of the running image digest |
| `DOCKER_URL` | — | — | talk to a Docker socket proxy (`http://proxy:2375`) instead of the socket; overrides `DOCKER_SOCK` |
| `UPDATES_STATE` | `/data/state.json` | — | where the last result is cached (mount a volume) |
| `UPDATES_PORT` | `8799` | — | listening port |
| `UPDATES_BIND` | `0.0.0.0` | — | listening address |
| `UPDATES_WORKERS` | `8` | — | parallel registry requests |
| `UPDATES_IGNORE` | `^coolify(-|$)` | — | comma-separated regexes for containers to hide entirely. Default hides Coolify's own stack (`coolify`, `coolify-db`, `coolify-proxy`…) |
| `UPDATES_TIMEOUT` | `60` | — | per-request timeout, seconds |
| `UPDATES_TAGS_TTL` | `21600` | — | how long a registry tag list is cached, seconds |
| `UPDATES_DIGEST_TTL` | `900` | — | how long a manifest digest is cached, seconds |
| `UPDATES_PREVIEW_TTL` | `300` | — | how long a computed deploy-dialog preview is reused for the same selection |
| `UPDATES_VERSIONS_TTL` | `300` | — | how long the running/next version of each image is cached |

### Which ones do I actually need?

**Read-only dashboard on a plain Docker host**

```yaml
volumes: ["/var/run/docker.sock:/var/run/docker.sock:ro"]
```
Nothing else. No token, no Coolify URL; the deploy buttons stay hidden.

**Dashboard that can redeploy through Coolify**

Add `COOLIFY_TOKEN`. Then set `COOLIFY_API_URL` to whatever reaches your Coolify
API from that container:

- same host, Coolify port published: `http://host.docker.internal:8000`
  (the compose file already adds `extra_hosts: host.docker.internal:host-gateway`);
- container attached to the `coolify` network: `http://coolify:8000`;
- another machine: `https://coolify.example.com`.

**Coolify app created from this repository**

Same as above; add `SERVICE_FQDN_<SERVICE>_<PORT>` (e.g.
`SERVICE_FQDN_APP_8799=updates.example.com`) or set the domain in the UI so
Coolify generates the Traefik labels.

> Do **not** rename `COOLIFY_API_URL` to `COOLIFY_URL`. Coolify reserves that
> name and injects the public URL of this app into it, so deploy calls would loop
> back into the dashboard and fail with `401 unauthorized`.

The footer of the page shows the effective configuration (socket, state file,
Coolify URL, deploy enabled/disabled, worker and TTL values), so you can confirm
what the container is actually using without reading logs.

## Quick start

```bash
cp .env.example .env      # optional: fill COOLIFY_TOKEN / COOLIFY_API_URL
docker compose up -d --build
```

Open `http://localhost:8799`.

## API

| method | path | description |
|---|---|---|
| GET | `/` | dashboard |
| GET | `/healthz` | health probe |
| GET | `/api/state` | last result, selected level, config, deploy capability |
| POST | `/api/check` | start a full check (202, or 409 when one is running) |
| POST | `/api/level` | `{"level": "current"\|"major"\|"minor"}` — switch the target |
| POST | `/api/preview` | `{"uuids": ["…"], "force": false}` — running version → version after the pull, per container (cached per selection for `UPDATES_PREVIEW_TTL`) |
| POST | `/api/deploy` | `{"uuids": ["<coolify uuid>"]}` — deploy in Coolify |
| POST | `/api/pin` | `{"container": "<name>"}` — versioned tag matching the current digest |

## Deploying it with Coolify

Create an **Application** with build pack *Docker Compose* pointing at this
repository (`/docker-compose.yml`). Notes learned the hard way:

- the resource uuid used by the deploy button comes from the container label
  `com.docker.compose.project`, which Coolify sets to the application uuid;
- if the server has more than one destination, the API requires
  `destination_uuid` when creating the app;
- for compose applications the Traefik labels come from
  `docker_compose_domains` (`PATCH /api/v1/applications/<uuid>` with
  `[{"name": "<compose service>", "domain": "https://..."}]`); setting only
  `SERVICE_FQDN_*` may leave the first deploy without labels;
- the token ability required by `POST /api/v1/deploy` is `deploy`, not `write`;
- the app only sees the Docker daemon of the server where it runs.

## Security

There is no built-in authentication: anyone who can reach the port can trigger
deploys. Run it on a private network, a VPN, or behind an authenticating proxy.

The container is hardened (read-only root filesystem, all capabilities dropped,
`no-new-privileges`) and the token is only read from the environment — it is never
sent to the browser.

About the Docker socket: it is mounted `:ro`, but that flag does **not** restrict
API calls — the panel needs `POST /images/create` to pull images. Treat the socket
mount as full Docker API access: this container can, in principle, do anything the
daemon allows.

### Putting a socket proxy in front of the daemon

A proxy narrows that to a per-endpoint allowlist. Point `DOCKER_URL` at it and drop
the socket mount:

```yaml
services:
  docker-proxy:
    image: tecnativa/docker-socket-proxy:latest
    restart: unless-stopped
    environment:
      CONTAINERS: "1"      # list containers, read their config
      IMAGES: "1"          # read image metadata, pull images
      POST: "1"            # required for the pull (POST /images/create)
      EXEC: "0"
      VOLUMES: "0"
      NETWORKS: "0"
      BUILD: "0"
      SYSTEM: "0"
      AUTH: "0"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro

  app:
    image: coolify-updates-manager:latest
    environment:
      DOCKER_URL: http://docker-proxy:2375
    # no /var/run/docker.sock mount
```

What that buys, measured against `tecnativa/docker-socket-proxy` with the config
above (the pull forces `POST=1`):

| request | raw socket | proxy |
|---|---|---|
| read containers / images | yes | yes |
| pull an image | yes | yes |
| start/stop/restart containers | yes | **yes** (availability only) |
| `POST /containers/create` (privileged, host mounts) | yes | **blocked (404)** |
| `GET /volumes` (read or mount volumes) | yes | **blocked (403)** |
| `POST /exec/{id}/start` (shell inside a container) | yes | **blocked (403)** |
| networks, build, swarm, secrets, plugins | yes | **blocked** |

The remaining write power is image pulls and container lifecycle — a DoS surface,
not host root. With `POST=0` the trade-off inverts: the proxy becomes read-only and
the panel can no longer pull, so **service deploys would recreate containers from
the cached image** (the panel reports `pull denied by the docker endpoint` and stops
before deploying). Applications are unaffected because Coolify pulls for them.

If you want the pull restricted to exactly one endpoint, use
[`wollomatic/socket-proxy`](https://github.com/wollomatic/socket-proxy), which
supports per-path regular expressions (`allowPOST = ["^/v[0-9.]+/images/create$"]`).

## Limitations

- Docker Hub rate-limits anonymous manifest requests (100 per 6h per IP). One
  check spends one request per Hub image; with 8 workers ~50 containers take
  about 10 s.
- Images built locally (no registry) show up as `??`.
- Containers from the Coolify stack itself (compose project `source`) are not
  deployable here; their version follows Coolify upgrades.
- Level resolution depends on tags the project publishes. Projects that ship a
  single channel and no versioned tags get an `??` with a note instead of a
  suggested tag.

## Contributing

`AGENTS.md` documents the architecture, the caching rules, the threading model and
the Coolify/Docker behaviours that are easy to break. Read it before changing
`app.py` — it explains why things are the way they are, including the parts that
look odd (like pulling images from the panel before deploying).

Tests need nothing but Python 3.12 and no credentials:

```bash
python3 -m unittest -v test_app      # 70 tests, ~5 s
```

CI (`.github/workflows/tests.yml`) runs them on every pull request: once on
CPython 3.12 and once inside the built image, plus a `/healthz` boot check.

## License

MIT.
