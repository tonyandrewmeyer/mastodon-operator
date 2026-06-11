# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the Mastodon machine charm.

These deploy the locally packed charm together with PostgreSQL on a machine
cloud (LXD works well) and verify a full Mastodon comes up. The first unit
compiles Ruby and builds Mastodon's assets, so allow ~30 minutes.
"""

import json
import logging
import pathlib
import subprocess

import pytest
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

APP = "mastodon"
HOSTNAME = "social.test.example"
POSTGRESQL_CHANNEL = "16/stable"


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    """Deploy mastodon with postgresql and wait for active status."""
    charms = list(pathlib.Path(".").glob("*.charm"))
    charm = charms[0].resolve() if charms else await ops_test.build_charm(".")

    await ops_test.model.deploy(
        str(charm),
        application_name=APP,
        config={"server-hostname": HOSTNAME},
    )
    await ops_test.model.deploy(
        "postgresql", channel=POSTGRESQL_CHANNEL, application_name="postgresql"
    )
    await ops_test.model.integrate(APP, "postgresql")

    await ops_test.model.wait_for_idle(apps=[APP], status="active", timeout=3600, idle_period=30)


async def test_web_responds(ops_test: OpsTest):
    """The instance API answers with our configured domain over HTTPS."""
    unit = ops_test.model.applications[APP].units[0]
    address = await unit.get_public_address()
    output = subprocess.check_output(
        [
            "curl",
            "-sk",
            "--max-time",
            "30",
            "--resolve",
            f"{HOSTNAME}:443:{address}",
            f"https://{HOSTNAME}/api/v2/instance",
        ],
        text=True,
    )
    instance = json.loads(output)
    assert instance["domain"] == HOSTNAME


async def test_streaming_health(ops_test: OpsTest):
    """The streaming API health endpoint is proxied by nginx."""
    unit = ops_test.model.applications[APP].units[0]
    address = await unit.get_public_address()
    output = subprocess.check_output(
        [
            "curl",
            "-sk",
            "--max-time",
            "30",
            "--resolve",
            f"{HOSTNAME}:443:{address}",
            f"https://{HOSTNAME}/api/v1/streaming/health",
        ],
        text=True,
    )
    assert "OK" in output


async def test_create_admin_action(ops_test: OpsTest):
    """The create-admin action returns a generated password."""
    unit = ops_test.model.applications[APP].units[0]
    action = await unit.run_action("create-admin", username="admin", email="admin@test.example")
    result = await action.wait()
    assert result.status == "completed"
    assert result.results.get("password")
