"""Node-RED at /nodered/<node>/ through EdgeShell (ADR 0014): who may open it,
what nginx is told, and how EdgeShell keeps Node-RED pages (same origin) from
using the EdgeShell session."""
import pytest
from test_heartbeat import app, login  # noqa: F401  (fixture and helper)

from app import db
from app.models import ActivityLog
from app.models.node import Node
from app.models.user import User

FROM_NODERED = {'Referer': 'https://fleet.test/nodered/pi-00/', 'Sec-Fetch-Site': 'same-origin',
                'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'}


def nid(app, name):
    with app.app_context():
        return Node.query.filter_by(name=name).first().id


def check(client, path, **headers):
    return client.get('/internal/nodered-check', headers={'X-Original-URI': path, **headers})


@pytest.fixture
def monitor(app):  # noqa: F811
    with app.app_context():
        m = User(username='mon', email='mon@fleet.test', role='monitor', is_active=True, must_change_password=False)
        m.set_password('Monitor-pass-1')
        db.session.add(m)
        db.session.commit()
    return login(app, 'mon', 'Monitor-pass-1')


def test_the_button_logs_and_goes_to_the_named_address(app):  # noqa: F811
    c = login(app)
    r = c.get(f'/nodes/{nid(app, "pi-00")}/nodered')
    assert r.status_code == 302 and r.headers['Location'].endswith('/nodered/pi-00/')
    with app.app_context():
        assert ActivityLog.query.filter_by(action='Open Node-RED', node='pi-00').count() == 1
    page = c.get('/nodes/').get_data(as_text=True)
    assert '/nodes/' in page and '/nodered' in page and ':1880' not in page and "window.open('http://" not in page


def test_check_answers_nginx_with_the_address_for_operators(app, monitor):  # noqa: F811
    c = login(app)
    r = check(c, '/nodered/pi-00/red/main.min.js')
    assert r.status_code == 204 and r.headers['X-NodeRed-Upstream'] == '10.0.0.1:1880'
    assert check(app.test_client(), '/nodered/pi-00/').status_code == 401           # signed out
    assert check(monitor, '/nodered/pi-00/').status_code == 403                      # monitors: no
    assert check(c, '/nodered/nobody/').status_code == 403
    assert check(c, '/nodered/../etc/').status_code == 403
    assert check(c, '/elsewhere/').status_code == 403


def test_only_a_plain_address_ever_reaches_nginx(app):  # noqa: F811
    with app.app_context():
        n = Node.query.filter_by(name='pi-01').first()
        n.ip_address = '10.0.0.2/evil?x='
        db.session.commit()
    assert check(login(app), '/nodered/pi-01/').status_code == 403


def test_names_that_are_not_url_safe_use_the_node_id(app):  # noqa: F811
    with app.app_context():
        n = Node(name='Gate house #2', ip_address='10.0.0.9', username='pi', status='online')
        db.session.add(n)
        db.session.commit()
        node_id = n.id
    c = login(app)
    r = c.get(f'/nodes/{node_id}/nodered')
    assert r.headers['Location'].endswith(f'/nodered/node-{node_id}/')
    assert check(c, f'/nodered/node-{node_id}/').headers['X-NodeRed-Upstream'] == '10.0.0.9:1880'
    assert check(c, f'/nodered/node-{nid(app, "pi-00")}/').status_code == 403      # node-<id> only when needed


def test_one_nodes_node_red_cannot_drive_anothers(app):  # noqa: F811
    c = login(app)
    assert check(c, '/nodered/pi-01/flows', Referer='https://fleet.test/nodered/pi-00/').status_code == 403
    assert check(c, '/nodered/pi-01/flows', Referer='https://fleet.test/nodered/pi-01/').status_code == 204


