"""
EdgeShell - WSGI entry point for production (Gunicorn / uWSGI).

Usage with Gunicorn:
  gunicorn -w 4 -b 0.0.0.0:5000 wsgi:app

Usage with uWSGI:
  uwsgi --http 0.0.0.0:5000 --module wsgi:app --processes 4
"""

import os
from app import create_app

app = create_app(os.getenv('FLASK_ENV', 'production'))
