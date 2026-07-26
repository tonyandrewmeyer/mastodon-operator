#!/usr/bin/env python3
# Copyright 2026 Tony Meyer
# See LICENSE file for licensing details.

"""Workload manager for the Mastodon machine charm.

This module contains all the logic that touches the machine: package
installation, building Mastodon releases, rendering configuration and driving
systemd services. It is deliberately free of any ops/charm imports so it can
be unit-tested (and mocked) in isolation.
"""

import base64
import logging
import os
import pwd
import re
import secrets
import shlex
import shutil
import string
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import jinja2

logger = logging.getLogger(__name__)

USER = "mastodon"
GROUP = "mastodon"
HOME = Path("/home/mastodon")
RELEASES_DIR = HOME / "releases"
APP_DIR = HOME / "live"  # symlink to the active release
ENV_FILE = APP_DIR / ".env.production"
RBENV_ROOT = HOME / ".rbenv"
RBENV_BIN = RBENV_ROOT / "bin" / "rbenv"
MEDIA_ROOT = Path("/var/lib/mastodon")
MEDIA_DIR = MEDIA_ROOT / "media"
BUILD_MARKER = ".charm-build-complete"

NODE_MAJOR = 24
NODESOURCE_KEY_URL = "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key"
NODESOURCE_KEYRING = Path("/etc/apt/keyrings/nodesource.gpg")
NODESOURCE_LIST = Path("/etc/apt/sources.list.d/nodesource.list")

NGINX_SITE = Path("/etc/nginx/sites-available/mastodon")
NGINX_SITE_LINK = Path("/etc/nginx/sites-enabled/mastodon")
NGINX_DEFAULT_SITE = Path("/etc/nginx/sites-enabled/default")
TLS_DIR = Path("/etc/nginx/mastodon-tls")
TLS_CERT = TLS_DIR / "mastodon.crt"
TLS_KEY = TLS_DIR / "mastodon.key"

SYSTEMD_DIR = Path("/etc/systemd/system")
SERVICES = ("mastodon-web", "mastodon-sidekiq", "mastodon-streaming")

# Services run by each value of the role config option.
ROLE_SERVICES = {
    "all": SERVICES,
    "web": ("mastodon-web",),
    "sidekiq": ("mastodon-sidekiq",),
    "streaming": ("mastodon-streaming",),
}

# Local Prometheus metrics servers (scraped by grafana-agent via cos-agent).
# Web and sidekiq each run a prometheus_exporter LocalServer; the streaming
# server exposes /metrics on its main port.
WEB_METRICS_PORT = 9394
SIDEKIQ_METRICS_PORT = 9395
STREAMING_PORT = 4000

CLEANUP_UNIT = "mastodon-cleanup"
# Upstream default for `tootctl preview_cards remove`.
PREVIEW_CARDS_RETENTION_DAYS = 180

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Build dependencies for Ruby and the native gems, plus Mastodon's runtime
# dependencies, per https://docs.joinmastodon.org/admin/install/
SYSTEM_PACKAGES = [
    "autoconf",
    "bison",
    "build-essential",
    "ca-certificates",
    "curl",
    "ffmpeg",
    "file",
    "g++",
    "gcc",
    "git",
    "imagemagick",
    "libffi-dev",
    "libgdbm-dev",
    "libicu-dev",
    "libidn-dev",
    "libjemalloc-dev",
    "libncurses-dev",
    "libpq-dev",
    "libreadline-dev",
    "libssl-dev",
    "libvips42t64",
    "libxml2-dev",
    "libxslt1-dev",
    "libyaml-dev",
    "nginx",
    "openssl",
    "pkg-config",
    "redis-server",
    "tzdata",
    "zlib1g-dev",
]

