# 0003. CSRF on, fail hard on missing production secrets

## Status
Accepted.

## Context
The previous build disabled CSRF and fell back to hardcoded SECRET_KEY and
CREDENTIAL_KEY. For a browser-based multi-user tool that stores fleet SSH
credentials, both are serious risks: state-changing requests were forgeable, and
credentials could be encrypted with a publicly known key.

## Decision
Enable Flask-WTF CSRF globally. Expose the token via a meta tag and a fetch shim
so AJAX is covered, and a hidden field in every form. In production, refuse to
start unless SECRET_KEY and CREDENTIAL_KEY are set in the environment. Keep a
dev-only fallback so local development stays frictionless.

## Consequences
- Forged cross-site state changes are blocked.
- A misconfigured production deploy fails loudly instead of running insecure.
- JSON API and health endpoints are CSRF-exempt but still auth-protected.
