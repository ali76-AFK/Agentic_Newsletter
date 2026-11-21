import os

from alembic import context
from sqlalchemy import create_engine

url = os.environ["DATABASE_URL"]
if url.startswith("postgresql://"):
    url = "postgresql+psycopg://" + url[len("postgresql://"):]

with create_engine(url).connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
