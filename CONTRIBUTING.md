# Contributing

Thanks for your interest in EdgeShell. Please read the
[Code of Conduct](CODE_OF_CONDUCT.md) first. Security problems go through
[SECURITY.md](SECURITY.md), never a public issue.

## Development setup

```bash
python run.py --dev        # bootstraps venv, deps, .env, and runs the dev server
```

Or manually:

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
FLASK_ENV=development ./venv/bin/python -m flask --app wsgi run
```

## Tests

```bash
FLASK_ENV=testing python -m pytest -q      # unit tests (SQLite, in memory)
bash sandbox/run.sh                        # end to end against an emulated fleet (needs Docker)
```

The testing config uses an in-memory database and disables CSRF. To run the
unit tests against MariaDB, set
`TEST_DATABASE_URL=mysql+mysqlconnector://user:pass@host/edgeshell_test`.

The sandbox builds the image, spawns a 6-node
[Pi Fleet Emulator](https://github.com/855princekumar/pi-fleet-emulator) fleet
(32/64-bit, Buster to Bookworm) and runs the full end-to-end suite. Run it for
any change to fleet operations, sessions, the Docker image or the installer.

## Conventions

- No em dashes in code, comments, UI text, or docs.
- Keep side effects (SSH, Ansible, VPN) in `app/services`, not in routes.
- Add a role decorator to every new state-changing route.
- New forms need a hidden `csrf_token`; new AJAX is covered by the fetch shim.
- Never commit secrets, real IP addresses or credentials, including in tests and screenshots.
- Prefer surgical changes; keep diffs reviewable.

## Pull requests

`main` is protected. Every change goes through a pull request, and eight CI
checks must pass before it can merge: lint, secret scan, unit tests on Python
3.11 to 3.13, the image build and audit, and the sandbox on amd64 and arm64.

1. Fork, create a branch, make the change.
2. Fill in the pull request template: what, why, and how you tested it.
3. Note anything that needs hardware validation (real Pis, WireGuard, EC2).
4. Update the docs and `CHANGELOG.md` when behavior changes.

Releases are cut by pushing a `vX.Y.Z` tag that matches `VERSION`; CI then
publishes the multi-arch image and the GitHub release.
