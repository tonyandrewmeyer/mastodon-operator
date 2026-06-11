# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for unit tests."""

from unittest.mock import MagicMock

import pytest

import mastodon

ALL_RUNNING = dict.fromkeys(mastodon.SERVICES, True)

# Default return values for every mastodon.py function that touches the
# system. Unit tests run against these mocks; the pure helpers (render_env,
# env_file_text, generate_secrets) stay real.
SYSTEM_FUNCTIONS = {
    "install_packages": None,
    "ensure_user": None,
    "ensure_media_dirs": None,
    "ensure_rbenv": None,
    "ensure_ruby": None,
    "fetch_app": None,
    "build_app": None,
    "is_release_built": True,
    "activate_release": False,
    "prune_releases": None,
    "installed_version": "v4.5.11",
    "write_env_text": False,
    "install_systemd_units": False,
    "ensure_tls_material": False,
    "configure_nginx": False,
    "set_local_redis": None,
    "enable_services": None,
    "restart_services": None,
    "stop_services": None,
    "services_running": ALL_RUNNING,
    "prepare_database": None,
    "run_migrations": None,
    "configure_cleanup_timer": None,
    "tootctl": "OK",
}


@pytest.fixture
def workload(monkeypatch, tmp_path):
    """Replace all system-touching workload functions with mocks."""
    mocks = {}
    for name, return_value in SYSTEM_FUNCTIONS.items():
        mock = MagicMock(name=name, return_value=return_value)
        monkeypatch.setattr(mastodon, name, mock)
        mocks[name] = mock
    env_file = tmp_path / ".env.production"
    monkeypatch.setattr(mastodon, "ENV_FILE", env_file)
    mocks["env_file"] = env_file
    return mocks
