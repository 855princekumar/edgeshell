# 0004. Central role-based access control

## Status
Accepted.

## Context
Role checks were duplicated across blueprints with slightly different behavior,
which is easy to get wrong as routes multiply.

## Decision
One module, `app/services/rbac.py`, defines a role ranking and
`role_required(minimum)`, plus `operator_required` and `admin_required`. Denials
return JSON 403 for API callers and a redirect for browser navigation.

## Consequences
- Consistent enforcement across all routes.
- New routes pick the right gate with one decorator.
- Sessions remain isolated per user via Flask-Login.
