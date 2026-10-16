"""Keeps node apps served on EdgeShell's own origin (ADR 0014) from acting as the
signed-in EdgeShell user: Node-RED at /nodered/<node>/ and the remote desktop
(OpenRemoteGUI) at /remoteGUI/<node>/.

A page from a node's app runs JavaScript on fleet.<domain>, the same origin as
EdgeShell, so the browser would attach the EdgeShell session to anything it
asks for. These rules let EdgeShell tell such requests apart and refuse them:

  - a request whose Referer is a node app page is refused;
  - a script request (fetch, XHR, form post: anything but a plain navigation)
    without a Referer is refused (a page can drop its Referer, so a missing one
    is not trusted);
  - EdgeShell is never loaded inside a frame (Sec-Fetch-Dest iframe, frame,
    embed or object);
  - WebSockets cannot be told apart by these headers, so the node terminal and
    the remote desktop need short-lived tickets only EdgeShell's own pages can
    get.

Only browsers send the Fetch Metadata headers (Sec-Fetch-*); API clients with a
bearer token and command-line tools are not affected.
"""
from urllib.parse import urlparse

NODERED_PREFIX = '/nodered/'
REMOTEGUI_PREFIX = '/remoteGUI/'
APP_PREFIXES = (NODERED_PREFIX, REMOTEGUI_PREFIX)
FRAMED = ('iframe', 'frame', 'embed', 'object')


def referer_path(req):
    ref = req.headers.get('Referer', '')
    if not ref:
        return ''
    try:
        return urlparse(ref).path or '/'
    except ValueError:
        return '/'


def blocked_reason(req):
    """Why this browser request must not use the EdgeShell session, or None."""
    path = referer_path(req)
    if path.startswith(APP_PREFIXES):
        return 'made by a node app page (Node-RED or remote desktop)'
    if req.headers.get('Sec-Fetch-Site') is None:
        return None                                   # not a browser with Fetch Metadata (API, curl)
    dest = req.headers.get('Sec-Fetch-Dest', '')
    mode = req.headers.get('Sec-Fetch-Mode', '')
    if dest in FRAMED:
        return 'inside a frame'
    if mode == 'websocket':
        return None                                   # tickets protect the WebSockets
    if mode == 'navigate' and req.method in ('GET', 'HEAD'):
        return None                                   # typing an address, a link, a bookmark
    if not path:
        return 'without a Referer'
    return None


def app_reason(req, prefix, node_segment):
    """For requests to <prefix><node>/...: refuse those started by another node app
    page (another node, or the other app), and embedding."""
    path = referer_path(req)
    if path.startswith(APP_PREFIXES):
        if not path.startswith(prefix) or path[len(prefix):].split('/', 1)[0] != node_segment:
            return 'made by another node app page'
    if req.headers.get('Sec-Fetch-Site') is None:
        return None
    if req.headers.get('Sec-Fetch-Dest', '') in ('embed', 'object'):
        return 'embedded'
    return None


def nodered_reason(req, node_segment):
    return app_reason(req, NODERED_PREFIX, node_segment)
