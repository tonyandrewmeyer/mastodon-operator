# Copyright 2026 Tony Meyer
# See LICENSE file for licensing details.

"""Unit tests for the Mastodon charm (ops Scenario)."""

import json
import unittest.mock

import pytest
from ops import testing

from charm import MastodonCharm

HOSTNAME = "social.example.com"
VERSION = "v4.5.11"

SECRET_CONTENT = {
    "secret-key-base": "k" * 32,
    "otp-secret": "o" * 32,
    "vapid-private-key": "priv",
    "vapid-public-key": "pub",
    "ar-deterministic-key": "d" * 32,
    "ar-key-derivation-salt": "s" * 32,
    "ar-primary-key": "p" * 32,
}

DB_DATA = {
    "endpoints": "10.10.0.5:5432",
    "username": "dbuser",
    "password": "dbpass",
    "database": "mastodon",
}


@pytest.fixture
def ctx():
    return testing.Context(MastodonCharm)


def db_relation(**kwargs):
    return testing.Relation(
        endpoint="database", remote_app_name="postgresql", remote_app_data=DB_DATA, **kwargs
    )


def app_secret():
    return testing.Secret(
        tracked_content=SECRET_CONTENT, label="mastodon-app-secrets", owner="app"
    )


def base_state(*, leader=True, secret=None, extra_relations=(), **kwargs):
    secret = secret or app_secret()
    peer = testing.PeerRelation(endpoint="mastodon-peers", local_app_data={"secret-id": secret.id})
    config = {"server-hostname": HOSTNAME}
    config.update(kwargs.pop("config", {}))
    return testing.State(
        leader=leader,
        config=config,
        relations={peer, db_relation(), *extra_relations},
        secrets={secret},
        **kwargs,
    )


def written_env(workload) -> dict:
    """Parse the env file text passed to write_env_text into a dict."""
    workload["write_env_text"].assert_called()
    text = workload["write_env_text"].call_args[0][0]
    env = {}
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key] = value.strip('"')
    return env