def test_edgeshell_refuses_requests_made_by_node_red_pages(app):  # noqa: F811
    c = login(app)
    assert c.get('/users/', headers=FROM_NODERED).status_code == 403
    assert c.get('/users/', headers={'Referer': 'https://fleet.test/nodered/pi-00/'}).status_code == 403
    assert c.post(f'/nodes/{nid(app, "pi-00")}/shell-ticket', headers=FROM_NODERED).status_code == 403
    no_ref = {k: v for k, v in FROM_NODERED.items() if k != 'Referer'}
    assert c.get('/users/', headers=no_ref).status_code == 403                    # a hidden Referer is not trusted
    nav = {'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
    assert c.get('/users/', headers=nav).status_code == 200                       # typing the address works
    framed = {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'iframe',
              'Referer': 'https://fleet.test/'}
    assert c.get('/users/', headers=framed).status_code == 403
    own = {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty',
           'Referer': 'https://fleet.test/nodes/'}
    assert c.get('/monitoring/fleet-summary', headers=own).status_code == 200     # EdgeShell's own pages work
    assert "frame-ancestors 'none'" in c.get('/nodes/').headers['Content-Security-Policy']


def test_api_tokens_and_tools_are_not_affected(app):  # noqa: F811
    r = app.test_client().get('/api/v1/health')
    assert r.status_code == 200
    r = app.test_client().get('/api/v1/nodes', headers={'Authorization': 'Bearer nope', **FROM_NODERED})
    assert r.status_code == 401                                                  # the API decides, not the guard


def test_sensehive_is_refused_to_node_red_pages(app):  # noqa: F811
    c = login(app)
    assert c.get('/internal/admin-check', headers=FROM_NODERED).status_code == 403


def test_the_terminal_needs_a_fresh_ticket_from_edgeshell(app):  # noqa: F811
    from app.routes import shell
    c = login(app)
    node_id = nid(app, 'pi-00')
    r = c.post(f'/nodes/{node_id}/shell-ticket')
    assert r.status_code == 200
    ticket = r.get_json()['ticket']
    with app.app_context():
        uid = User.query.filter_by(username='admin').first().id
    with c.session_transaction() as sess:
        sid = sess.get('sid')
    with app.test_request_context():
        assert shell.ticket_ok(ticket, uid, node_id, sid)
        assert not shell.ticket_ok(ticket, uid, node_id + 1, sid)                 # for that node only
        assert not shell.ticket_ok(ticket, uid + 1, node_id, sid)                 # for that user only
        assert not shell.ticket_ok(ticket, uid, node_id, 'another-session')       # for that session only
        assert not shell.ticket_ok('forged', uid, node_id, sid)
        assert not shell.ticket_ok(None, uid, node_id, sid)


# ── Remote desktop (OpenRemoteGUI) at /remoteGUI/<node>/ ─────────────────────
def rcheck(client, path, **headers):
    return client.get('/internal/remotegui-check', headers={'X-Original-URI': path, **headers})


def test_the_remote_button_opens_novnc_on_the_named_address_with_a_ticket(app):  # noqa: F811
    from urllib.parse import parse_qs, unquote, urlsplit
    c = login(app)
    r = c.get(f'/nodes/{nid(app, "pi-00")}/remotegui')
    loc = r.headers['Location']
    assert r.status_code == 302 and urlsplit(loc).path == '/remoteGUI/pi-00/vnc.html'
    q = parse_qs(urlsplit(loc).query)
    assert q['autoconnect'] == ['true'] and q['path'][0].startswith('remoteGUI/pi-00/websockify?ticket=')
    assert '10.0.0.1' not in unquote(loc) and '6080' not in loc
    with app.app_context():
        assert ActivityLog.query.filter_by(action='Open remote desktop', node='pi-00').count() == 1
    page = c.get('/nodes/').get_data(as_text=True)
    assert 'data-ip=' not in page and '${ip}' not in page


def test_remote_desktop_check_and_its_websocket_ticket(app, monitor):  # noqa: F811
    from urllib.parse import parse_qs, urlsplit
    c = login(app)
    r = rcheck(c, '/remoteGUI/pi-00/vnc.html')
    assert r.status_code == 204 and r.headers['X-Node-Upstream'] == '10.0.0.1:6080'
    assert rcheck(app.test_client(), '/remoteGUI/pi-00/vnc.html').status_code == 401
    assert rcheck(monitor, '/remoteGUI/pi-00/vnc.html').status_code == 403
    ws = {'X-Original-Upgrade': 'websocket'}
    assert rcheck(c, '/remoteGUI/pi-00/websockify', **ws).status_code == 403          # keyboard needs a ticket
    loc = c.get(f'/nodes/{nid(app, "pi-00")}/remotegui').headers['Location']
    ticket = parse_qs(parse_qs(urlsplit(loc).query)['path'][0].split('?', 1)[1])['ticket'][0]
    assert rcheck(c, f'/remoteGUI/pi-00/websockify?ticket={ticket}', **ws).status_code == 204
    assert rcheck(c, f'/remoteGUI/pi-01/websockify?ticket={ticket}', **ws).status_code == 403   # that node only
    assert rcheck(c, '/remoteGUI/pi-00/websockify?ticket=forged', **ws).status_code == 403


def test_node_apps_cannot_reach_each_other_or_edgeshell(app):  # noqa: F811
    c = login(app)
    assert rcheck(c, '/remoteGUI/pi-00/vnc.html', Referer='https://fleet.test/nodered/pi-00/').status_code == 403
    assert check(c, '/nodered/pi-00/', Referer='https://fleet.test/remoteGUI/pi-00/vnc.html').status_code == 403
    assert rcheck(c, '/remoteGUI/pi-00/app/ui.js', Referer='https://fleet.test/remoteGUI/pi-00/vnc.html').status_code == 204
    assert c.get('/users/', headers={'Referer': 'https://fleet.test/remoteGUI/pi-00/vnc.html'}).status_code == 403


def test_the_status_check_never_returns_the_node_address(app, monkeypatch):  # noqa: F811
    from app.services import orgui_service
    monkeypatch.setattr(orgui_service, 'persist_check', lambda node: (True, 'RUNNING'))
    d = login(app).post(f'/nodes/{nid(app, "pi-00")}/orgui/status').get_json()
    assert d['installed'] and d['url'] == f'/nodes/{nid(app, "pi-00")}/remotegui'
