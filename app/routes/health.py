"""Unauthenticated health endpoint for the watchdog and load balancers."""
import os
from flask import Blueprint, jsonify
from sqlalchemy import text
from app import db

health_bp = Blueprint('health', __name__)


@health_bp.route('/healthz')
def healthz():
    checks = {'app': 'ok'}
    code = 200
    try:
        db.session.execute(text('SELECT 1'))
        checks['db'] = 'ok'
    except Exception as e:
        checks['db'] = f'error: {e}'
        code = 503
    try:
        st = os.statvfs('/')
        free_pct = round(st.f_bavail / st.f_blocks * 100, 1) if st.f_blocks else 0
        checks['disk_free_pct'] = free_pct
        if free_pct < 5:
            # Reported, but not a 503: a restart cannot free disk, so the
            # watchdog must not restart-loop on it.
            checks['disk'] = 'low'
    except Exception:
        checks['disk_free_pct'] = None
    status = 'down' if code != 200 else ('degraded' if checks.get('disk') == 'low' else 'ok')
    return jsonify({'status': status, 'checks': checks}), code
