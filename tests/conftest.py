"""With TEST_DATABASE_URL (e.g. MariaDB), start every test on empty tables,
matching the per-test fresh in-memory SQLite the suite was written for."""
import os
import pytest


@pytest.fixture(autouse=True)
def _fresh_persistent_db():
    if os.environ.get('TEST_DATABASE_URL'):
        from app import create_app, db
        app = create_app('testing')
        with app.app_context():
            db.drop_all()
            db.session.remove()
    yield
