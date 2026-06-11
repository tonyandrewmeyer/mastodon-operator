# Contributing

## Developing

Dependencies are managed with [uv](https://docs.astral.sh/uv/):
`pyproject.toml` declares them, `uv.lock` pins them, and everything
(charmcraft, tox, CI, Dependabot) installs from the lockfile. There is no
requirements.txt.

Install uv and tox (with the [tox-uv](https://github.com/tox-dev/tox-uv)
plugin, which lets tox create its environments from the lockfile):

```bash
sudo snap install astral-uv --classic   # or: pipx install uv
pipx install tox
pipx inject tox tox-uv
```

For an editor/REPL environment with the dev dependencies:

```bash
uv sync   # creates .venv from uv.lock (including the dev group)
```

After changing dependencies in `pyproject.toml`, run `uv lock` and commit
the updated lockfile; CI fails if the two drift apart.

## Testing

```bash
tox -e format        # auto-format the code
tox -e lint          # ruff + pyright
tox -e unit          # unit tests (ops Scenario)
tox -e integration   # integration tests; needs a bootstrapped Juju controller
```

The tox environments install from `uv.lock` via the dependency groups in
`pyproject.toml` (`dev` for format/lint/unit, `integration` for the jubilant
tests). The integration tests deploy the locally packed charm together with
PostgreSQL on a machine cloud (LXD works well) and take a long time: the
first unit compiles Ruby and builds Mastodon's assets.

To provision a machine with everything the integration tests need
(charmcraft, LXD and a bootstrapped Juju controller), use
[concierge](https://github.com/canonical/concierge) with the config in this
repository:

```bash
sudo snap install --classic concierge
sudo concierge prepare
```

## Building

```bash
charmcraft pack
```

The charm is built with charmcraft's `uv` plugin, which installs the
runtime dependencies from `uv.lock` into the charm's venv. Note that
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
  the `migrated-version` key in peer application data. Upgrades run
  migrations in two phases (pre- and post-deployment) per Mastodon's
  upgrade procedure.
- With the `role` option, auxiliary applications receive their rendered
  environment, version and migration state from the primary over the
  `cluster`/`primary` relation; only the primary talks to the database.
