#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Machine charm for the Mastodon federated social network server."""

import logging
import re
import shlex

import ops
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.data_platform_libs.v0.s3 import S3Requirer
from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.smtp_integrator.v0.smtp import SecretError, SmtpRequires
from charms.tls_certificates_interface.v4.tls_certificates import (
    CertificateRequestAttributes,
    TLSCertificatesRequiresV4,
)

import mastodon

logger = logging.getLogger(__name__)

PEER_RELATION = "mastodon-peers"
DATABASE_RELATION = "database"
REDIS_RELATION = "redis"
S3_RELATION = "s3"
ELASTICSEARCH_RELATION = "elasticsearch"
CERTIFICATES_RELATION = "certificates"
SMTP_RELATION = "smtp"
WEBSITE_RELATION = "website"
DATABASE_NAME = "mastodon"
SECRETS_LABEL = "mastodon-app-secrets"
MIGRATED_VERSION_KEY = "migrated-version"
SECRET_ID_KEY = "secret-id"

VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
SMTP_ENCRYPTION_MODES = ("none", "starttls", "tls")
ES_PRESETS = ("single_node_cluster", "small_cluster", "large_cluster")


class MastodonCharm(ops.CharmBase):
    """Operate a Mastodon server on a machine."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self.database = DatabaseRequires(
            self, relation_name=DATABASE_RELATION, database_name=DATABASE_NAME
        )
        self.s3 = S3Requirer(self, S3_RELATION, bucket_name=DATABASE_NAME)
        # grafana-agent (subordinate) ships node metrics, machine logs and
        # Mastodon's own Prometheus metrics to a COS stack, along with our
        # bundled alert rules.
        self.cos_agent = COSAgentProvider(
            self,
            relation_name="cos-agent",
            metrics_endpoints=[
                {"path": "/metrics", "port": mastodon.WEB_METRICS_PORT},
                {"path": "/metrics", "port": mastodon.SIDEKIQ_METRICS_PORT},
                {"path": "/metrics", "port": mastodon.STREAMING_PORT},
            ],
            refresh_events=[self.on.config_changed, self.on.upgrade_charm],
        )

        self.smtp = SmtpRequires(self)
        framework.observe(self.smtp.on.smtp_data_available, self._reconcile)
        framework.observe(self.on[SMTP_RELATION].relation_broken, self._reconcile)

        self.certificates = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name=CERTIFICATES_RELATION,
            certificate_requests=self._certificate_requests(),
            refresh_events=[self.on.config_changed],
        )
        framework.observe(self.certificates.on.certificate_available, self._reconcile)
        framework.observe(self.on[CERTIFICATES_RELATION].relation_broken, self._reconcile)

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.upgrade_charm, self._on_install)
        framework.observe(self.on.stop, self._on_stop)
        framework.observe(self.on.collect_unit_status, self._on_collect_status)

        framework.observe(self.on.config_changed, self._reconcile)
        framework.observe(self.on.update_status, self._reconcile)
        framework.observe(self.on.leader_elected, self._reconcile)
        framework.observe(self.on.secret_changed, self._reconcile)
        framework.observe(self.on[PEER_RELATION].relation_created, self._reconcile)
        framework.observe(self.on[PEER_RELATION].relation_changed, self._reconcile)

        framework.observe(self.database.on.database_created, self._reconcile)
        framework.observe(self.database.on.endpoints_changed, self._reconcile)
        framework.observe(self.on[DATABASE_RELATION].relation_broken, self._on_database_broken)

        framework.observe(self.s3.on.credentials_changed, self._reconcile)
        framework.observe(self.s3.on.credentials_gone, self._reconcile)

        framework.observe(self.on[REDIS_RELATION].relation_changed, self._reconcile)
        framework.observe(self.on[REDIS_RELATION].relation_broken, self._reconcile)

        framework.observe(self.on[ELASTICSEARCH_RELATION].relation_changed, self._reconcile)
        framework.observe(self.on[ELASTICSEARCH_RELATION].relation_broken, self._reconcile)

        framework.observe(self.on[WEBSITE_RELATION].relation_joined, self._reconcile)

        framework.observe(self.on.media_storage_attached, self._on_media_storage_attached)

        framework.observe(self.on.create_admin_action, self._on_create_admin_action)
        framework.observe(self.on.tootctl_action, self._on_tootctl_action)
        framework.observe(self.on.media_cleanup_action, self._on_media_cleanup_action)

    # ------------------------------------------------------------------
    # Configuration / relation introspection
    # ------------------------------------------------------------------

    @property
    def _peer_relation(self) -> ops.Relation | None:
        return self.model.get_relation(PEER_RELATION)

    def _invalid_config_reason(self) -> str | None:
        """Return a human-readable reason when config is invalid, else None."""
        version = str(self.config["version"]).strip()
        if not VERSION_RE.match(version):
            return f'invalid version {version!r}, expected e.g. "v4.5.11"'
        if self.config["smtp-encryption"] not in SMTP_ENCRYPTION_MODES:
            return 'smtp-encryption must be one of "none", "starttls" or "tls"'
        if str(self.config["es-preset"]).strip() not in ES_PRESETS:
            return f"es-preset must be one of {', '.join(ES_PRESETS)}"
        tls_cert, tls_key = self.config["tls-certificate"], self.config["tls-key"]
        if bool(tls_cert) != bool(tls_key):
            return "tls-certificate and tls-key must be set together"
        if tls_cert and self._decoded_tls() is None:
            return "tls-certificate and tls-key must be valid base64-encoded PEM"
        return None

    def _certificate_requests(self) -> list[CertificateRequestAttributes]:
        """Certificate request for the configured hostname (and web-domain)."""
        hostname = str(self.config["server-hostname"]).strip()
        if not hostname:
            return []
        sans = {hostname}
        web_domain = str(self.config["web-domain"]).strip()
        if web_domain:
            sans.add(web_domain)
        return [CertificateRequestAttributes(common_name=hostname, sans_dns=frozenset(sans))]

    def _relation_tls(self) -> tuple[str, str] | None:
        """Certificate (full chain) and key from the certificates relation."""
        if self.model.get_relation(CERTIFICATES_RELATION) is None:
            return None
        requests = self._certificate_requests()
        if not requests:
            return None
        cert, key = self.certificates.get_assigned_certificate(certificate_request=requests[0])
        if cert is None or key is None:
            return None
        leaf = str(cert.certificate).strip()
        chain = [str(c).strip() for c in cert.chain]
        fullchain = [leaf] + [c for c in chain if c != leaf]
        return "\n".join(fullchain) + "\n", str(key)

    def _decoded_tls(self) -> tuple[str, str] | None:
        """Decode the configured TLS material, or None if unset/invalid."""
        import base64
        import binascii

        cert_b64 = str(self.config["tls-certificate"]).strip()
        key_b64 = str(self.config["tls-key"]).strip()
        if not cert_b64 or not key_b64:
            return None
        try:
            cert = base64.b64decode(cert_b64, validate=True).decode()
            key = base64.b64decode(key_b64, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
        if "BEGIN CERTIFICATE" not in cert or "PRIVATE KEY" not in key:
            return None
        return cert, key

    def _missing_scaling_integrations(self) -> list[str]:
        """Integrations required to run more than one unit but not present."""
        if self.app.planned_units() <= 1:
            return []
        missing = []
        if self.model.get_relation(REDIS_RELATION) is None:
            missing.append(REDIS_RELATION)
        if self._s3_info() is None:
            missing.append(S3_RELATION)
        return missing

    def _app_secrets(self) -> dict | None:
        """Fetch (or, on the leader, generate) the Mastodon app secrets."""
        peer = self._peer_relation
        if peer is None:
            return None
        secret_id = peer.data[self.app].get(SECRET_ID_KEY)
        if secret_id:
            try:
                return self.model.get_secret(id=secret_id).peek_content()
            except ops.SecretNotFoundError:
                logger.warning("Secret %s vanished; regenerating", secret_id)
        if not self.unit.is_leader():
            return None
        try:
            secret = self.model.get_secret(label=SECRETS_LABEL)
        except ops.SecretNotFoundError:
            secret = self.app.add_secret(mastodon.generate_secrets(), label=SECRETS_LABEL)
            logger.info("Generated new Mastodon application secrets")
        peer.data[self.app][SECRET_ID_KEY] = secret.id or ""
        return secret.peek_content()

    def _database_info(self) -> dict | None:
        """Connection info for PostgreSQL, or None when not (yet) available."""
        for data in self.database.fetch_relation_data().values():
            endpoints = data.get("endpoints")
            username = data.get("username")
            password = data.get("password")
            if not (endpoints and username and password):
                continue
            host, _, port = endpoints.split(",")[0].partition(":")
            return {
                "host": host,
                "port": port or "5432",
                "dbname": data.get("database", DATABASE_NAME),
                "user": username,
                "password": password,
            }
        return None

    def _redis_info(self) -> dict | None:
        """Connection info from the redis relation, or None when absent."""
        relation = self.model.get_relation(REDIS_RELATION)
        if relation is None or relation.app is None:
            return None
        bags = [relation.data[relation.app]]
        bags += [relation.data[unit] for unit in relation.units]
        for bag in bags:
            host = bag.get("hostname") or bag.get("host")
            if host:
                return {
                    "host": host,
                    "port": bag.get("port") or "6379",
                    "password": bag.get("password") or "",
                }
        return None

    def _s3_info(self) -> dict | None:
        """S3 connection info, or None when the integration is not usable."""
        if self.model.get_relation(S3_RELATION) is None:
            return None
        info = self.s3.get_s3_connection_info()
        if info.get("bucket") and info.get("access-key") and info.get("secret-key"):
            return dict(info)
        return None

    def _elasticsearch_info(self) -> dict | None:
        """Connection info from the elasticsearch relation, or None.

        Supports the legacy elasticsearch interface (host/port in the remote
        unit databags) as well as providers publishing application data.
        """
        relation = self.model.get_relation(ELASTICSEARCH_RELATION)
        if relation is None or relation.app is None:
            return None
        bags = [relation.data[relation.app]]
        bags += [relation.data[unit] for unit in relation.units]
        for bag in bags:
            host = bag.get("host") or bag.get("hostname") or bag.get("ingress-address")
            if host:
                return {
                    "host": host,
                    "port": bag.get("port") or "9200",
                    "preset": str(self.config["es-preset"]).strip(),
                }
        return None

    def _smtp_config(self) -> dict:
        """SMTP settings; the smtp integration takes precedence over config."""
        if self.model.get_relation(SMTP_RELATION) is not None:
            try:
                data = self.smtp.get_relation_data()
            except SecretError:
                logger.exception("Cannot read the SMTP password secret yet")
                data = None
            if data is not None:
                return {
                    "server": data.host,
                    "port": data.port,
                    "login": data.user or "",
                    "password": data.password or "",
                    "from_address": str(self.config["smtp-from-address"]).strip(),
                    "encryption": data.transport_security.value,
                    "domain": data.domain or "",
                }
        return {
            "server": str(self.config["smtp-server"]).strip(),
            "port": self.config["smtp-port"],
            "login": str(self.config["smtp-login"]).strip(),
            "password": str(self.config["smtp-password"]),
            "from_address": str(self.config["smtp-from-address"]).strip(),
            "encryption": str(self.config["smtp-encryption"]).strip(),
        }

    def _migrated_version(self) -> str | None:
        peer = self._peer_relation
        if peer is None:
            return None
        return peer.data[self.app].get(MIGRATED_VERSION_KEY) or None

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def _on_install(self, _event: ops.EventBase) -> None:
        self.unit.status = ops.MaintenanceStatus("installing system packages")
        mastodon.install_packages()
        mastodon.ensure_user()
        mastodon.ensure_media_dirs()
        self.unit.status = ops.MaintenanceStatus("installing rbenv")
        mastodon.ensure_rbenv()
        self._reconcile(None)

    def _on_stop(self, _event: ops.EventBase) -> None:
        mastodon.stop_services()

    def _on_media_storage_attached(self, _event: ops.EventBase) -> None:
        from charmlibs import passwd

        if passwd.user_exists(mastodon.USER):
            mastodon.ensure_media_dirs()

    def _on_database_broken(self, _event: ops.EventBase) -> None:
        logger.warning("Database relation removed; stopping Mastodon services")
        mastodon.stop_services()

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def _reconcile(self, _event: ops.EventBase | None) -> None:
        """Converge the machine towards the desired state.

        Each step is idempotent; the method simply returns early when a
        prerequisite (config, secrets, database) is missing and the
        collect-status handler reports why.
        """
        if self._invalid_config_reason():
            return
        hostname = str(self.config["server-hostname"]).strip()
        if not hostname or self._missing_scaling_integrations():
            return
        app_secrets = self._app_secrets()
        if app_secrets is None:
            return
        db = self._database_info()
        if db is None:
            return

        version = str(self.config["version"]).strip()
        release_changed = self._ensure_release(version)

        redis = self._redis_info()
        s3 = self._s3_info()
        env = mastodon.render_env(
            hostname=hostname,
            web_domain=str(self.config["web-domain"]).strip(),
            single_user_mode=bool(self.config["single-user-mode"]),
            default_locale=str(self.config["default-locale"]).strip() or "en",
            app_secrets=app_secrets,
            db=db,
            redis=redis,
            s3=s3,
            smtp=self._smtp_config(),
            es=self._elasticsearch_info(),
            trusted_proxy_ips=str(self.config["trusted-proxy-ips"]).strip(),
            extra_env=str(self.config["extra-env"]),
        )
        env_changed = mastodon.write_env_text(mastodon.env_file_text(env))
        units_changed = mastodon.install_systemd_units(
            web_concurrency=int(self.config["web-concurrency"]),
            max_threads=int(self.config["max-threads"]),
            sidekiq_concurrency=int(self.config["sidekiq-concurrency"]),
        )

        behind_proxy = bool(self.config["behind-proxy"])
        if not behind_proxy:
            # Precedence: relation-issued certificate, then config-provided,
            # then a self-signed fallback (also used while a related
            # certificate provider has not issued ours yet).
            tls = self._relation_tls() or self._decoded_tls()
            mastodon.ensure_tls_material(hostname, *(tls or (None, None)))
        mastodon.configure_nginx(hostname=hostname, behind_proxy=behind_proxy)

        if self.unit.is_leader() and self._migrated_version() != version:
            self.unit.status = ops.MaintenanceStatus("running database migrations")
            mastodon.prepare_database()
            peer = self._peer_relation
            assert peer is not None  # checked via _app_secrets above
            peer.data[self.app][MIGRATED_VERSION_KEY] = version

        if self._migrated_version() == version:
            mastodon.set_local_redis(enabled=redis is None)
            mastodon.enable_services()
            running = mastodon.services_running()
            if release_changed or env_changed or units_changed or not all(running.values()):
                self.unit.status = ops.MaintenanceStatus("restarting Mastodon services")
                mastodon.restart_services()

        self.unit.set_ports(80, 443)
        self.unit.set_workload_version(version.lstrip("v"))
        self._publish_website()

    def _ensure_release(self, version: str) -> bool:
        """Download, build and activate the requested release if needed."""
        if not mastodon.is_release_built(version):
            self.unit.status = ops.MaintenanceStatus(f"downloading Mastodon {version}")
            mastodon.fetch_app(version)
            self.unit.status = ops.MaintenanceStatus(
                f"building Mastodon {version} (this takes several minutes)"
            )
            mastodon.build_app(version)
        changed = mastodon.activate_release(version)
        if changed:
            mastodon.prune_releases()
        return changed

    def _publish_website(self) -> None:
        """Expose our address on the website (http) interface for proxies."""
        for relation in self.model.relations[WEBSITE_RELATION]:
            binding = self.model.get_binding(relation)
            if binding is None or binding.network.bind_address is None:
                continue
            relation.data[self.unit].update(
                {"hostname": str(binding.network.bind_address), "port": "80"}
            )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _on_collect_status(self, event: ops.CollectStatusEvent) -> None:
        reason = self._invalid_config_reason()
        if reason:
            event.add_status(ops.BlockedStatus(reason))
            return
        if not str(self.config["server-hostname"]).strip():
            event.add_status(ops.BlockedStatus("server-hostname must be set"))
            return
        missing = self._missing_scaling_integrations()
        if missing:
            event.add_status(
                ops.BlockedStatus(
                    f"scaling beyond 1 unit requires the {' and '.join(missing)} integration(s)"
                )
            )
            return
        if self.model.get_relation(DATABASE_RELATION) is None:
            event.add_status(ops.BlockedStatus("missing required database integration"))
            return
        if self._app_secrets() is None:
            event.add_status(ops.WaitingStatus("waiting for leader to generate secrets"))
            return
        if self._database_info() is None:
            event.add_status(ops.WaitingStatus("waiting for database credentials"))
            return
        version = str(self.config["version"]).strip()
        if mastodon.installed_version() != version:
            event.add_status(ops.MaintenanceStatus(f"Mastodon {version} not yet installed"))
            return
        if self._migrated_version() != version:
            event.add_status(ops.WaitingStatus("waiting for database migrations"))
            return
        running = mastodon.services_running()
        stopped = sorted(name for name, up in running.items() if not up)
        if stopped:
            event.add_status(ops.MaintenanceStatus(f"services not running: {', '.join(stopped)}"))
            return
        event.add_status(ops.ActiveStatus())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _workload_ready(self) -> bool:
        return mastodon.installed_version() is not None and mastodon.ENV_FILE.exists()

    def _on_create_admin_action(self, event: ops.ActionEvent) -> None:
        if not self._workload_ready():
            event.fail("Mastodon is not ready yet; deploy and integrate it first.")
            return
        username = event.params["username"]
        email = event.params["email"]
        try:
            output = mastodon.tootctl(
                [
                    "accounts",
                    "create",
                    username,
                    "--email",
                    email,
                    "--confirmed",
                    "--approve",
                    "--role",
                    "Owner",
                ]
            )
        except mastodon.WorkloadError as e:
            event.fail(f"creating admin failed: {e}")
            return
        password = ""
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            if line.lower().startswith("new password"):
                _, _, inline = line.partition(":")
                password = inline.strip() or (lines[i + 1] if i + 1 < len(lines) else "")
                break
        event.set_results({"password": password, "output": output})

    def _on_tootctl_action(self, event: ops.ActionEvent) -> None:
        if not self._workload_ready():
            event.fail("Mastodon is not ready yet; deploy and integrate it first.")
            return
        command = shlex.split(event.params["command"])
        if not command:
            event.fail("command must not be empty")
            return
        try:
            output = mastodon.tootctl(command)
        except mastodon.WorkloadError as e:
            event.fail(f"tootctl failed: {e}")
            return
        event.set_results({"output": output})

    def _on_media_cleanup_action(self, event: ops.ActionEvent) -> None:
        if not self._workload_ready():
            event.fail("Mastodon is not ready yet; deploy and integrate it first.")
            return
        days = int(event.params["days"])
        try:
            output = mastodon.tootctl(["media", "remove", "--days", str(days)])
        except mastodon.WorkloadError as e:
            event.fail(f"media cleanup failed: {e}")
            return
        event.set_results({"output": output})


if __name__ == "__main__":  # pragma: nocover
    ops.main(MastodonCharm)
