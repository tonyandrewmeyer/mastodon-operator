# Mastodon machine charm

[Mastodon](https://joinmastodon.org) is a free, open-source federated social
network server based on ActivityPub. This [Juju](https://juju.is) machine
charm installs and operates a complete Mastodon deployment on Ubuntu 24.04:

- **Puma** web server (port 3000) and **Sidekiq** background workers
- The Node.js **streaming API** (port 4000)
- **nginx** in front, terminating TLS and serving static assets
- Ruby (built via rbenv with jemalloc, matching the release's
  `.ruby-version`) and Node.js 24 from Nodesource

The charm deploys official Mastodon release tarballs, builds them on the
unit, and manages all long-lived secrets (`SECRET_KEY_BASE`, `OTP_SECRET`,
VAPID and Active Record encryption keys) as Juju secrets shared across units.

## Usage

```bash
juju deploy ./mastodon_amd64.charm mastodon --config server-hostname=social.example.com
juju deploy postgresql --channel 16/stable
juju integrate mastodon postgresql
```

Once the application is active, point DNS for `social.example.com` at the
unit (or your load balancer) and create the first administrator:

```bash
juju run mastodon/0 create-admin username=admin email=admin@example.com
```

The action returns the generated password; log in and rotate it. Mastodon
validates that the e-mail domain resolves in DNS, so use a real domain.

> **Warning**: `server-hostname` is permanent. Changing the domain of a
> Mastodon server after it has federated is unsupported and breaks the
> instance's identity.

### E-mail

Mastodon requires outgoing e-mail for sign-ups and notifications. Configure
an SMTP relay:

```bash
juju config mastodon \
    smtp-server=smtp.example.com smtp-port=587 \
    smtp-login=mastodon smtp-password=hunter2 \
    smtp-from-address=notifications@social.example.com
```

### TLS

By default nginx listens on 443 with a self-signed certificate (and
redirects port 80). Either:

- provide a real certificate: `juju config mastodon
  tls-certificate="$(base64 -w0 fullchain.pem)" tls-key="$(base64 -w0 privkey.pem)"`, or
- terminate TLS in front (load balancer / reverse proxy) and set
  `behind-proxy=true` so nginx serves plain HTTP on port 80 and trusts
  `X-Forwarded-Proto`. Set `trusted-proxy-ips` to the proxy's addresses.

### Object storage and external Redis

```bash
juju deploy s3-integrator
juju integrate mastodon s3-integrator
juju integrate mastodon <redis-provider>   # any charm providing the redis interface
```

Without these integrations the charm uses a colocated `redis-server` and
stores media on the `media` Juju storage volume.

### Full-text search (Elasticsearch)

Integrate any charm providing the `elasticsearch` interface, then build the
indices once:

```bash
juju integrate mastodon <elasticsearch-provider>
juju run mastodon/leader tootctl command="search deploy"
```

Tune `es-preset` (`single_node_cluster`, `small_cluster`, `large_cluster`)
to match the cluster. For authenticated clusters, set `ES_USER`/`ES_PASS`
(and `ES_CA_FILE` if needed) through `extra-env`.

### Observability (COS)

The charm provides a `cos-agent` endpoint for the
[grafana-agent](https://charmhub.io/grafana-agent) machine subordinate,
which forwards node metrics, machine logs and the charm's bundled alert
rules (host down, low memory, low disk) to a
[COS stack](https://charmhub.io/topics/canonical-observability-stack):

```bash
juju deploy grafana-agent
juju integrate mastodon grafana-agent
# then integrate grafana-agent with COS (typically via cross-model offers)
```

Mastodon's own application metrics are exposed only as StatsD; pointing
`STATSD_ADDR` (via `extra-env`) at a statsd-exporter is left to the
operator for now.

### Scaling

Scaling to multiple units requires the `redis` and `s3` integrations so all
units share queues, streaming pub/sub and media; the charm blocks otherwise.
A proxy charm can be integrated via the `website` interface, or you can
front the units with your own load balancer (TCP/443, or HTTP/80 with
`behind-proxy=true`).

### Upgrades

```bash
juju config mastodon version=v4.5.12
```

The new release is downloaded and built alongside the running one, the
symlink is switched, database migrations run (on the leader) and services
restart. Read the upstream release notes first; downgrades are not
supported. For zero-downtime upgrades of large instances, see the
`SKIP_POST_DEPLOYMENT_MIGRATIONS` procedure in the Mastodon docs and run the
remaining migrations with the `tootctl`/Rails tasks via the `tootctl` action.

## Actions

| Action | Purpose |
| --- | --- |
| `create-admin` | Create a user with the Owner role; returns the password. |
| `tootctl` | Run any [tootctl](https://docs.joinmastodon.org/admin/tootctl/) command, e.g. `command="cache clear"`. |
| `media-cleanup` | Remove cached remote media older than `days`. |

## Notable configuration

| Option | Description |
| --- | --- |
| `server-hostname` | The instance domain (`LOCAL_DOMAIN`). Required. |
| `version` | Mastodon release tag to deploy/upgrade to. |
| `behind-proxy` | Serve plain HTTP for an upstream TLS terminator. |
| `single-user-mode` | Single-user instance; disables registrations. |
| `web-concurrency`, `max-threads`, `sidekiq-concurrency` | Performance tuning. |
| `extra-env` | Additional `KEY=VALUE` lines for `.env.production` (charm-managed keys cannot be overridden). |

See `charmcraft.yaml` for the full list.

## Limitations

- The charm targets Ubuntu 24.04 on amd64 and arm64 (each architecture is
  built on a host of that architecture).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The charm is built with
[charmcraft](https://juju.is/docs/sdk): `charmcraft pack`.