# Environment keys owned by the charm; users may not override these through
# the extra-env config option.
MANAGED_ENV_KEYS = frozenset(
    {
        "LOCAL_DOMAIN",
        "WEB_DOMAIN",
        "SINGLE_USER_MODE",
        "DEFAULT_LOCALE",
        "SECRET_KEY_BASE",
        "OTP_SECRET",
        "VAPID_PRIVATE_KEY",
        "VAPID_PUBLIC_KEY",
        "ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY",
        "ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT",
        "ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASS",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_PASSWORD",
        "S3_ENABLED",
        "S3_BUCKET",
        "S3_REGION",
        "S3_ENDPOINT",
        "S3_OVERRIDE_PATH_STYLE",
        "ES_ENABLED",
        "ES_HOST",
        "ES_PORT",
        "ES_PRESET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "SMTP_SERVER",
        "SMTP_PORT",
        "SMTP_LOGIN",
        "SMTP_PASSWORD",
        "SMTP_FROM_ADDRESS",
        "SMTP_DOMAIN",
        "SMTP_TLS",
        "SMTP_ENABLE_STARTTLS",
        "TRUSTED_PROXY_IP",
        "MASTODON_PROMETHEUS_EXPORTER_ENABLED",
        "MASTODON_PROMETHEUS_EXPORTER_LOCAL",
        "RAILS_ENV",
        "NODE_ENV",
    }
)


class WorkloadError(Exception):
    """Raised when an operation on the workload fails."""


def _user_env() -> dict:
    return {
        "HOME": str(HOME),
        "PATH": f"{RBENV_ROOT}/shims:{RBENV_ROOT}/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "RAILS_ENV": "production",
        "NODE_ENV": "production",
    }


def _run(
    cmd: list,
    *,
    as_mastodon: bool = False,
    cwd: Path | None = None,
    env: dict | None = None,
    timeout: int = 3600,
) -> str:
    """Run a command, optionally as the mastodon user, and return its output.

    Raises WorkloadError (with the tail of the combined output) on failure.
    """
    run_env = dict(os.environ)
    if as_mastodon:
        run_env = _user_env()
    if env:
        run_env.update(env)
    kwargs: dict = {}
    if as_mastodon:
        kwargs.update(user=USER, group=GROUP)
    logger.info(
        "Running %s%s", shlex.join(str(c) for c in cmd), " (as mastodon)" if as_mastodon else ""
    )
    try:
        result = subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd) if cwd else None,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=True,
            **kwargs,
        )
    except subprocess.CalledProcessError as e:
        tail = "\n".join((e.output or "").splitlines()[-25:])
        logger.error("Command %s failed (rc=%s):\n%s", cmd, e.returncode, tail)
        raise WorkloadError(f"{cmd[0]} failed (rc={e.returncode}): {tail[-400:]}") from e
    except subprocess.TimeoutExpired as e:
        raise WorkloadError(f"{cmd[0]} timed out after {timeout}s") from e
    logger.debug("Output: %s", result.stdout)
    return result.stdout


def _write_file(
    path: Path,
    content: str,
    *,
    mode: int = 0o644,
    owner: str = "root",
    group: str = "root",
) -> bool:
    """Write a file if its content changed. Returns True when modified."""
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    shutil.chown(tmp, owner, group)
    tmp.rename(path)
    logger.info("Wrote %s", path)
    return True


def _render_template(name: str, context: dict) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    return env.get_template(name).render(**context)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def install_packages() -> None:
    """Install all system dependencies, including Node.js from Nodesource."""
    from charmlibs import apt

    _setup_nodesource()
    apt.update()
    try:
        apt.add_package([*SYSTEM_PACKAGES, "nodejs"])
    except apt.PackageNotFoundError as e:
        raise WorkloadError(f"failed to install packages: {e}") from e
    # Yarn 4 is provisioned through corepack, shipped with Node.js >= 16.
    _run(["corepack", "enable"])


