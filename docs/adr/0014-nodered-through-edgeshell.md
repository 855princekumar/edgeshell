# 0014: Node-RED through EdgeShell, on EdgeShell's own address

- **Status:** Accepted (1.11.0)
- **Date:** 2026-10-16

## Context

The Node-RED button opened `http://<node ip>:1880` in a new tab. That showed the
node's private address, worked only for people on the VPN, and bypassed
EdgeShell's sign-in and roles: anyone who could reach the address could edit
the flows. The owner wants every node's Node-RED at
`https://fleet.<domain>/nodered/<node name>/`, with the address hidden.

A separate subdomain (for example `nodered.fleet.<domain>`) would give Node-RED
its own browser origin and isolate it from EdgeShell for free. The owner chose
the path on the main address instead, so Node-RED pages and EdgeShell share
one origin. The browser then lets a script on a Node-RED page (a flow's
dashboard, a node's own UI, or a compromised node) send requests to EdgeShell
with the signed-in user's session, read EdgeShell pages, and open the node
terminal. This ADR records the defences that make that path acceptable.

## Decision

**Proxy.** nginx serves `/nodered/<node>/` by asking EdgeShell
(`/internal/nodered-check`) on every request. EdgeShell allows signed-in
operators and administrators (the roles that could open Node-RED before) and
answers with the node's address in a header nginx keeps to itself. The address
must be a plain host or IP. The browser never sees it and needs no VPN. Names
that are not URL-safe use `node-<id>`. The button goes through
`/nodes/<id>/nodered`, which writes the opening to the activity log.

**Isolation of the same origin.**

| Threat from a Node-RED page | Defence |
| --- | --- |
| Reading EdgeShell's cookies | EdgeShell's session cookie is HttpOnly; nginx sends Node-RED no cookies at all and drops any `Set-Cookie` from it |
| Calling EdgeShell with the user's session (fetch, XHR, form posts) | EdgeShell refuses any browser request whose `Referer` is a `/nodered/` page, and any script request (not a plain navigation) without a `Referer`, since a page can hide its own |
| Loading EdgeShell in a frame and reading it | EdgeShell pages send `frame-ancestors 'none'` and refuse requests for a frame |
| Opening an EdgeShell window and reading it | Node-RED pages get a different `Cross-Origin-Opener-Policy` from EdgeShell's (`unsafe-none` versus `same-origin`), so the browser cuts the window handle |
| Opening the node terminal (WebSockets carry no telling headers) | The terminal needs a ticket: signed, 60 seconds, for this user, node and session, issued only by a CSRF-checked POST that Node-RED pages are refused |
| Reaching SenseHive with the admin's pass | The SenseHive gate refuses the same requests |
| Driving another node's Node-RED (deploying flows elsewhere) | The check refuses `/nodered/<a>/` requests whose `Referer` is a `/nodered/<b>/` page |
| A service worker taking over pages | nginx refuses service-worker script requests under `/nodered/` |

The rules apply only to browsers (requests with Fetch Metadata,
`Sec-Fetch-*`). API clients with a bearer token and command-line tools are not
affected.

## Consequences

- One address for the whole fleet's Node-RED, behind EdgeShell's sign-in,
  2FA and roles, with every opening logged; no VPN needed on the user's side.
- Residual risk, smaller than with a separate origin but not zero: a hostile
  page can still open a WebSocket to another node's Node-RED (live comms, not
  deploys), and a browser that does not send `Referer` or Fetch Metadata
  weakens the request rules. Node-RED's own login (`adminAuth`) remains
  recommended on every node.
- Browser storage (`localStorage`) is shared by all nodes' Node-RED editors on
  this origin: editor preferences are common to all nodes.
- A browser extension that strips `Referer` from same-origin requests makes
  EdgeShell refuse its own scripts; the refusal says why.
- The nginx host must reach each node's Node-RED port over the private mesh.
