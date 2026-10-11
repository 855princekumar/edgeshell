# Contributing

Thanks for your interest in EdgeShell.

## Development setup

```bash
python run.py          # bootstraps venv, deps, .env, and runs dev server
```

Or manually:

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
FLASK_ENV=development ./venv/bin/python -m flask --app wsgi run
```

## Conventions

- No em dashes in code, comments, UI text, or docs.
- Keep side effects (SSH, Ansible, VPN) in `app/services`, not in routes.
- Add a role decorator to every new state-changing route.
- New forms need a hidden `csrf_token`; new AJAX is covered by the fetch shim.
- Prefer surgical changes; keep diffs reviewable.

## Tests

```bash
FLASK_ENV=testing ./venv/bin/python -m pytest -q
```

The testing config uses an in-memory database and disables CSRF.

## Pull requests

- Describe the change and its motivation.
- Note anything that needs hardware validation (SSH, fleet, WireGuard).
- Update docs and CHANGELOG when behavior changes.
