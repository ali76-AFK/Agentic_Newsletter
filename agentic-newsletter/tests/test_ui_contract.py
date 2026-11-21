"""The web UI (ui/) must only call API routes that exist, must not reach into the database, and must stay local-only."""
import ast
import os
import re
import unittest

from tests import test_config
from tests.helpers import ROOT

read = test_config.read

UI = os.path.join(ROOT, "ui")


def ui_files():
    return sorted(f for f in os.listdir(UI) if f.endswith(".py"))


def normalise(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


class UiContract(unittest.TestCase):
    def routes(self):
        return {(m, normalise(p)) for m, p in test_config.Docs().api_routes()}

    def test_every_api_call_in_the_ui_exists_in_the_api(self):
        routes = self.routes()
        seen = set()
        for name in ui_files():
            src = read("ui", name)
            for method, path in re.findall(r'api\.(get|post|put|patch|delete)\b[,(]\s*f?"(/[^"]+)"', src):
                seen.add((method.upper(), normalise(path)))
            for path in re.findall(r'(?:load|c\.load)\(\s*f?"(/[^"]+)"', src):
                seen.add(("GET", normalise(path)))
        self.assertTrue(seen, "no API calls found in ui/ - did the pattern break?")
        self.assertFalse(seen - routes, f"the UI calls routes the API does not have: {sorted(seen - routes)}")

    def test_every_api_path_literal_in_the_ui_is_a_known_path(self):
        known = {p for _, p in self.routes()}
        for name in ui_files():
            for path in re.findall(r'f?"(/api/[^"]*)"', read("ui", name)):
                self.assertIn(normalise(path), known, f"{name}: unknown API path {path}")

    def test_ui_never_touches_the_database_or_app_package(self):
        banned = {"psycopg", "sqlalchemy", "sqlite3", "alembic", "app"}
        for name in ui_files():
            for node in ast.walk(ast.parse(read("ui", name))):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    mods = [node.module.split(".")[0]]
                self.assertFalse(set(mods) & banned, f"ui/{name} imports {set(mods) & banned}")

    def test_navigation_lists_only_existing_page_files(self):
        pages = re.findall(r'st\.Page\("([^"]+)"', read("ui", "app.py"))
        self.assertGreaterEqual(len(pages), 5)
        for p in pages:
            self.assertTrue(os.path.isfile(os.path.join(UI, p)), f"app.py lists missing page {p}")

    def test_compose_ui_is_local_only_and_has_no_secrets(self):
        compose = read("docker-compose.yml")
        self.assertIn('"127.0.0.1:${HOST_PORT_UI:-8502}:8501"', compose)
        ui_block = compose.split("  ui:\n", 1)[1].split("\n  ui-tests:", 1)[0]
        self.assertNotIn("env_file", ui_block, "the UI container must not receive .env (it holds secrets)")
        self.assertNotIn("DATABASE_URL", ui_block)
        self.assertNotIn("POSTGRES_PASSWORD", ui_block)

    def test_ui_requirements_are_pinned(self):
        for line in read("ui", "requirements.txt").splitlines():
            if line.strip():
                self.assertRegex(line, r"^[A-Za-z0-9_.\[\]-]+==[0-9][^=]*$", line)


if __name__ == "__main__":
    unittest.main()
