"""An existing database gets every column a newer EdgeShell needs: a column
the upgrade forgets breaks every query on its table (1.15.0 shipped three
heartbeat columns without the upgrade step; 1.15.2 fixed it and added the
safety net these tests hold in place)."""
from sqlalchemy import inspect, text

from app import create_app, db, _ensure_schema


def _drop(columns_by_table):
    """Drop columns the way an older database lacks them (skips what SQLite cannot drop)."""
    dropped = []
    for table, cols in columns_by_table.items():
        for col in cols:
            try:
                with db.engine.begin() as c:
                    c.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{col}"'))
                dropped.append((table, col))
            except Exception:                    # indexed, unique or keyed columns: SQLite refuses
                pass
    return dropped


def test_a_database_from_before_the_heartbeat_routine_still_works():
    app = create_app('testing')
    with app.app_context():
        db.drop_all()
        db.create_all()
        assert len(_drop({'nodes': ['hb_way', 'hb_routine_at', 'hb_routine_note']})) == 3
        db.session.remove()
        _ensure_schema()
        from app.models.node import Node
        db.session.add(Node(name='pi-old', ip_address='10.0.0.9', username='pi', hb_way='tcp:1883'))
        db.session.commit()
        assert Node.query.one().to_dict()['heartbeat']['way'] == 'tcp:1883'


def test_every_nullable_model_column_is_added_back_to_an_older_database():
    app = create_app('testing')
    with app.app_context():
        db.drop_all()
        db.create_all()
        wanted = {t.name: [c.name for c in t.columns if c.nullable and not c.primary_key]
                  for t in db.metadata.sorted_tables}
        dropped = _drop(wanted)
        assert len(dropped) > 50                  # a real test: most columns of every table
        db.session.remove()
        _ensure_schema()
        _ensure_schema()                          # idempotent
        insp = inspect(db.engine)
        missing = [(t, c) for t, c in dropped if c not in {x['name'] for x in insp.get_columns(t)}]
        assert missing == []
        for t in db.metadata.sorted_tables:       # every model reads again
            db.session.execute(t.select().limit(1)).all()