def _setup_nodesource() -> None:
    """Configure the Nodesource apt repository for Node.js."""
    if NODESOURCE_LIST.exists() and NODESOURCE_KEYRING.exists():
        return
    NODESOURCE_KEYRING.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(NODESOURCE_KEY_URL, timeout=60) as resp:
        armored = resp.read()
    with tempfile.NamedTemporaryFile() as f:
        f.write(armored)
        f.flush()
        NODESOURCE_KEYRING.unlink(missing_ok=True)
        _run(["gpg", "--dearmor", "-o", str(NODESOURCE_KEYRING), f.name])
    NODESOURCE_LIST.write_text(
        f"deb [signed-by={NODESOURCE_KEYRING}] "
        f"https://deb.nodesource.com/node_{NODE_MAJOR}.x nodistro main\n"
    )


def ensure_user() -> None:
    """Create the mastodon system user and base directories."""
    from charmlibs import passwd

    passwd.add_user(USER, home_dir=str(HOME), system_user=True, create_home=True)
    # nginx (www-data) must be able to traverse into the app's public/ dir.
    os.chmod(HOME, 0o751)
    RELEASES_DIR.mkdir(exist_ok=True)
    shutil.chown(RELEASES_DIR, USER, GROUP)


def ensure_media_dirs() -> None:
    """Create the media directory on the storage mount."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(MEDIA_ROOT, USER, GROUP)
    shutil.chown(MEDIA_DIR, USER, GROUP)
    os.chmod(MEDIA_ROOT, 0o755)
    os.chmod(MEDIA_DIR, 0o755)


def ensure_rbenv() -> None:
    """Install (or update) rbenv and ruby-build for the mastodon user."""
    if not (RBENV_ROOT / ".git").exists():
        _run(
            ["git", "clone", "https://github.com/rbenv/rbenv.git", str(RBENV_ROOT)],
            as_mastodon=True,
        )
    ruby_build = RBENV_ROOT / "plugins" / "ruby-build"
    if not (ruby_build / ".git").exists():
        ruby_build.parent.mkdir(parents=True, exist_ok=True)
        shutil.chown(ruby_build.parent, USER, GROUP)
        _run(
            ["git", "clone", "https://github.com/rbenv/ruby-build.git", str(ruby_build)],
            as_mastodon=True,
        )
    else:
        # Refresh ruby-build so new Ruby releases are available on upgrade.
        _run(["git", "-C", str(ruby_build), "pull", "--ff-only"], as_mastodon=True)


def ensure_ruby(version: str) -> None:
    """Compile the requested Ruby version with jemalloc, if not present."""
    if (RBENV_ROOT / "versions" / version).exists():
        return
    logger.info("Building Ruby %s (this takes several minutes)", version)
    _run(
        [str(RBENV_BIN), "install", "--skip-existing", version],
        as_mastodon=True,
        env={"RUBY_CONFIGURE_OPTS": "--with-jemalloc", "MAKE_OPTS": f"-j{os.cpu_count() or 2}"},
        timeout=3600,
    )
    _run([str(RBENV_BIN), "global", version], as_mastodon=True)


def release_dir(version: str) -> Path:
    """Directory holding the given release."""
    return RELEASES_DIR / version


def is_release_built(version: str, role: str = "all") -> bool:
    """Whether the given release has been built sufficiently for `role`.

    A full build satisfies every role; streaming-only units get by with the
    much lighter JavaScript-only build.
    """
    if (release_dir(version) / BUILD_MARKER).exists():
        return True
    if role == "streaming":
        return (release_dir(version) / f"{BUILD_MARKER}-streaming").exists()
    return False


def installed_version() -> str | None:
    """Version of the currently activated release, or None."""
    if not APP_DIR.is_symlink():
        return None
    return Path(os.readlink(APP_DIR)).name


def fetch_app(version: str) -> None:
    """Download and unpack the given Mastodon release tarball."""
    target = release_dir(version)
    if (target / "Gemfile").exists():
        return
    url = f"https://github.com/mastodon/mastodon/archive/refs/tags/{version}.tar.gz"
    logger.info("Downloading %s", url)
    with tempfile.TemporaryDirectory(dir=RELEASES_DIR) as tmp:
        tarball = Path(tmp) / "mastodon.tar.gz"
        with urllib.request.urlopen(url, timeout=300) as resp, tarball.open("wb") as f:
            shutil.copyfileobj(resp, f)
        extract = Path(tmp) / "extract"
        extract.mkdir()
        _run(["tar", "-xzf", str(tarball), "-C", str(extract), "--strip-components=1"])
        shutil.rmtree(target, ignore_errors=True)
        extract.rename(target)
    _run(["chown", "-R", f"{USER}:{GROUP}", str(target)])


def read_ruby_version(version: str) -> str:
    """Ruby version required by the given (fetched) release."""
    return (release_dir(version) / ".ruby-version").read_text().strip()


def build_app(version: str, role: str = "all") -> None:
    """Install dependencies and precompile assets for a release.

    Streaming-only units need just the Node.js dependencies; every other
    role gets the full build (Ruby gems and precompiled assets).
    """
    target = release_dir(version)
    if is_release_built(version, role):
        return
    full_build = role != "streaming"

    if full_build:
        ensure_ruby(read_ruby_version(version))
        logger.info("Installing Ruby gems for %s", version)
        _run(
            ["bundle", "config", "set", "--local", "deployment", "true"],
            as_mastodon=True,
            cwd=target,
        )
        _run(
            ["bundle", "config", "set", "--local", "without", "development test"],
            as_mastodon=True,
            cwd=target,
        )
        _run(
            ["bundle", "install", "-j", str(os.cpu_count() or 2)],
            as_mastodon=True,
            cwd=target,
            timeout=3600,
        )

    logger.info("Installing JavaScript dependencies for %s", version)
    _run(
        ["yarn", "install", "--immutable"],
        as_mastodon=True,
        cwd=target,
        env={"COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"},
        timeout=3600,
    )

    if full_build:
        logger.info("Precompiling assets for %s", version)
        _run(
            ["bundle", "exec", "rails", "assets:precompile"],
            as_mastodon=True,
            cwd=target,
            env={"SECRET_KEY_BASE_DUMMY": "1", "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"},
            timeout=3600,
        )

    # Media lives on Juju storage (or S3); replace public/system with a link.
    system_dir = target / "public" / "system"
    if not system_dir.is_symlink():
        shutil.rmtree(system_dir, ignore_errors=True)
        system_dir.symlink_to(MEDIA_DIR)
        os.lchown(system_dir, pwd.getpwnam(USER).pw_uid, pwd.getpwnam(USER).pw_gid)

    marker = target / (BUILD_MARKER if full_build else f"{BUILD_MARKER}-streaming")
    marker.touch()
    shutil.chown(marker, USER, GROUP)


def activate_release(version: str) -> bool:
    """Point the live symlink at the given release. Returns True if changed."""
    if installed_version() == version:
        return False
    # Carry the environment file over to the new release.
    old_env = ENV_FILE.read_text() if ENV_FILE.exists() else None
    tmp_link = HOME / ".live.tmp"
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(release_dir(version))
    tmp_link.rename(APP_DIR)
    if old_env is not None and not ENV_FILE.exists():
        write_env_text(old_env)
    logger.info("Activated Mastodon %s", version)
    return True


def prune_releases(keep: int = 2) -> None:
    """Remove old release directories, keeping the newest `keep` ones."""
    if not RELEASES_DIR.exists():
        return
    active = installed_version()
    releases = sorted(
        (p for p in RELEASES_DIR.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for release in releases[keep:]:
        if release.name != active:
            logger.info("Pruning old release %s", release.name)
            shutil.rmtree(release, ignore_errors=True)


# ---------------------------------------------------------------------------
# Configuration rendering
# ---------------------------------------------------------------------------


def generate_secrets() -> dict:
    """Generate the full set of long-lived Mastodon secrets.

    Formats match what `rake secret`, `rails db:encryption:init` and
    `rake mastodon:webpush:generate_vapid_key` would produce.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_value = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_point = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    alphanum = string.ascii_letters + string.digits
    return {
        "secret-key-base": secrets.token_hex(64),
        "otp-secret": secrets.token_hex(64),
        "vapid-private-key": base64.urlsafe_b64encode(private_value).decode(),
        "vapid-public-key": base64.urlsafe_b64encode(public_point).decode(),
        "ar-deterministic-key": "".join(secrets.choice(alphanum) for _ in range(32)),
        "ar-key-derivation-salt": "".join(secrets.choice(alphanum) for _ in range(32)),
        "ar-primary-key": "".join(secrets.choice(alphanum) for _ in range(32)),
    }


