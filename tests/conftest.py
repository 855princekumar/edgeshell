"""Every test starts on empty tables: a temporary SQLite file by default, or
the database in TEST_DATABASE_URL (e.g. MariaDB)."""
import pytest


@pytest.fixture(autouse=True)
def _fresh_db():
    from app import create_app, db
    app = create_app('testing')
    with app.app_context():
        db.drop_all()
        db.session.remove()
        db.engine.dispose()
    yield
