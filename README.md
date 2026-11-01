# coolify-updates-manager

Dashboard that answers two questions about your Docker hosts:

1. **Is the image I am running still what its tag points to?** It compares the
   digest of the running image with the digest the registry reports for that tag.
2. **If I pin my compose to a major or minor line, which tag should I write?**
   A selector in the header switches the target between `current tag`, `major`
   line and `minor` line, and the listing tells you the exact tag to use and
   whether your container is already on it.

When a Coolify API token is configured, each row also gets an **update** button
that triggers a deploy of that resource.

Single-file Python app (standard library only), one Dockerfile, one compose file.

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

The deploy never rewrites your compose. If you want the tag pinned, take the value from the *target* column (or from `exact tag`) and edit the compose yourself.

## Environment variables

Everything is optional; the defaults give you a read-only dashboard.

| variable | default | required for | description |
|---|---|---|---|
| `COOLIFY_TOKEN` | — | deploy buttons | Coolify API token with the `deploy` ability. Empty = read-only mode |
| `COOLIFY_API_URL` | `http://host.docker.internal:8000` | deploy buttons | Coolify API base URL as seen **from inside the container** |
| `DOCKER_SOCK` | `/var/run/docker.sock` | always | path to the Docker socket *inside the container* |
| `UPDATES_STATE` | `/data/state.json` | — | where the last result is cached (mount a volume) |
| `UPDATES_PORT` | `8799` | — | listening port |
| `UPDATES_BIND` | `0.0.0.0` | — | listening address |
| `UPDATES_WORKERS` | `8` | — | parallel registry requests |
| `UPDATES_TIMEOUT` | `60` | — | per-request timeout, seconds |
| `UPDATES_TAGS_TTL` | `21600` | — | how long a registry tag list is cached, seconds |
| `UPDATES_DIGEST_TTL` | `900` | — | how long a manifest digest is cached, seconds |

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
| POST | `/api/preview` | `{"uuids": ["…"]}` — running version → version after the pull, per container |
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
`no-new-privileges`, Docker socket mounted read-only) and the token is only read
from the environment — it is never sent to the browser.

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

## License

MIT.
