# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the pure helpers in the workload module."""

import base64

import pytest

import mastodon

SECRETS = {
    "secret-key-base": "k" * 32,
    "otp-secret": "o" * 32,
    "vapid-private-key": "priv",
    "vapid-public-key": "pub",
    "ar-deterministic-key": "d" * 32,
    "ar-key-derivation-salt": "s" * 32,
    "ar-primary-key": "p" * 32,
}

DB = {"host": "10.0.0.1", "port": "5432", "dbname": "mastodon", "user": "u", "password": "p"}


def render(**kwargs):
    defaults = {"hostname": "social.example.com", "app_secrets": SECRETS, "db": DB}
    defaults.update(kwargs)
    return mastodon.render_env(**defaults)


def test_render_env_basics():
    env = render()
    assert env["LOCAL_DOMAIN"] == "social.example.com"
    assert env["DB_HOST"] == "10.0.0.1"
    assert env["REDIS_HOST"] == "127.0.0.1"
    assert "WEB_DOMAIN" not in env
    assert "S3_ENABLED" not in env
    assert "SMTP_SERVER" not in env


def test_render_env_redis_password():
    env = render(redis={"host": "r", "port": 6380, "password": "pw"})
    assert env["REDIS_HOST"] == "r"
    assert env["REDIS_PORT"] == "6380"
    assert env["REDIS_PASSWORD"] == "pw"


def test_render_env_smtp_starttls():
    env = render(smtp={"server": "mail", "port": 587, "encryption": "starttls"})
    assert env["SMTP_SERVER"] == "mail"
    assert env["SMTP_ENABLE_STARTTLS"] == "auto"
    assert env["SMTP_FROM_ADDRESS"] == "notifications@social.example.com"


def test_render_env_smtp_tls_and_none():
    assert render(smtp={"server": "m", "encryption": "tls"})["SMTP_TLS"] == "true"
    env = render(smtp={"server": "m", "encryption": "none"})
    assert env["SMTP_ENABLE_STARTTLS"] == "never"


def test_render_env_s3_uri_style():
    s3 = {"bucket": "b", "access-key": "a", "secret-key": "s", "s3-uri-style": "host"}
    assert render(s3=s3)["S3_OVERRIDE_PATH_STYLE"] == "true"
    s3["s3-uri-style"] = "path"
    assert "S3_OVERRIDE_PATH_STYLE" not in render(s3=s3)


def test_render_env_elasticsearch():
    env = render(es={"host": "es.local", "port": "9201", "preset": "small_cluster"})
    assert env["ES_ENABLED"] == "true"
    assert env["ES_HOST"] == "es.local"
    assert env["ES_PORT"] == "9201"
    assert env["ES_PRESET"] == "small_cluster"
    # ES auth stays user-settable through extra-env.
    env = render(es={"host": "es.local"}, extra_env="ES_USER=elastic\nES_PASS=changeme\n")
    assert env["ES_USER"] == "elastic"
    assert env["ES_PASS"] == "changeme"


def test_extra_env_appended_but_cannot_override_managed():
    env = render(
        extra_env="OMNIAUTH_ONLY=true\nDB_HOST=evil\nES_ENABLED=true\n# comment\nbroken\n"
    )
    assert env["OMNIAUTH_ONLY"] == "true"
    assert env["DB_HOST"] == "10.0.0.1"
    assert "ES_ENABLED" not in env  # managed key, override rejected


def test_env_file_text_quoting():
    text = mastodon.env_file_text({"A": 'va "l" ue', "B": "plain"})
    assert 'A="va \\"l\\" ue"' in text
    assert 'B="plain"' in text
    assert text.startswith("#")
    assert text.endswith("\n")


def test_env_value_newline_rejected():
    with pytest.raises(mastodon.WorkloadError):
        mastodon.env_file_text({"A": "a\nb"})


def test_generate_secrets_shapes():
    secrets = mastodon.generate_secrets()
    assert len(secrets["secret-key-base"]) == 128
    assert len(secrets["otp-secret"]) == 128
    assert len(secrets["ar-primary-key"]) == 32
    private = base64.urlsafe_b64decode(secrets["vapid-private-key"])
    public = base64.urlsafe_b64decode(secrets["vapid-public-key"])
    assert len(private) == 32
    assert len(public) == 65 and public[0] == 0x04


def test_templates_render():
    nginx = mastodon._render_template(
        "nginx.conf.j2",
        {
            "hostname": "social.example.com",
            "behind_proxy": False,
            "app_dir": "/home/mastodon/live",
            "tls_cert_path": "/etc/nginx/mastodon-tls/mastodon.crt",
            "tls_key_path": "/etc/nginx/mastodon-tls/mastodon.key",
        },
    )
    assert "server_name social.example.com;" in nginx
    assert "listen 443 ssl" in nginx
    assert "proxy_pass http://mastodon-backend;" in nginx

    proxied = mastodon._render_template(
        "nginx.conf.j2",
        {
            "hostname": "social.example.com",
            "behind_proxy": True,
            "app_dir": "/home/mastodon/live",
            "tls_cert_path": "",
            "tls_key_path": "",
        },
    )
    assert "listen 443" not in proxied
    assert "$mastodon_fwd_proto" in proxied

    for service in mastodon.SERVICES:
        unit = mastodon._render_template(
            f"{service}.service.j2",
            {
                "app_dir": "/home/mastodon/live",
                "media_root": "/var/lib/mastodon",
                "rbenv_shims": "/home/mastodon/.rbenv/shims",
                "web_concurrency": 2,
                "max_threads": 5,
                "sidekiq_concurrency": 25,
            },
        )
        assert f"Description={service}" in unit
        assert "EnvironmentFile=/home/mastodon/live/.env.production" in unit
