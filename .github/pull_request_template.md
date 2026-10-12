## What and why

<!-- What this changes and the problem it solves. Link the issue: Fixes #123 -->

## How it was tested

- [ ] `python -m pytest -q` passes
- [ ] `bash sandbox/run.sh` passes (for changes to fleet operations, sessions, the image or the installer)
- [ ] Tested on real hardware or a real host, if relevant (describe below)

## Checklist

- [ ] New state-changing routes have a role decorator and CSRF protection
- [ ] Side effects (SSH, Ansible, VPN) live in `app/services`, not in routes
- [ ] No secrets, real IPs or credentials in code, tests or screenshots
- [ ] Docs and `CHANGELOG.md` updated when behavior changes
