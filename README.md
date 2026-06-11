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

Mastodon requires outgoing e-mail for sign-ups and notifications. The
preferred way is the `smtp` integration with
[smtp-integrator](https://charmhub.io/smtp-integrator), which keeps the
relay password in a Juju secret:

```bash
juju deploy smtp-integrator --config host=smtp.example.com --config port=587 ...
juju integrate mastodon smtp-integrator:smtp
```

Alternatively, configure the relay directly:

```bash
juju config mastodon \
    smtp-server=smtp.example.com smtp-port=587 \
    smtp-login=mastodon smtp-password=hunter2 \
    smtp-from-address=notifications@social.example.com
```

The integration takes precedence when both are present;
`smtp-from-address` applies in both cases.

### TLS

nginx listens on 443 (and redirects port 80). The certificate is chosen
with the following precedence:

1. **`certificates` integration** (`tls-certificates` interface): relate any
   v4 provider — e.g. the [lego](https://charmhub.io/lego) charm for ACME /
   Let's Encrypt, or
   [self-signed-certificates](https://charmhub.io/self-signed-certificates)
   for internal CAs. The charm requests a certificate for
   `server-hostname` (plus `web-domain` as a SAN) and rotates nginx
   automatically on issuance and renewal.

   ```bash
   juju deploy lego --config email=you@example.com ...
   juju integrate mastodon lego
   ```

2. **Config**: `juju config mastodon tls-certificate="$(base64 -w0
   fullchain.pem)" tls-key="$(base64 -w0 privkey.pem)"`.

3. **Self-signed fallback**, also used while a related provider has not
   issued the certificate yet.

Alternatively, terminate TLS in front (load balancer / reverse proxy) and
set `behind-proxy=true` so nginx serves plain HTTP on port 80 and trusts
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

Mastodon's application metrics are exported natively: the charm enables
Mastodon's built-in Prometheus exporters, and grafana-agent scrapes the
web (Puma, port 9394), Sidekiq (port 9395) and streaming (port 4000)
`/metrics` endpoints on localhost. Set
`MASTODON_PROMETHEUS_EXPORTER_WEB_DETAILED_METRICS=true` (or the Sidekiq
equivalent) via `extra-env` for per-action/per-job detail at the cost of
some overhead.

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

The charm follows Mastodon's documented upgrade procedure: the new release
is downloaded and built alongside the running one, pre-deployment database
migrations run (`SKIP_POST_DEPLOYMENT_MIGRATIONS=true`, safe against the
old code), the `live` symlink is switched and services restart onto the
new release, and the remaining post-deployment migrations run last. Read
the upstream release notes first — some releases have extra steps (use the
`tootctl` action for those); downgrades are not supported. The previous
release directory is kept on disk for inspection.

### Scheduled maintenance

A daily systemd timer prunes cached remote media older than
`media-cache-retention-days` (default 7) and link-preview cards older than
180 days, per Mastodon's storage optimization guidance. Local user uploads
are never touched; set the option to 0 to disable. One-off maintenance
(`tootctl media remove-orphans`, `accounts cull`, `cache recount`, ...)
is available through the `tootctl` action.

When the colocated Redis is used, the charm enables append-only
persistence so queued Sidekiq jobs survive a crash or reboot.

### Backup and restore

What to back up, and where it lives:

| Data | Where | How |
| --- | --- | --- |
| PostgreSQL | postgresql application | Use the postgresql charm's backup actions / `s3-parameters` integration. |
| Secrets (`SECRET_KEY_BASE`, OTP, VAPID, encryption keys) | Juju app secret | `juju show-secret mastodon-app-secrets --reveal` — store the output securely. **Without these, the database backup is unusable** (2FA, push, encrypted columns). |
| Media | S3 bucket, or the `media` Juju storage | Bucket versioning/replication, or snapshot the storage volume. |
| Redis | colocated (or external) | Optional: only volatile queues/caches are lost; home feeds are rebuilt with `tootctl feeds build`. |

To restore: deploy the charm, restore the PostgreSQL backup into the new
database, recreate the app secret content before the first start (or
replace the generated secret's content with the saved values via
`juju update-secret`), reattach media, and run `tootctl feeds build`.

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
