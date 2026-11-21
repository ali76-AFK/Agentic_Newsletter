"""Documentation <-> code consistency (defects #5 and #12 of the previous build)."""
import ast
import os
import re
import unittest

from app.config import COMPOSE_ONLY, ConfigError, Settings
from tests.helpers import ROOT


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


class Settings_(unittest.TestCase):
    def test_every_env_var_is_a_settings_field_and_is_read(self):
        names = Settings.env_names()
        self.assertTrue(names)
        env = {n: "" for n in names}
        Settings.from_env(env)   # does not crash with empty values -> defaults
        probes = {"FEED_FRESHNESS_DAYS": ("12", "feed_freshness_days", 12), "RELEVANCE_MIN": ("0.7", "relevance_min", 0.7),
                  "SCHEDULE_CATCHUP_MINUTES": ("9", "schedule_catchup_minutes", 9), "EMAIL_MODE": ("smtp", "email_mode", "smtp"),
                  "SMTP_PORT": ("2525", "smtp_port", 2525), "REAL_SEND_ALLOWLIST": ("a@b.test", "real_send_allowlist", "a@b.test")}
        for name, (raw, field, want) in probes.items():
            self.assertEqual(getattr(Settings.from_env({name: raw}), field), want, name)

    def test_every_declared_var_actually_changes_a_field(self):
        from app.config import _SPEC
        for name, field, caster, default in _SPEC:
            probe = {"str": "x", "int": "7", "float": "0.5"}[caster.__name__]
            env = {name: probe}
            if name in ("EMAIL_MODE", "SMTP_SECURITY", "FEED_MODE", "DEFAULT_LANGUAGE", "LLM_PROVIDER"):
                continue   # enumerations are covered separately
            self.assertNotEqual(getattr(Settings.from_env(env), field), default, f"{name} is declared but not read")

    def test_readme_and_env_example_mention_only_real_variables(self):
        known = Settings.env_names() | COMPOSE_ONLY
        env_keys = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", read(".env.example"), re.M))
        self.assertFalse(env_keys - known, f".env.example has unknown vars: {env_keys - known}")
        readme_rows = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", read("README.md"), re.M))
        self.assertTrue(readme_rows)
        self.assertFalse(readme_rows - known, f"README documents vars that do not exist: {readme_rows - known}")
        self.assertFalse(Settings.env_names() - readme_rows, f"settings missing from README: {Settings.env_names() - readme_rows}")
        self.assertFalse(Settings.env_names() - env_keys, f"settings missing from .env.example: {Settings.env_names() - env_keys}")

    def test_old_value_real_is_rejected_with_allowed_values(self):
        with self.assertRaisesRegex(ConfigError, "mailpit, smtp"):
            Settings.from_env({"EMAIL_MODE": "real"})

    def test_bad_number_message_names_variable(self):
        with self.assertRaisesRegex(ConfigError, "SMTP_PORT"):
            Settings.from_env({"SMTP_PORT": "abc"})

    def test_non_mock_llm_refused_in_this_build(self):
        with self.assertRaisesRegex(ConfigError, "not available"):
            Settings.from_env({"LLM_PROVIDER": "groq"})

    def test_secrets_are_redacted(self):
        r = Settings(smtp_password="hunter2", secret_key="abc", database_url="postgresql://u:pw@h/db").redacted()
        blob = str(r)
        for secret in ("hunter2", "pw@h", "abc"):
            self.assertNotIn(secret, blob)
        self.assertEqual(r["smtp_password"], "set (length 7)")

    def test_env_example_has_no_secrets_filled_in(self):
        for line in read(".env.example").splitlines():
            if re.match(r"^(POSTGRES_PASSWORD|SECRET_KEY|SMTP_PASSWORD)=", line):
                self.assertTrue(line.endswith("="), line)


class Docs(unittest.TestCase):
    def api_routes(self):
        tree = ast.parse(read("app", "api.py"))
        routes = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for d in node.decorator_list:
                    if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "post", "put", "patch", "delete"):
                        routes.add((d.func.attr.upper(), d.args[0].value))
        return routes

    def test_every_endpoint_in_readme_exists_in_api(self):
        documented = set(re.findall(r"^\| `(GET|POST|PUT|PATCH|DELETE) (/[^`]*)` \|", read("README.md"), re.M))
        self.assertTrue(documented)
        self.assertFalse(documented - self.api_routes(), f"README endpoints missing in api.py: {documented - self.api_routes()}")

    def test_every_endpoint_is_documented(self):
        documented = set(re.findall(r"^\| `(GET|POST|PUT|PATCH|DELETE) (/[^`]*)` \|", read("README.md"), re.M))
        self.assertFalse(self.api_routes() - documented, f"undocumented endpoints: {self.api_routes() - documented}")

    def test_openapi_matches_when_fastapi_is_installed(self):
        try:
            from app.api import app
        except ImportError:
            self.skipTest("fastapi not installed here (runs inside Docker)")
        spec = app.openapi()["paths"]
        for method, path in self.api_routes():
            self.assertIn(method.lower(), spec.get(path, {}), f"{method} {path} missing from OpenAPI")

    def test_run_sh_subcommands_documented(self):
        sh = read("run.sh")
        for cmd in re.findall(r"^\s+([a-z][a-z-]+)\)\s", sh, re.M):
            if cmd in ("help",):
                continue
            self.assertIn(f"./run.sh {cmd}", read("README.md"), f"run.sh {cmd} not in README")

    def test_compose_has_no_version_key_and_uses_agentic_prefix(self):
        c = read("docker-compose.yml")
        self.assertFalse(re.search(r"^version\s*:", c, re.M))
        self.assertNotIn("newsletter_db", c)
        self.assertIn("agentic_nl_", c)
        self.assertNotIn(":latest", c.replace("axllent/mailpit:latest", ""))

    def test_no_secrets_committed(self):
        for name in (".env.example", "docker-compose.yml", "run.sh", "README.md"):
            self.assertFalse(re.search(r"gsk_[A-Za-z0-9]{10,}", read(name)), name)


if __name__ == "__main__":
    unittest.main()