def render_env(
    *,
    hostname: str,
    web_domain: str = "",
    single_user_mode: bool = False,
    default_locale: str = "en",
    app_secrets: dict,
    db: dict,
    redis: dict | None = None,
    s3: dict | None = None,
    smtp: dict | None = None,
    es: dict | None = None,
    trusted_proxy_ips: str = "",
    extra_env: str = "",
) -> dict:
    """Build the .env.production contents as an ordered dict.

    `db` requires host, port, dbname, user, password. `redis` requires host
    and port (password optional); None means local Redis. `s3` follows the
    data_platform_libs s3 connection-info keys. `smtp` requires server, port,
    from_address (login/password/encryption optional). `es` requires host
    (port and preset optional); credentials for authenticated clusters can be
    supplied through extra-env (ES_USER/ES_PASS are intentionally unmanaged).
    """
    env: dict = {
        "LOCAL_DOMAIN": hostname,
        "SINGLE_USER_MODE": "true" if single_user_mode else "false",
        "DEFAULT_LOCALE": default_locale,
        "SECRET_KEY_BASE": app_secrets["secret-key-base"],
        "OTP_SECRET": app_secrets["otp-secret"],
        "VAPID_PRIVATE_KEY": app_secrets["vapid-private-key"],
        "VAPID_PUBLIC_KEY": app_secrets["vapid-public-key"],
        "ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY": app_secrets["ar-deterministic-key"],
        "ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT": app_secrets["ar-key-derivation-salt"],
        "ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY": app_secrets["ar-primary-key"],
        "DB_HOST": db["host"],
        "DB_PORT": str(db["port"]),
        "DB_NAME": db["dbname"],
        "DB_USER": db["user"],
        "DB_PASS": db["password"],
    }
    if web_domain:
        env["WEB_DOMAIN"] = web_domain

    if redis:
        env["REDIS_HOST"] = redis["host"]
        env["REDIS_PORT"] = str(redis["port"])
        if redis.get("password"):
            env["REDIS_PASSWORD"] = redis["password"]
    else:
        env["REDIS_HOST"] = "127.0.0.1"
        env["REDIS_PORT"] = "6379"

    if s3:
        env["S3_ENABLED"] = "true"
        env["S3_BUCKET"] = s3["bucket"]
        env["AWS_ACCESS_KEY_ID"] = s3["access-key"]
        env["AWS_SECRET_ACCESS_KEY"] = s3["secret-key"]
        if s3.get("region"):
            env["S3_REGION"] = s3["region"]
        if s3.get("endpoint"):
            env["S3_ENDPOINT"] = s3["endpoint"]
        if s3.get("s3-uri-style") == "path":
            # Mastodon defaults to path-style; setting this to true selects
            # virtual-host style. Only set it for explicit "host" style.
            pass
        elif s3.get("s3-uri-style") == "host":
            env["S3_OVERRIDE_PATH_STYLE"] = "true"

    if es and es.get("host"):
        env["ES_ENABLED"] = "true"
        env["ES_HOST"] = es["host"]
        env["ES_PORT"] = str(es.get("port") or "9200")
        if es.get("preset"):
            env["ES_PRESET"] = es["preset"]

    # Local Prometheus exporters, scraped through the cos-agent integration.
    # The per-process ports are set in the systemd units.
    env["MASTODON_PROMETHEUS_EXPORTER_ENABLED"] = "true"
    env["MASTODON_PROMETHEUS_EXPORTER_LOCAL"] = "true"

    env.update(_smtp_env(smtp, hostname))

    if trusted_proxy_ips:
        env["TRUSTED_PROXY_IP"] = trusted_proxy_ips

    _merge_extra_env(env, extra_env)
    return env


