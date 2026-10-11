# 0007. Manifest-based update and rollback

## Status
Accepted.

## Context
Deployments need to be reliable and reversible, matching the pattern already
used for OpenRemoteGUI.

## Decision
`deploy/update.sh` records each release as `releases/<timestamp>/manifest.json`
(version, git sha, timestamp, checksums) and moves a `current` pointer.
`deploy/rollback.sh` reverts to the previous release and restarts the service.
A JSON schema validates the manifest shape.

## Consequences
- Updates are traceable and reversible.
- Rollback is a single command.
- The manifest is a simple, inspectable artifact.
