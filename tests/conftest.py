"""Test harness written for this repository.

Production runs this suite against Postgres — a fresh database per test run, built
from the Alembic migrations — because the full schema cannot be created on SQLite.
The engine's own tables can, so here the default is in-memory SQLite and the suite
needs no services. Set TEST_DATABASE_URL to a Postgres database to also run the
advisory-lock tests (they are skipped otherwise):

    TEST_DATABASE_URL=postgresql://localhost/engine_test pytest

Tables are dropped and recreated around every test, so tests are order-independent.
"""
import os

import pytest

from app import create_app, db as _db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
IS_POSTGRES = bool(TEST_DATABASE_URL) and TEST_DATABASE_URL.startswith("postgres")

requires_postgres = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="advisory locks are a Postgres feature — set TEST_DATABASE_URL to run",
)


@pytest.fixture(scope="session")
def app():
    if TEST_DATABASE_URL:
        # Same guard production uses: never build/drop tables on a non-test database.
        name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
        assert name.endswith("_test"), (
            f"refusing to run against {name!r}: the database name must end in _test")
    app = create_app(TEST_DATABASE_URL or "sqlite:///:memory:")
    with app.app_context():
        yield app


@pytest.fixture()
def session(app):
    _db.drop_all()
    _db.create_all()
    yield _db.session
    _db.session.rollback()
    _db.session.remove()
    _db.drop_all()