def _smtp_env(smtp: dict | None, hostname: str) -> dict:
    """SMTP-related environment variables (empty when not configured)."""
    if not smtp or not smtp.get("server"):
        return {}
    env = {"SMTP_SERVER": smtp["server"], "SMTP_PORT": str(smtp.get("port", 587))}
    if smtp.get("login"):
        env["SMTP_LOGIN"] = smtp["login"]
    if smtp.get("password"):
        env["SMTP_PASSWORD"] = smtp["password"]
    env["SMTP_FROM_ADDRESS"] = smtp.get("from_address") or f"notifications@{hostname}"
    if smtp.get("domain"):
        env["SMTP_DOMAIN"] = smtp["domain"]
    encryption = smtp.get("encryption", "starttls")
    if encryption == "tls":
        env["SMTP_TLS"] = "true"
    elif encryption == "none":
        env["SMTP_ENABLE_STARTTLS"] = "never"
    else:
        env["SMTP_ENABLE_STARTTLS"] = "auto"
    return env


def _merge_extra_env(env: dict, extra_env: str) -> None:
    """Merge user-provided KEY=VALUE lines, refusing charm-managed keys."""
    for line in (extra_env or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.warning("Ignoring malformed extra-env line: %r", line)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in MANAGED_ENV_KEYS or key in env:
            logger.warning("Ignoring extra-env override of managed key %s", key)
            continue
        env[key] = value.strip()


def _quote_env_value(value: str) -> str:
    if "\n" in value:
        raise WorkloadError("environment values must not contain newlines")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def env_file_text(env: dict) -> str:
    """Serialize an environment dict into .env.production file content."""
    lines = ["# Managed by the mastodon charm. Manual changes will be overwritten."]
    lines += [f"{key}={_quote_env_value(str(value))}" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def write_env_text(text: str) -> bool:
    """Write .env.production. Returns True when the content changed."""
    return _write_file(ENV_FILE, text, mode=0o640, owner=USER, group=GROUP)


def install_systemd_units(
    *, role: str, web_concurrency: int, max_threads: int, sidekiq_concurrency: int
) -> bool:
    """Render the role's service units (removing the others' files).

    Returns True when anything changed.
    """
    from charmlibs import systemd

    context = {
        "app_dir": str(APP_DIR),
        "media_root": str(MEDIA_ROOT),
        "rbenv_shims": str(RBENV_ROOT / "shims"),
        "web_concurrency": web_concurrency,
        "max_threads": max_threads,
        "sidekiq_concurrency": sidekiq_concurrency,
        "web_metrics_port": WEB_METRICS_PORT,
        "sidekiq_metrics_port": SIDEKIQ_METRICS_PORT,
    }
    wanted = ROLE_SERVICES[role]
    changed = False
    for service in SERVICES:
        unit_path = SYSTEMD_DIR / f"{service}.service"
        if service in wanted:
            content = _render_template(f"{service}.service.j2", context)
            changed |= _write_file(unit_path, content)
        elif unit_path.exists():
            if systemd.service_running(service):
                systemd.service_stop(service)
            systemd.service_disable(service)
            unit_path.unlink()
            changed = True
    if changed:
        systemd.daemon_reload()
    return changed


def configure_cleanup_timer(retention_days: int) -> None:
    """Install (or remove) the daily media cache cleanup timer.

    Per Mastodon's storage optimization guidance, cached remote media and
    old link-preview cards are pruned on a schedule; local user uploads are
    never touched. retention_days <= 0 disables the timer.
    """
    from charmlibs import systemd

    service_path = SYSTEMD_DIR / f"{CLEANUP_UNIT}.service"
    timer_path = SYSTEMD_DIR / f"{CLEANUP_UNIT}.timer"
    if retention_days <= 0:
        if timer_path.exists() or service_path.exists():
            systemd.service_disable("--now", f"{CLEANUP_UNIT}.timer")
            timer_path.unlink(missing_ok=True)
            service_path.unlink(missing_ok=True)
            systemd.daemon_reload()
        return
    context = {
        "app_dir": str(APP_DIR),
        "rbenv_shims": str(RBENV_ROOT / "shims"),
        "retention_days": retention_days,
        "preview_retention_days": PREVIEW_CARDS_RETENTION_DAYS,
    }
    changed = _write_file(service_path, _render_template(f"{CLEANUP_UNIT}.service.j2", context))
    changed |= _write_file(timer_path, _render_template(f"{CLEANUP_UNIT}.timer.j2", context))
    if changed:
        systemd.daemon_reload()
    systemd.service_enable("--now", f"{CLEANUP_UNIT}.timer")


def ensure_tls_material(hostname: str, cert_pem: str | None, key_pem: str | None) -> bool:
    """Install the given TLS cert/key, or a self-signed one. True if changed."""
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    marker = TLS_DIR / ".self-signed-cn"
    if cert_pem and key_pem:
        changed = _write_file(TLS_CERT, cert_pem)
        changed |= _write_file(TLS_KEY, key_pem, mode=0o600)
        # The installed material is no longer self-signed; without this, a
        # later fallback to self-signed would keep serving the stale cert.
        marker.unlink(missing_ok=True)
        return changed
    if (
        TLS_CERT.exists()
        and TLS_KEY.exists()
        and marker.exists()
        and marker.read_text() == hostname
    ):
        return False
    logger.info("Generating self-signed certificate for %s", hostname)
    _run(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-days",
            "3650",
            "-subj",
            f"/CN={hostname}",
            "-addext",
            f"subjectAltName=DNS:{hostname}",
            "-keyout",
            str(TLS_KEY),
            "-out",
            str(TLS_CERT),
        ]
    )
    os.chmod(TLS_KEY, 0o600)
    marker.write_text(hostname)
    return True


def configure_nginx(*, hostname: str, behind_proxy: bool, role: str = "all") -> bool:
    """Render, enable and apply the nginx vhost. Returns True when changed.

    A marker records the digest of the configuration nginx last successfully
    loaded. Validation and reload are retried until they succeed, so a config
    that was written but never applied (e.g. an earlier failed hook) is not
    skipped just because the file content is already up to date.
    """
    import hashlib

    from charmlibs import systemd

    services = ROLE_SERVICES[role]
    content = _render_template(
        "nginx.conf.j2",
        {
            "hostname": hostname,
            "behind_proxy": behind_proxy,
            "app_dir": str(APP_DIR),
            "tls_cert_path": str(TLS_CERT),
            "tls_key_path": str(TLS_KEY),
            "has_web": "mastodon-web" in services,
            "has_streaming": "mastodon-streaming" in services,
        },
    )
    _write_file(NGINX_SITE, content)
    if not NGINX_SITE_LINK.exists():
        NGINX_SITE_LINK.parent.mkdir(parents=True, exist_ok=True)
        NGINX_SITE_LINK.symlink_to(NGINX_SITE)
    if NGINX_DEFAULT_SITE.exists() or NGINX_DEFAULT_SITE.is_symlink():
        NGINX_DEFAULT_SITE.unlink()

    tls_material = b""
    if not behind_proxy:
        tls_material = TLS_CERT.read_bytes() + TLS_KEY.read_bytes()
    digest = hashlib.sha256(content.encode() + tls_material).hexdigest()
    marker = Path("/etc/nginx/.mastodon-charm-applied")
    if marker.exists() and marker.read_text() == digest:
        return False

    _run(["nginx", "-t"])
    systemd.service_enable("nginx")
    systemd.service_reload("nginx", restart_on_failure=True)
    marker.write_text(digest)
    return True


# ---------------------------------------------------------------------------
# Service management
# ---------------------------------------------------------------------------


def disable_nginx() -> None:
    """Stop and disable nginx (for units whose role does not serve HTTP)."""
    from charmlibs import systemd

    if NGINX_SITE_LINK.exists() or NGINX_SITE_LINK.is_symlink():
        NGINX_SITE_LINK.unlink()
    Path("/etc/nginx/.mastodon-charm-applied").unlink(missing_ok=True)
    if systemd.service_running("nginx"):
        systemd.service_stop("nginx")
    systemd.service_disable("nginx")


def _ensure_redis_durability() -> None:
    """Enable AOF persistence on the colocated Redis.

    Sidekiq queues live in Redis; with the default snapshot-only persistence
    a crash loses queued jobs (deliveries, e-mails). Mastodon's docs
    recommend appendonly for production.
    """
    from charmlibs import systemd

    conf = Path("/etc/redis/redis.conf")
    if not conf.exists():
        return
    text = conf.read_text()
    if re.search(r"^appendonly yes", text, flags=re.M):
        return
    new_text, count = re.subn(r"^appendonly no", "appendonly yes", text, count=1, flags=re.M)
    if not count:
        new_text = text + "\nappendonly yes\n"
    conf.write_text(new_text)
    logger.info("Enabled Redis appendonly persistence")
    if systemd.service_running("redis-server"):
        systemd.service_restart("redis-server")


def set_local_redis(enabled: bool) -> None:
    """Start or stop the colocated redis-server."""
    from charmlibs import systemd

    if enabled:
        _ensure_redis_durability()
        systemd.service_enable("redis-server")
        if not systemd.service_running("redis-server"):
            systemd.service_start("redis-server")
    else:
        systemd.service_disable("redis-server")
        if systemd.service_running("redis-server"):
            systemd.service_stop("redis-server")


def enable_services(role: str = "all") -> None:
    """Enable the role's Mastodon services to start at boot."""
    from charmlibs import systemd

    for service in ROLE_SERVICES[role]:
        systemd.service_enable(service)


def restart_services(role: str = "all") -> None:
    """Restart the role's Mastodon services."""
    from charmlibs import systemd

    for service in ROLE_SERVICES[role]:
        systemd.service_restart(service)


def stop_services() -> None:
    """Stop all running Mastodon services."""
    from charmlibs import systemd

    for service in SERVICES:
        if (SYSTEMD_DIR / f"{service}.service").exists() and systemd.service_running(service):
            systemd.service_stop(service)


def services_running(role: str = "all") -> dict:
    """Map of the role's service names to whether they are active."""
    from charmlibs import systemd

    return {service: systemd.service_running(service) for service in ROLE_SERVICES[role]}


# ---------------------------------------------------------------------------
# Database / admin operations
# ---------------------------------------------------------------------------


def prepare_database() -> None:
    """Create the schema on first run, or run pending migrations.

    `rails db:prepare` loads the schema (and seeds) into an empty database
    and runs `db:migrate` on an existing one.
    """
    _run(
        ["bundle", "exec", "rails", "db:prepare"],
        as_mastodon=True,
        cwd=APP_DIR,
        timeout=3600,
    )


def run_migrations(*, skip_post_deployment: bool = False) -> None:
    """Run database migrations, optionally excluding post-deployment ones.

    Mastodon's upgrade procedure runs migrations in two phases: with
    SKIP_POST_DEPLOYMENT_MIGRATIONS=true before the new code is started
    (these are safe against the old code), and the remaining post-deployment
    migrations once the services run the new release.
    """
    env = {"SKIP_POST_DEPLOYMENT_MIGRATIONS": "true"} if skip_post_deployment else {}
    _run(
        ["bundle", "exec", "rails", "db:migrate"],
        as_mastodon=True,
        cwd=APP_DIR,
        env=env,
        timeout=3600,
    )


def tootctl(args: list) -> str:
    """Run a tootctl command and return its output."""
    return _run(
        ["bundle", "exec", "bin/tootctl", *args],
        as_mastodon=True,
        cwd=APP_DIR,
        timeout=3600,
    )
