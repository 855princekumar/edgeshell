## What and why

<!-- What this changes and the problem it solves. Link the issue: Fixes #123 -->

## How it was tested

- [ ] `python -m pytest -q` passes (CI also needs at least 170 tests, none skipped, coverage of at least 74 percent)
- [ ] `python tools/ui_test.py --nodes 1500 --ssh 127.0.0.1:2299` passes for UI changes (CI needs at least 375 checks)
- [ ] `bash sandbox/run.sh` passes for changes to fleet operations, sessions, the API, the image or the installer
- [ ] `python tools/lake_e2e.py` passes for changes to the data lake, retention or the collector's purges
- [ ] Tested on real hardware or a real host, if relevant (describe below)

## Security checklist

- [ ] New page routes have a role decorator and CSRF protection; new API routes use `endpoint(...)` with a scope, parameter limits and a JSON schema (`additionalProperties: false`)
- [ ] No SQL built from request input: ORM or bound parameters only
- [ ] Nothing secret in responses, logs, the activity log, tests or screenshots (no passwords, tokens, keys, real IPs or accounts)
- [ ] New SSH paths go through `ssh_service.connect` (login policy, closed on failure)
- [ ] A new `# nosec` marker has a `# Bandit ... reviewed:` line explaining why
- [ ] Anything that deletes data is safe for the data lake (`lake.purge`, never a plain DELETE on history)

## Checklist

- [ ] Side effects (SSH, Ansible, VPN) live in `app/services`, not in routes
- [ ] New features come with tests: a unit test, and `tools/ui_test.py` for pages, so the counts only go up
- [ ] Docs (`docs/API.md` for the API, `docs/TROUBLESHOOTING.md` for a new issue and fix) and `CHANGELOG.md` updated
- [ ] No em dashes anywhere (CI checks)
