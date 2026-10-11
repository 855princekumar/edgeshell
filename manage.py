#!/usr/bin/env python
"""EdgeShell management CLI.

Usage:
  python manage.py seed
  python manage.py create-admin --username alice --email a@x.com --password ...
  python manage.py db-export snapshot.json
  python manage.py db-import snapshot.json [--wipe]
  python manage.py reencrypt --old <OLD_CREDENTIAL_KEY>
  python manage.py migrate-sqlite /path/to/edgeshell.db   (into DATABASE_URL)
  python manage.py record-event start|stop

Migration (SQLite -> Postgres/MariaDB), export/import mode:
  1) On the old DB:   python manage.py db-export snapshot.json
  2) Point DATABASE_URL at the new DB (Postgres/MariaDB)
  3) On the new DB:   python manage.py db-import snapshot.json --wipe
Keep the CREDENTIAL_KEY with the snapshot; node passwords travel as ciphertext.
"""
import os
import sys
import argparse

os.environ.setdefault('FLASK_ENV', os.getenv('FLASK_ENV', 'development'))


def get_app():
    from app import create_app
    return create_app(os.getenv('FLASK_ENV', 'development'))


def cmd_seed(args):
    app = get_app()
    print('Seed complete (admin + default settings ensured on startup).')


def cmd_create_admin(args):
    app = get_app()
    with app.app_context():
        from app import db
        from app.models.user import User
        if User.query.filter_by(username=args.username).first():
            print('User already exists.'); return
        u = User(username=args.username, email=args.email, role='administrator',
                 is_active=True, must_change_password=False)
        u.set_password(args.password)
        db.session.add(u); db.session.commit()
        print(f'Administrator {args.username} created.')


def cmd_db_export(args):
    app = get_app()
    with app.app_context():
        from app.services.dbio_service import export_snapshot
        counts = export_snapshot(args.path)
        print('Exported:', counts)
        print('Wrote', args.path)


def cmd_db_import(args):
    app = get_app()
    with app.app_context():
        from app import db
        db.create_all()
        from app.services.dbio_service import import_snapshot
        counts = import_snapshot(args.path, wipe=args.wipe)
        print('Imported:', counts)


def cmd_reencrypt(args):
    app = get_app()
    with app.app_context():
        from app import db
        from app.models.node import Node
        from app.services.crypto import decrypt_value, encrypt_value
        n = 0
        for node in Node.query.all():
            if node._password:
                plain = decrypt_value(node._password, key_str=args.old)
                if plain:
                    node._password = encrypt_value(plain)  # new key from config
                    n += 1
        db.session.commit()
        print(f'Re-encrypted {n} node credential(s) under the current key.')


def cmd_migrate_sqlite(args):
    """Copy an old SQLite database into the current DATABASE_URL (MariaDB).

    Export runs in a child process pointed at the SQLite file (config binds the
    DB URL at import time), then rows are bulk-inserted here. Node passwords
    move as ciphertext, so CREDENTIAL_KEY must stay the same.
    """
    import subprocess, tempfile, json
    src = os.path.abspath(args.path)
    if not os.path.isfile(src):
        print('No such SQLite file:', src); sys.exit(1)
    snap = os.path.join(tempfile.mkdtemp(prefix='es-migrate-'), 'snapshot.json')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + src)
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.run([sys.executable, os.path.join(here, 'manage.py'), 'db-export', snap],
                   env=env, check=True)
    app = get_app()
    with app.app_context():
        from app import db
        from app.services.dbio_service import import_snapshot
        db.create_all()
        counts = import_snapshot(snap, wipe=True, bulk=True)
    os.remove(snap)
    print('MIGRATED', json.dumps(counts))


def cmd_record_event(args):
    app = get_app()
    with app.app_context():
        from app.services import service_events as se
        ev = se.record_start() if args.kind == 'start' else se.record_stop()
        print(f'{ev.event}: {ev.reason} - {ev.detail}')


def main():
    p = argparse.ArgumentParser(description='EdgeShell management CLI')
    sub = p.add_subparsers(dest='cmd', required=True)

    sub.add_parser('seed').set_defaults(func=cmd_seed)

    ca = sub.add_parser('create-admin')
    ca.add_argument('--username', required=True)
    ca.add_argument('--email', required=True)
    ca.add_argument('--password', required=True)
    ca.set_defaults(func=cmd_create_admin)

    ex = sub.add_parser('db-export'); ex.add_argument('path')
    ex.set_defaults(func=cmd_db_export)

    im = sub.add_parser('db-import'); im.add_argument('path')
    im.add_argument('--wipe', action='store_true')
    im.set_defaults(func=cmd_db_import)

    re_ = sub.add_parser('reencrypt'); re_.add_argument('--old', required=True)
    re_.set_defaults(func=cmd_reencrypt)

    ms = sub.add_parser('migrate-sqlite'); ms.add_argument('path')
    ms.set_defaults(func=cmd_migrate_sqlite)

    re_ev = sub.add_parser('record-event'); re_ev.add_argument('kind', choices=['start', 'stop'])
    re_ev.set_defaults(func=cmd_record_event)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
