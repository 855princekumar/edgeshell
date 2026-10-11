# 0001. Flask app-factory pattern

## Status
Accepted.

## Context
The app must run in several modes (local dev, production via Gunicorn, and
tests) and be importable by both `run.py` and `wsgi.py` without side effects at
import time.

## Decision
Use `create_app(config_name)` that builds and configures a fresh app per call.
Extensions (SQLAlchemy, Login, Bcrypt, Limiter, CSRF) are module-level
singletons initialized inside the factory.

## Consequences
- Clean separation of config environments.
- Tests build an isolated in-memory app.
- Blueprints register in one place; new features are additive.
