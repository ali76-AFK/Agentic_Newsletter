"""Settings. EVERY environment variable the system reads is declared once in _SPEC.

A test checks that README / .env.example only mention variables that exist here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Mapping


class ConfigError(Exception):
    pass


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


# (ENV_NAME, field_name, caster, default)
_SPEC = [
    ("DATABASE_URL", "database_url", str, "sqlite:///./local.db"),
    ("TIMEZONE", "timezone", str, "UTC"),
    ("SECRET_KEY", "secret_key", str, ""),
    ("PUBLIC_BASE_URL", "public_base_url", str, "http://localhost:8100"),
    ("DEFAULT_LANGUAGE", "default_language", str, "en"),
    ("FEED_MODE", "feed_mode", str, "fixture"),
    ("FEED_FRESHNESS_DAYS", "feed_freshness_days", int, 30),
    ("INGEST_MIN_INTERVAL_MINUTES", "ingest_min_interval_minutes", int, 30),
    ("SOURCES_FILE", "sources_file", str, "sources.yaml"),
    ("LLM_PROVIDER", "llm_provider", str, "mock"),
    ("LLM_MODEL", "llm_model", str, ""),
    ("EMAIL_MODE", "email_mode", str, "mailpit"),
    ("MAILPIT_SMTP_HOST", "mailpit_smtp_host", str, "mailpit"),
    ("MAILPIT_SMTP_PORT", "mailpit_smtp_port", int, 1025),
    ("MAILPIT_API_URL", "mailpit_api_url", str, "http://mailpit:8025"),
    ("SMTP_HOST", "smtp_host", str, ""),
    ("SMTP_PORT", "smtp_port", int, 587),
    ("SMTP_SECURITY", "smtp_security", str, "starttls"),
    ("SMTP_USERNAME", "smtp_username", str, ""),
    ("SMTP_PASSWORD", "smtp_password", str, ""),
    ("SMTP_SENDER", "smtp_sender", str, ""),
    ("SMTP_TIMEOUT_SECONDS", "smtp_timeout_seconds", int, 20),
    ("REAL_SEND_ALLOWLIST", "real_send_allowlist", str, ""),
    ("EMAIL_MAX_PER_MINUTE", "email_max_per_minute", int, 0),
    ("MAIL_FROM_DEMO", "mail_from_demo", str, "Agentic Newsletter <newsletter@agentic-nl.local>"),
    ("SCHEDULE_SCAN_SECONDS", "schedule_scan_seconds", int, 30),
    ("SCHEDULE_CATCHUP_MINUTES", "schedule_catchup_minutes", int, 15),
    ("CURATOR_MIN_SCORE", "curator_min_score", float, 0.4),
    ("RELEVANCE_MIN", "relevance_min", float, 0.6),
    ("RELEVANCE_HIGH", "relevance_high", float, 0.8),
    ("LOG_LEVEL", "log_level", str, "INFO"),
]

# Variables consumed by docker-compose / run.sh only (the Python app never reads them).
COMPOSE_ONLY = {
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
    "HOST_PORT_DB", "HOST_PORT_MAILPIT_SMTP", "HOST_PORT_MAILPIT_UI", "HOST_PORT_API", "HOST_PORT_UI",
}

_SECRETS = {"database_url", "secret_key", "smtp_password"}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./local.db"
    timezone: str = "UTC"
    secret_key: str = ""
    public_base_url: str = "http://localhost:8100"
    default_language: str = "en"
    feed_mode: str = "fixture"
    feed_freshness_days: int = 30
    ingest_min_interval_minutes: int = 30
    sources_file: str = "sources.yaml"
    llm_provider: str = "mock"
    llm_model: str = ""
    email_mode: str = "mailpit"
    mailpit_smtp_host: str = "mailpit"
    mailpit_smtp_port: int = 1025
    mailpit_api_url: str = "http://mailpit:8025"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_sender: str = ""
    smtp_timeout_seconds: int = 20
    real_send_allowlist: str = ""
    email_max_per_minute: int = 0
    mail_from_demo: str = "Agentic Newsletter <newsletter@agentic-nl.local>"
    schedule_scan_seconds: int = 30
    schedule_catchup_minutes: int = 15
    curator_min_score: float = 0.4
    relevance_min: float = 0.6
    relevance_high: float = 0.8
    log_level: str = "INFO"

    @staticmethod
    def env_names() -> set[str]:
        return {name for name, *_ in _SPEC}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        values = {}
        for name, field, caster, default in _SPEC:
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                values[field] = default
                continue
            try:
                values[field] = caster(raw.strip()) if caster is not str else raw.strip()
            except ValueError:
                raise ConfigError(f"{name}={raw!r} is not a valid {caster.__name__}") from None
        s = cls(**values)
        s.validate()
        return s

    def validate(self) -> None:
        def one_of(name, value, allowed):
            if value not in allowed:
                raise ConfigError(f"{name}={value!r} is invalid. Allowed values: {', '.join(allowed)}")

        one_of("EMAIL_MODE", self.email_mode, ("mailpit", "smtp"))
        one_of("SMTP_SECURITY", self.smtp_security, ("starttls", "ssl", "none"))
        one_of("FEED_MODE", self.feed_mode, ("fixture", "live"))
        one_of("DEFAULT_LANGUAGE", self.default_language, ("en", "de"))
        if self.llm_provider != "mock":
            raise ConfigError(
                f"LLM_PROVIDER={self.llm_provider!r} is not available in this build "
                "(only 'mock' is implemented; groq/ollama are planned for Phase 2)")
        if self.schedule_catchup_minutes < 0 or self.schedule_scan_seconds < 1:
            raise ConfigError("SCHEDULE_CATCHUP_MINUTES must be >= 0 and SCHEDULE_SCAN_SECONDS >= 1")

    def with_(self, **kw) -> "Settings":
        return replace(self, **kw)

    def redacted(self) -> dict:
        out = {}
        for _, field, _, _ in _SPEC:
            v = getattr(self, field)
            if field in _SECRETS:
                out[field] = f"set (length {len(v)})" if v else "not set"
            else:
                out[field] = v
        return out
