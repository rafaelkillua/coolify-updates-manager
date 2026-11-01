# updates-web

Dashboard that tells you which running Docker containers are behind their image
tag, and lets you redeploy the matching resource in [Coolify](https://coolify.io)
with one click.

Single-file Python app (stdlib only), one Dockerfile, one compose file.

## How it works

- reads container state from the Docker daemon over `/var/run/docker.sock`
  (mounted **read-only**);
- for every image it compares the local digest (`RepoDigests`) with the
  `Docker-Content-Digest` the registry reports for that tag
  (`HEAD /v2/<repo>/manifests/<tag>`, fetching an anonymous token when the
  registry asks for one);
- it never pulls layers and never changes local images;
- the Coolify resource uuid comes from the container labels
  (`com.docker.compose.project`, `coolify.resourceName`), so anything not managed
  by Coolify is listed as read-only;
- clicking **update** calls `POST /api/v1/deploy?uuid=<uuid>&force=false`.

The **pin** button asks the registry which versioned tag currently resolves to
the same digest as the floating tag (`latest`, `stable`, `release`). Use it to
replace a floating tag with an exact version in your compose file.

## Quick start

```bash
cp .env.example .env      # set COOLIFY_API_URL / COOLIFY_TOKEN if you want deploy
docker compose up -d --build
```

Open `http://localhost:8799`. Without `COOLIFY_TOKEN` the dashboard runs
read-only: it reports updates and hides the deploy buttons.

## Deploying with Coolify

Create an **Application** with build pack *Docker Compose* pointing at this
repository (`/docker-compose.yml`), then add the environment variables:

| variable | value |
|---|---|
| `COOLIFY_TOKEN` | API token with the `deploy` ability |
| `COOLIFY_API_URL` | `http://host.docker.internal:8000` (default in compose) |
| `SERVICE_FQDN_<SERVICE>_<PORT>` | domain for the dashboard, e.g. `updates.example.com` |

Notes:

- `COOLIFY_API_URL` defaults to `http://host.docker.internal:8000` and the compose
  file adds an `extra_hosts` entry with `host-gateway`, so the container reaches
  Coolify through the published port. Alternatively use `http://coolify:8000`
  with the app attached to the `coolify` network.
- Never rename that variable to `COOLIFY_URL`. Coolify reserves the name and
  injects the public URL of this app into it, so deploy calls would loop back
  into the dashboard and fail with `401 unauthorized`.
- For compose applications Coolify builds the Traefik labels from
  `docker_compose_domains`. Setting only the `SERVICE_FQDN_*` variables may leave
  the app without labels on the first deploy; setting the domain in the UI (or
  `PATCH`ing `docker_compose_domains`) is what actually routes it.
- The dashboard reads the Docker daemon of the server it runs on. Deploy it on
  the server whose containers you want to watch.

## Endpoints

| method | path | description |
|---|---|---|
| GET | `/` | dashboard |
| GET | `/healthz` | health probe |
| GET | `/api/state` | last result, whether a check is running, deploy capability |
| POST | `/api/check` | start a full check (202, or 409 if one is running) |
| POST | `/api/deploy` | `{"uuids": ["..."]}` → deploy in Coolify |
| POST | `/api/pin` | `{"container": "name"}` → versioned tag matching the current digest |

## Environment variables

| name | default | description |
|---|---|---|
| `COOLIFY_TOKEN` | — | token with the `deploy` ability; empty = read-only mode |
| `COOLIFY_API_URL` | `http://host.docker.internal:8000` | Coolify API base |
| `UPDATES_PORT` | `8799` | listening port |
| `UPDATES_BIND` | `0.0.0.0` | listening address |
| `UPDATES_STATE` | `/data/state.json` | state cache (volume) |
| `UPDATES_WORKERS` | `8` | parallel registry requests |
| `UPDATES_TIMEOUT` | `60` | per-request timeout in seconds |
| `DOCKER_SOCK` | `/var/run/docker.sock` | Docker socket path |

## Security

There is no built-in authentication: anyone who can reach the port can trigger
deploys. Run it on a private network, behind a VPN, or in front of an
authenticating proxy. The container itself is hardened (read-only root
filesystem, all capabilities dropped, `no-new-privileges`, Docker socket mounted
read-only), and the token is only ever read from the environment.

## Limitations

- Docker Hub rate-limits anonymous manifest requests (100 per 6h per IP). One
  full check spends one request per Hub image; with 8 workers, ~50 containers
  take about 10 s.
- Images built locally (no registry) show up as `??`.
- Containers from the Coolify stack itself (compose project `source`) are not
  deployable here; their version follows Coolify upgrades.
- `pin` returns nothing for projects that publish channels only (`latest` rebuilt
  on every base image update).

## License

MIT.
