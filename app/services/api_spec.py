"""OpenAPI 3.1 document for /api/v1, built from the route declarations in
app/routes/api.py (so it cannot drift from the code)."""

SCOPE_TEXT = {'read': 'monitor or higher', 'report': 'monitor or higher, hardware fault reports only', 'write': 'operator or higher', 'control': 'operator or higher',
              'admin': 'administrator'}
ERRORS = {
    '400': 'Validation error: `{"error": "validation_error", "details": {field: problem}}`',
    '401': 'Not signed in, or the token is wrong, expired or revoked',
    '403': 'Signed in, but the role or credential does not allow this',
    '404': 'Not found',
    '429': 'Rate limit reached; see the Retry-After header',
}


def build(routes, version):
    paths = {}
    for method, path, meta in routes:
        op = {'summary': meta['summary'], 'tags': [meta['tag']], 'operationId': _op_id(method, path)}
        desc = meta.get('description') or ''
        if meta.get('scope'):
            desc += (' ' if desc else '') + f'Needs the `{meta["scope"]}` permission ({SCOPE_TEXT[meta["scope"]]}).'
        if meta.get('rate'):
            desc += f' Rate limit: {meta["rate"]}.'
        op['description'] = desc
        params = [p.openapi() for p in meta.get('params') or ()]
        for name in _path_params(path):
            params.append({'name': name, 'in': 'path', 'required': True,
                           'schema': {'type': 'string', 'pattern': '^[0-9a-f]{6,32}$'} if meta.get('string_id')
                           else {'type': 'integer', 'minimum': 1}})
        if params:
            op['parameters'] = params
        if meta.get('body') is not None:
            op['requestBody'] = {'required': True,
                                 'content': {'application/json': {'schema': meta['body']}}}
        op['responses'] = {'200': {'description': 'OK (201 when created, 202 when a job started)'}}
        for code, text in ERRORS.items():
            if code in ('401', '403') and not meta.get('scope'):
                continue
            op['responses'][code] = {'description': text}
        if meta.get('scope'):
            op['security'] = [{'bearer': []}]
        else:
            op['security'] = []
        paths.setdefault(path, {})[method.lower()] = op
    return {
        'openapi': '3.1.0',
        'info': {'title': 'EdgeShell API', 'version': version,
                 'description': 'Read fleet data points and events, manage nodes, run jobs. '
                                'Sign in with POST /auth/login and /auth/verify (password + 2FA) or use a '
                                'service key; send `Authorization: Bearer <token>`. Full guide: docs/API.md.'},
        'servers': [{'url': '/api/v1'}],
        'components': {'securitySchemes': {'bearer': {'type': 'http', 'scheme': 'bearer',
                                                      'description': 'An API session token (est_...) or a '
                                                                     'service key (esk_...)'}}},
        'paths': paths,
    }


def _path_params(path):
    import re
    return re.findall(r'\{(\w+)\}', path)


def _op_id(method, path):
    import re
    return method.lower() + ''.join(w.capitalize() for w in re.split(r'[^A-Za-z0-9]+', path) if w)