def test_blocked_when_hostname_missing(ctx, workload):
    state = testing.State(
        leader=True,
        relations={testing.PeerRelation(endpoint="mastodon-peers"), db_relation()},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "server-hostname" in out.unit_status.message


def test_blocked_when_database_relation_missing(ctx, workload):
    state = testing.State(
        leader=True,
        config={"server-hostname": HOSTNAME},
        relations={testing.PeerRelation(endpoint="mastodon-peers")},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "database" in out.unit_status.message


def test_blocked_on_invalid_version(ctx, workload):
    state = base_state(config={"version": "latest"})
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "invalid version" in out.unit_status.message


def test_blocked_on_partial_tls_config(ctx, workload):
    state = base_state(config={"tls-certificate": "YWJj"})
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "tls" in out.unit_status.message.lower()


def test_waiting_for_database_credentials(ctx, workload):
    secret = app_secret()
    peer = testing.PeerRelation(endpoint="mastodon-peers", local_app_data={"secret-id": secret.id})
    empty_db = testing.Relation(endpoint="database", remote_app_name="postgresql")
    state = testing.State(
        leader=True,
        config={"server-hostname": HOSTNAME},
        relations={peer, empty_db},
        secrets={secret},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, testing.WaitingStatus)
    assert "database" in out.unit_status.message
    workload["prepare_database"].assert_not_called()


def test_non_leader_waits_for_secrets(ctx, workload):
    state = testing.State(
        leader=False,
        config={"server-hostname": HOSTNAME},
        relations={testing.PeerRelation(endpoint="mastodon-peers"), db_relation()},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, testing.WaitingStatus)
    assert "leader" in out.unit_status.message


def test_leader_generates_secrets(ctx, workload):
    peer = testing.PeerRelation(endpoint="mastodon-peers")
    state = testing.State(
        leader=True,
        config={"server-hostname": HOSTNAME},
        relations={peer, db_relation()},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    secret = out.get_secret(label="mastodon-app-secrets")
    assert set(secret.tracked_content) == set(SECRET_CONTENT)
    assert out.get_relation(peer.id).local_app_data["secret-id"]
    env = written_env(workload)
    assert env["SECRET_KEY_BASE"] == secret.tracked_content["secret-key-base"]


def test_active_happy_path(ctx, workload):
    state = base_state()
    out = ctx.run(ctx.on.config_changed(), state)
    assert out.unit_status == testing.ActiveStatus()
    # Fresh database: one-shot schema load; no two-phase migrations needed.
    workload["prepare_database"].assert_called_once()
    workload["run_migrations"].assert_not_called()
    peer = next(r for r in out.relations if r.endpoint == "mastodon-peers")
    assert peer.local_app_data["migrated-version"] == VERSION
    assert peer.local_app_data["post-migrated-version"] == VERSION
    workload["configure_cleanup_timer"].assert_called_once_with(7)
    assert {port.port for port in out.opened_ports} == {80, 443}
    assert out.workload_version == VERSION.lstrip("v")
    env = written_env(workload)
    assert env["LOCAL_DOMAIN"] == HOSTNAME
    assert env["DB_HOST"] == "10.10.0.5"
    assert env["DB_PORT"] == "5432"
    assert env["DB_USER"] == "dbuser"
    assert env["DB_PASS"] == "dbpass"
    # No changes and everything running: no restart needed.
    workload["restart_services"].assert_not_called()
    workload["enable_services"].assert_called()


def test_restart_when_env_changes(ctx, workload):
    workload["write_env_text"].return_value = True
    out = ctx.run(ctx.on.config_changed(), base_state())
    workload["restart_services"].assert_called_once()
    assert out.unit_status == testing.ActiveStatus()


def test_services_restarted_when_down(ctx, workload):
    workload["services_running"].return_value = {"mastodon-web": False}
    ctx.run(ctx.on.config_changed(), base_state())
    workload["restart_services"].assert_called_once()


def test_non_leader_does_not_migrate(ctx, workload):
    state = base_state(leader=False)
    out = ctx.run(ctx.on.config_changed(), state)
    workload["prepare_database"].assert_not_called()
    workload["run_migrations"].assert_not_called()
    assert isinstance(out.unit_status, testing.WaitingStatus)
    assert "migrations" in out.unit_status.message
    # Services are not started before migrations have run.
    workload["restart_services"].assert_not_called()


def test_upgrade_builds_and_migrates(ctx, workload):
    new_version = "v4.5.12"
    workload["is_release_built"].return_value = False
    workload["activate_release"].return_value = True
    workload["installed_version"].return_value = new_version
    secret = app_secret()
    peer = testing.PeerRelation(
        endpoint="mastodon-peers",
        local_app_data={
            "secret-id": secret.id,
            "migrated-version": VERSION,
            "post-migrated-version": VERSION,
        },
    )
    state = testing.State(
        leader=True,
        config={"server-hostname": HOSTNAME, "version": new_version},
        relations={peer, db_relation()},
        secrets={secret},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    workload["fetch_app"].assert_called_once_with(new_version)
    workload["build_app"].assert_called_once_with(new_version, "all")
    # Two-phase upgrade: pre-deployment migrations, restart, then the rest.
    workload["prepare_database"].assert_not_called()
    assert workload["run_migrations"].call_args_list == [
        unittest.mock.call(skip_post_deployment=True),
        unittest.mock.call(),
    ]
    workload["restart_services"].assert_called_once()
    data = out.get_relation(peer.id).local_app_data
    assert data["migrated-version"] == new_version
    assert data["post-migrated-version"] == new_version
    assert out.unit_status == testing.ActiveStatus()


def test_cleanup_timer_disabled(ctx, workload):
    ctx.run(ctx.on.config_changed(), base_state(config={"media-cache-retention-days": 0}))
    workload["configure_cleanup_timer"].assert_called_once_with(0)


def test_redis_relation_disables_local_redis(ctx, workload):
    redis = testing.Relation(
        endpoint="redis",
        remote_app_name="redis",
        remote_units_data={0: {"hostname": "10.20.0.7", "port": "6379"}},
    )
    ctx.run(ctx.on.config_changed(), base_state(extra_relations=(redis,)))
    env = written_env(workload)
    assert env["REDIS_HOST"] == "10.20.0.7"
    workload["set_local_redis"].assert_called_once_with(enabled=False)


def test_local_redis_fallback(ctx, workload):
    ctx.run(ctx.on.config_changed(), base_state())
    env = written_env(workload)
    assert env["REDIS_HOST"] == "127.0.0.1"
    workload["set_local_redis"].assert_called_once_with(enabled=True)


def test_s3_relation_enables_object_storage(ctx, workload):
    s3 = testing.Relation(
        endpoint="s3",
        remote_app_name="s3-integrator",
        remote_app_data={
            "bucket": "mastodon",
            "access-key": "AKIA123",
            "secret-key": "shhh",
            "endpoint": "https://s3.example.com",
            "region": "us-east-1",
        },
    )
    ctx.run(ctx.on.config_changed(), base_state(extra_relations=(s3,)))
    env = written_env(workload)
    assert env["S3_ENABLED"] == "true"
    assert env["S3_BUCKET"] == "mastodon"
    assert env["AWS_ACCESS_KEY_ID"] == "AKIA123"
    assert env["S3_ENDPOINT"] == "https://s3.example.com"


def test_elasticsearch_relation_enables_search(ctx, workload):
    es = testing.Relation(
        endpoint="elasticsearch",
        remote_app_name="elasticsearch",
        remote_units_data={0: {"host": "10.30.0.9", "port": "9200"}},
    )
    ctx.run(ctx.on.config_changed(), base_state(extra_relations=(es,)))
    env = written_env(workload)
    assert env["ES_ENABLED"] == "true"
    assert env["ES_HOST"] == "10.30.0.9"
    assert env["ES_PORT"] == "9200"
    assert env["ES_PRESET"] == "single_node_cluster"


def test_no_elasticsearch_by_default(ctx, workload):
    ctx.run(ctx.on.config_changed(), base_state())
    assert "ES_ENABLED" not in written_env(workload)


def test_invalid_es_preset_blocks(ctx, workload):
    out = ctx.run(ctx.on.config_changed(), base_state(config={"es-preset": "huge"}))
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "es-preset" in out.unit_status.message


def test_smtp_relation_preferred_over_config(ctx, workload):
    smtp = testing.Relation(
        endpoint="smtp",
        remote_app_name="smtp-integrator",
        remote_app_data={
            "host": "relay.example.com",
            "port": "2525",
            "transport_security": "tls",
            "auth_type": "none",
        },
    )
    ctx.run(
        ctx.on.config_changed(),
        base_state(extra_relations=(smtp,), config={"smtp-server": "ignored.example.com"}),
    )
    env = written_env(workload)
    assert env["SMTP_SERVER"] == "relay.example.com"
    assert env["SMTP_PORT"] == "2525"
    assert env["SMTP_TLS"] == "true"
    assert "SMTP_LOGIN" not in env


def test_smtp_config_fallback(ctx, workload):
    ctx.run(
        ctx.on.config_changed(),
        base_state(config={"smtp-server": "mail.example.com", "smtp-login": "mastodon"}),
    )
    env = written_env(workload)
    assert env["SMTP_SERVER"] == "mail.example.com"
    assert env["SMTP_LOGIN"] == "mastodon"
    assert env["SMTP_ENABLE_STARTTLS"] == "auto"


def test_relation_tls_preferred_over_self_signed(ctx, workload, monkeypatch):
    from charm import MastodonCharm

    monkeypatch.setattr(
        MastodonCharm, "_relation_tls", lambda self: ("RELATION-CERT\n", "RELATION-KEY\n")
    )
    out = ctx.run(ctx.on.config_changed(), base_state())
    workload["ensure_tls_material"].assert_called_once_with(
        HOSTNAME, "RELATION-CERT\n", "RELATION-KEY\n"
    )
    assert out.unit_status == testing.ActiveStatus()


def test_self_signed_fallback_without_certificates(ctx, workload):
    ctx.run(ctx.on.config_changed(), base_state())
    workload["ensure_tls_material"].assert_called_once_with(HOSTNAME, None, None)


def test_cos_agent_relation_publishes_config(ctx, workload):
    cos = testing.Relation(endpoint="cos-agent", remote_app_name="grafana-agent")
    out = ctx.run(ctx.on.relation_joined(cos), base_state(extra_relations=(cos,)))
    config = json.loads(out.get_relation(cos.id).local_unit_data["config"])
    rules = config["metrics_alert_rules"]
    alerts = [rule["alert"] for group in rules["groups"] for rule in group["rules"]]
    assert "MastodonHostLowDiskSpace" in alerts
    assert "MastodonHostLowMemory" in alerts
    scrape_ports = {
        int(target.rsplit(":", 1)[1])
        for job in config["metrics_scrape_jobs"]
        for static in job["static_configs"]
        for target in static["targets"]
    }
    assert {9394, 9395, 4000} <= scrape_ports


def test_scaling_requires_redis_and_s3(ctx, workload):
    state = base_state(planned_units=3)
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "redis" in out.unit_status.message
    assert "s3" in out.unit_status.message
    workload["restart_services"].assert_not_called()


def test_website_relation_published(ctx, workload):
    website = testing.Relation(endpoint="website", remote_app_name="haproxy")
    out = ctx.run(ctx.on.relation_joined(website), base_state(extra_relations=(website,)))
    data = out.get_relation(website.id).local_unit_data
    assert data["port"] == "80"
    assert data["hostname"]


def test_database_broken_stops_services(ctx, workload):
    relation = db_relation()
    secret = app_secret()
    peer = testing.PeerRelation(endpoint="mastodon-peers", local_app_data={"secret-id": secret.id})
    state = testing.State(
        leader=True,
        config={"server-hostname": HOSTNAME},
        relations={peer, relation},
        secrets={secret},
    )
    ctx.run(ctx.on.relation_broken(relation), state)
    workload["stop_services"].assert_called_once()


def test_install_sets_up_machine(ctx, workload):
    state = testing.State(
        leader=True,
        relations={testing.PeerRelation(endpoint="mastodon-peers")},
    )
    ctx.run(ctx.on.install(), state)
    workload["install_packages"].assert_called_once()
    workload["ensure_user"].assert_called_once()
    workload["ensure_rbenv"].assert_called_once()


def test_create_admin_action(ctx, workload):
    workload["env_file"].write_text("LOCAL_DOMAIN=x\n")
    workload["tootctl"].return_value = "OK\nNew password:\nsup3rsecret\n"
    ctx.run(
        ctx.on.action("create-admin", params={"username": "admin", "email": "a@b.com"}),
        base_state(),
    )
    assert ctx.action_results["password"] == "sup3rsecret"
    args = workload["tootctl"].call_args[0][0]
    assert args[:3] == ["accounts", "create", "admin"]
    assert "--role" in args


def test_tootctl_action(ctx, workload):
    workload["env_file"].write_text("LOCAL_DOMAIN=x\n")
    workload["tootctl"].return_value = "cleared"
    ctx.run(ctx.on.action("tootctl", params={"command": "cache clear"}), base_state())
    workload["tootctl"].assert_called_once_with(["cache", "clear"])
    assert ctx.action_results["output"] == "cleared"


def test_actions_fail_when_not_ready(ctx, workload):
    workload["installed_version"].return_value = None
    with pytest.raises(testing.ActionFailed):
        ctx.run(ctx.on.action("tootctl", params={"command": "cache clear"}), base_state())


def test_media_cleanup_action(ctx, workload):
    workload["env_file"].write_text("LOCAL_DOMAIN=x\n")
    ctx.run(ctx.on.action("media-cleanup", params={"days": 3}), base_state())
    workload["tootctl"].assert_called_once_with(["media", "remove", "--days", "3"])


# ----------------------------------------------------------------------
# Roles and the cluster/primary integrations
# ----------------------------------------------------------------------


def cluster_secret():
    return testing.Secret(
        tracked_content={"env": 'LOCAL_DOMAIN="social.example.com"\nDB_HOST="10.10.0.5"\n'},
    )


def primary_relation(secret, **extra):
    data = {
        "version": VERSION,
        "hostname": HOSTNAME,
        "secret-id": secret.id,
        "migrated-version": VERSION,
        "post-migrated-version": VERSION,
    }
    data.update(extra)
    return testing.Relation(
        endpoint="primary", remote_app_name="mastodon-main", remote_app_data=data
    )


def test_invalid_role_blocks(ctx, workload):
    out = ctx.run(ctx.on.config_changed(), base_state(config={"role": "worker"}))
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "role" in out.unit_status.message


def test_database_and_primary_relations_conflict(ctx, workload):
    secret = cluster_secret()
    state = base_state(extra_relations=(primary_relation(secret),))
    state = testing.State(
        leader=state.leader,
        config=dict(state.config),
        relations=state.relations,
        secrets=state.secrets | {secret},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "both" in out.unit_status.message


def test_auxiliary_sidekiq_flow(ctx, workload):
    secret = cluster_secret()
    relation = primary_relation(secret)
    state = testing.State(
        leader=True,
        config={"role": "sidekiq"},
        relations={testing.PeerRelation(endpoint="mastodon-peers"), relation},
        secrets={secret},
    )
    out = ctx.run(ctx.on.relation_changed(relation), state)
    # Env comes verbatim from the primary's shared secret.
    workload["write_env_text"].assert_called_once_with(secret.tracked_content["env"])
    workload["install_systemd_units"].assert_called_once()
    assert workload["install_systemd_units"].call_args.kwargs["role"] == "sidekiq"
    workload["disable_nginx"].assert_called_once()
    workload["set_local_redis"].assert_called_once_with(enabled=False)
    workload["prepare_database"].assert_not_called()
    workload["run_migrations"].assert_not_called()
    workload["enable_services"].assert_called_once_with("sidekiq")
    assert not out.opened_ports
    assert out.get_relation(relation.id).local_unit_data["active-version"] == VERSION
    assert out.unit_status == testing.ActiveStatus("role: sidekiq")


def test_auxiliary_waits_without_primary_data(ctx, workload):
    relation = testing.Relation(endpoint="primary", remote_app_name="mastodon")
    state = testing.State(
        leader=True,
        config={"role": "streaming"},
        relations={testing.PeerRelation(endpoint="mastodon-peers"), relation},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, testing.WaitingStatus)
    assert "primary" in out.unit_status.message
    workload["restart_services"].assert_not_called()


def test_primary_publishes_cluster_data(ctx, workload):
    cluster = testing.Relation(endpoint="cluster", remote_app_name="mastodon-workers")
    redis = testing.Relation(
        endpoint="redis",
        remote_app_name="redis",
        remote_units_data={0: {"hostname": "10.20.0.7", "port": "6379"}},
    )
    s3 = testing.Relation(
        endpoint="s3",
        remote_app_name="s3-integrator",
        remote_app_data={"bucket": "m", "access-key": "a", "secret-key": "s"},
    )
    out = ctx.run(ctx.on.config_changed(), base_state(extra_relations=(cluster, redis, s3)))
    data = out.get_relation(cluster.id).local_app_data
    assert data["version"] == VERSION
    assert data["hostname"] == HOSTNAME
    assert data["migrated-version"] == VERSION
    assert data["secret-id"]
    shared = out.get_secret(label="mastodon-cluster-env")
    assert "DB_HOST" in shared.tracked_content["env"]


def test_cluster_relation_requires_redis_and_s3(ctx, workload):
    cluster = testing.Relation(endpoint="cluster", remote_app_name="mastodon-workers")
    out = ctx.run(ctx.on.config_changed(), base_state(extra_relations=(cluster,)))
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "redis" in out.unit_status.message and "s3" in out.unit_status.message


def test_post_migrations_wait_for_auxiliaries(ctx, workload):
    new_version = "v4.5.12"
    workload["installed_version"].return_value = new_version
    secret = app_secret()
    peer = testing.PeerRelation(
        endpoint="mastodon-peers",
        local_app_data={
            "secret-id": secret.id,
            "migrated-version": VERSION,
            "post-migrated-version": VERSION,
        },
    )
    cluster = testing.Relation(
        endpoint="cluster",
        remote_app_name="mastodon-workers",
        remote_units_data={0: {"active-version": VERSION}},  # still on the old release
    )
    redis = testing.Relation(
        endpoint="redis",
        remote_app_name="redis",
        remote_units_data={0: {"hostname": "10.20.0.7", "port": "6379"}},
    )
    s3 = testing.Relation(
        endpoint="s3",
        remote_app_name="s3-integrator",
        remote_app_data={"bucket": "m", "access-key": "a", "secret-key": "s"},
    )
    state = testing.State(
        leader=True,
        config={"server-hostname": HOSTNAME, "version": new_version},
        relations={peer, db_relation(), cluster, redis, s3},
        secrets={secret},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    # Pre-deployment migrations ran, but post waits for the aux app.
    assert workload["run_migrations"].call_args_list == [
        unittest.mock.call(skip_post_deployment=True)
    ]
    assert out.get_relation(peer.id).local_app_data["post-migrated-version"] == VERSION

    # Once the auxiliary reports the new release, post migrations run.
    cluster2 = testing.Relation(
        endpoint="cluster",
        remote_app_name="mastodon-workers",
        remote_units_data={0: {"active-version": new_version}},
    )
    peer2 = testing.PeerRelation(
        endpoint="mastodon-peers",
        local_app_data={
            "secret-id": secret.id,
            "migrated-version": new_version,
            "post-migrated-version": VERSION,
        },
    )
    workload["run_migrations"].reset_mock()
    state2 = testing.State(
        leader=True,
        config={"server-hostname": HOSTNAME, "version": new_version},
        relations={peer2, db_relation(), cluster2, redis, s3},
        secrets={secret},
    )
    out2 = ctx.run(ctx.on.relation_changed(cluster2), state2)
    assert workload["run_migrations"].call_args_list == [unittest.mock.call()]
    assert out2.get_relation(peer2.id).local_app_data["post-migrated-version"] == new_version
