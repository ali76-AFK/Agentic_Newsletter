"""initial schema

Revision ID: 0001
Revises:
"""
import re
from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0001_initial.sql"


def upgrade():
    text = re.sub(r"--[^\n]*", "", SQL.read_text(encoding="utf-8"))
    bind = op.get_bind()
    for stmt in (s.strip() for s in text.split(";")):
        if stmt:
            bind.exec_driver_sql(stmt)   # raw SQL: no bind-parameter parsing


def downgrade():
    raise RuntimeError("downgrading the initial schema would delete all data; use ./run.sh reset instead")
