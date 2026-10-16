"""Keeps Node-RED pages, served on EdgeShell's own origin under /nodered/<node>/
(ADR 0014), from acting as the signed-in EdgeShell user.

A page from a node's Node-RED runs JavaScript on fleet.<domain>, the same origin
as EdgeShell, so the browser would attach the EdgeShell session to anything it
asks for. These rules let EdgeShell tell such requests apart and refuse them:

  - a request whose Referer is a /nodered/ page is refused;
  - a script request (fetch, XHR, form post: anything but a plain navigation)
    without a Referer is refused (a page can drop its Referer, so a missing one
    is not trusted);
  - EdgeShell is never loaded inside a frame (Sec-Fetch-Dest iframe, frame,
    embed or object);
  - WebSockets cannot be told apart by these headers, so the node terminal
    needs a short-lived ticket only EdgeShell's own pages can get (shell.py).

Only browsers send the Fetch Metadata headers (Sec-Fetch-*); API clients with a
bearer token and command-line tools do not, and are not affected.
"""
from urllib.parse import urlparse

NODERED_PREFIX = '/nodered/'
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
    if path.startswith(NODERED_PREFIX):
        return 'made by a Node-RED page'
    if req.headers.get('Sec-Fetch-Site') is None:
        return None                                   # not a browser with Fetch Metadata (API, curl)
    dest = req.headers.get('Sec-Fetch-Dest', '')
    mode = req.headers.get('Sec-Fetch-Mode', '')
    if dest in FRAMED:
        return 'inside a frame'
    if mode == 'websocket':
        return None                                   # the terminal checks its own ticket
    if mode == 'navigate' and req.method in ('GET', 'HEAD'):
        return None                                   # typing an address, a link, a bookmark
    if not path:
        return 'without a Referer'
    return None


def nodered_reason(req, node_segment):
    """For requests to /nodered/<node>/...: refuse those started by another
    node's Node-RED page, or framed, or a script without a Referer."""
    path = referer_path(req)
    if path.startswith(NODERED_PREFIX) and path[len(NODERED_PREFIX):].split('/', 1)[0] != node_segment:
        return "made by another node's Node-RED page"
    if req.headers.get('Sec-Fetch-Site') is None:
        return None
    if req.headers.get('Sec-Fetch-Dest', '') in ('embed', 'object'):
        return 'embedded'
    return None
