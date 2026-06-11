# Contributing

## Developing

Create and activate a virtualenv with the development requirements:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install tox
```

## Testing

```bash
tox -e format        # auto-format the code
tox -e lint          # ruff checks
tox -e unit          # unit tests (ops Scenario)
tox -e integration   # integration tests; needs a bootstrapped Juju controller
```

The integration tests deploy the locally packed charm together with
PostgreSQL on a machine cloud (LXD works well) and take a long time: the
first unit compiles Ruby and builds Mastodon's assets.

## Building

```bash
charmcraft pack
```

charmcraft's incremental build cache does not pick up file-mode-only
changes (e.g. restoring the executable bit on `src/charm.py`); run
`charmcraft clean` first after such changes.

## Design notes

- `src/mastodon.py` contains everything that touches the machine (apt,
  rbenv, systemd, nginx, the release builds). It has no ops imports and is
  mocked wholesale in unit tests; `src/charm.py` contains only Juju-facing
  logic.
- Releases are installed under `/home/mastodon/releases/<tag>` and activated
  by switching the `/home/mastodon/live` symlink, so upgrades build offline
  and the previous release is kept for inspection.
- Long-lived Mastodon secrets are generated once by the leader and stored in
  a Juju application secret; the secret ID is shared via peer relation data.
- Database migrations run only on the leader; peers gate service startup on
  the `migrated-version` key in peer application data.
