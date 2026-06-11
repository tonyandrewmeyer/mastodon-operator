# Copyright 2026 Tony Meyer
# See LICENSE file for licensing details.

"""Integration tests for the Mastodon machine charm (jubilant).

These deploy the locally packed charm together with PostgreSQL on a machine
cloud (LXD works well) in a temporary model and verify a full Mastodon comes
up. Pack the charm first (`charmcraft pack`); the first unit compiles Ruby
and builds Mastodon's assets, so allow ~30 minutes.

Run with `tox -e integration` against a bootstrapped controller.
"""

import json
import logging
import pathlib
import subprocess

import jubilant
import pytest

logger = logging.getLogger(__name__)

APP = "mastodon"
HOSTNAME = "social.test.example"
POSTGRESQL_CHANNEL = "16/stable"


@pytest.fixture(scope="module")
def charm_path() -> pathlib.Path:
    """Path to the locally packed charm."""
    charms = sorted(pathlib.Path(__file__).parents[2].glob("*.charm"))
    assert charms, "no *.charm file found; run `charmcraft pack` first"
    return charms[0].resolve()


def unit_address(juju: jubilant.Juju) -> str:
    """Public address of the first mastodon unit."""
    status = juju.status()
    return status.apps[APP].units[f"{APP}/0"].public_address


def https_get(address: str, path: str) -> str:
    """Fetch a path from the unit over HTTPS with SNI for our test domain."""
    return subprocess.check_output(
        [
            "curl",
            "-sk",
            "--max-time",
            "30",
            "--resolve",
            f"{HOSTNAME}:443:{address}",
            f"https://{HOSTNAME}{path}",
        ],
        text=True,
    )


def test_deploy(juju: jubilant.Juju, charm_path: pathlib.Path):
    """Deploy mastodon with postgresql and wait for everything active."""
    juju.deploy(charm_path, app=APP, config={"server-hostname": HOSTNAME})
    juju.deploy("postgresql", channel=POSTGRESQL_CHANNEL)
    juju.integrate(APP, "postgresql")
    juju.wait(
        lambda status: jubilant.all_active(status, APP, "postgresql"),
        error=jubilant.any_error,
        delay=10,
        timeout=3600,
    )


def test_web_responds(juju: jubilant.Juju):
    """The instance API answers with our configured domain over HTTPS."""
    instance = json.loads(https_get(unit_address(juju), "/api/v2/instance"))
    assert instance["domain"] == HOSTNAME


def test_streaming_health(juju: jubilant.Juju):
    """The streaming API health endpoint is proxied by nginx."""
    assert "OK" in https_get(unit_address(juju), "/api/v1/streaming/health")


def test_create_admin_action(juju: jubilant.Juju):
    """The create-admin action returns a generated password."""
    # Mastodon validates that the e-mail domain resolves in DNS, so a
    # real-world domain is required even in tests.
    task = juju.run(
        f"{APP}/0",
        "create-admin",
        {"username": "admin", "email": "admin@gmail.com"},
        wait=600,
    )
    assert task.success
    assert task.results.get("password")


def test_tootctl_action(juju: jubilant.Juju):
    """An arbitrary tootctl command runs and returns output."""
    task = juju.run(f"{APP}/0", "tootctl", {"command": "cache clear"}, wait=600)
    assert task.success
    assert "OK" in task.results.get("output", "")
