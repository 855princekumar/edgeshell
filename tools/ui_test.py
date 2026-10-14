"""Browser UI test: layout on every screen size and zoom, and the main interactions.

Starts EdgeShell in-process with a large fleet (1500 nodes shaped like a real
one: categories, long names, mixed status), then drives Chromium through
every page on phone, tablet, laptop and desktop sizes and at browser zoom
levels, checking that:

  - no page scrolls sideways and no JavaScript error occurs,
  - nothing sticks out of its card, dialog, top bar or sidebar (text that
    spills out of a button or badge counts too),
  - filters, Select All, category scoping, dialogs and the fleet terminal
    work, and, with a real SSH node (--ssh 127.0.0.1:2299, e.g. a Pi Fleet
    Emulator node), the per-node terminal opens, runs a command and closes.

    pip install playwright && python -m playwright install chromium
    python tools/ui_test.py --nodes 1500 --ssh 127.0.0.1:2299 --out ui-report

Exit code 1 on any failure; screenshots of failures go to --out.
"""
import argparse
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('FLASK_ENV', 'testing')

# name, CSS width, CSS height, device scale. Browser zoom Z on a W px screen
# gives a W/Z px wide layout at Z device pixels per CSS pixel.
SCREENS = [('phone', 390, 844, 3), ('tablet', 820, 1180, 2), ('laptop', 1366, 768, 1),
           ('desktop', 1920, 1080, 1)]
ZOOMS = [('laptop', 1366, 768, z) for z in (0.67, 1.25, 1.5, 2.0)] + \
        [('desktop', 1920, 1080, z) for z in (1.5, 2.0)]
PAGES = ['/', '/nodes/', '/monitoring/', '/terminal/', '/files/', '/logs/', '/logs/activity',
         '/users/', '/users/add', '/account/', '/tailscale/', '/settings/', '/issues/',
         '/nodes/add', '/nodes/import']
TYPES = [('datalogger', 0.86, 'adani-ahmedabad-phase'), ('floc-detector', 0.10, 'floc-detector-unit'),
         ('bht', 0.02, 'bio-health-tracker'), ('rtu', 0.02, 'reliance-jamnagar-rtu-panel-east-gate')]

LAYOUT_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity !== 0; };
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
    + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '')
    + (el.textContent ? ' "' + el.textContent.replace(/\s+/g, ' ').trim().slice(0, 30) + '"' : '');
  const out = [];
  const vw = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > vw + 1)
  {
    const wide = [...document.body.querySelectorAll('*')].filter(el => vis(el)
      && el.getBoundingClientRect().right > vw + 1 && !(el.parentElement && el.parentElement.getBoundingClientRect().right > vw + 1));
    out.push('page scrolls sideways: content ' + document.documentElement.scrollWidth + 'px wide in a ' + vw
      + 'px window, widened by ' + wide.slice(0, 3).map(el => name(el) + ' (' + Math.round(el.getBoundingClientRect().width) + 'px)').join(', '));
  }
  const boxes = [...document.querySelectorAll('.card, .node-card, .stat-card, .cat-card, .topbar, .sidebar, #esCredsOverlay > *, #catEditor > *, #esJobWin > *, .es-shell > *')]
    .filter(vis);
  for (const el of document.querySelectorAll('.card, .term-main, .term-nodes, .node-card, .stat-card, .cat-card'))
    if (vis(el) && el.getBoundingClientRect().width < 64)
      out.push(name(el) + ' is squeezed to ' + Math.round(el.getBoundingClientRect().width) + 'px wide');
  // a short word or number split over two lines ("ONLI / NE", "7 / 91") means its box is too narrow
  const range = document.createRange();
  for (const el of document.querySelectorAll('body *')) {
    if (el.childElementCount || !vis(el)) continue;
    const t = el.textContent.trim();
    if (t.length < 2 || t.length > 20 || /\s/.test(t) || el.closest('pre, code, textarea, .xterm, #termOut')) continue;
    range.selectNodeContents(el);
    const tops = new Set([...range.getClientRects()].filter(r => r.width > 0).map(r => Math.round(r.top)));
    if (tops.size > 1) out.push('"' + t + '" is broken over ' + tops.size + ' lines in ' + name(el));
  }
  for (const box of boxes) {
    const b = box.getBoundingClientRect();
    for (const el of box.querySelectorAll('*')) {
      if (!vis(el)) continue;
      let p = el.parentElement, clipped = false;
      while (p && p !== box) {                 // inside a scroll or clip area of the box: intended
        if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(p).overflowX)) { clipped = true; break; }
        p = p.parentElement;
      }
      if (clipped) continue;
      const r = el.getBoundingClientRect();
      if (r.right > b.right + 1.5 || r.left < b.left - 1.5)
        out.push(name(el) + ' sticks out of ' + name(box) + ' by ' + Math.round(Math.max(r.right - b.right, b.left - r.left)) + 'px');
      const cs = getComputedStyle(el);
      if (!el.childElementCount && el.textContent.trim() && cs.display !== 'inline'
          && !/(auto|scroll|hidden|clip)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth + 1)
        out.push('text spills out of ' + name(el) + ' (' + el.scrollWidth + 'px in ' + el.clientWidth + 'px)');
    }
  }
  return [...new Set(out)].slice(0, 15);
}
"""


def seed(app, nodes, ssh):
    from datetime import datetime
    from app import db, _seed_defaults
    from app.models.node import Node
    from app.models.user import User
    random.seed(11)
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_defaults()
        a = User.query.filter_by(username='admin').first()
        a.must_change_password, a.email, a.max_sessions = False, 'owner@fleet.test', 4
        made = 0
        for t, share, prefix in TYPES:
            for k in range(max(1, int(nodes * share))):
                if made >= nodes:
                    break
                r = random.random()
                st = 'online' if r < 0.6 else ('auth_failed' if r < 0.93 else 'offline')
                n = Node(name=f'{prefix}-{k + 1:04d}', ip_address=f'100.{64 + made % 60}.{made % 250}.{k % 250 + 1}',
                         username='pi', node_type=t, environment='production', status=st,
                         location='adani', tags='datalogger;ts-online;tailscale',
                         cpu_percent=round(random.uniform(1, 95), 1) if st == 'online' else None,
                         ram_percent=round(random.uniform(10, 92), 1) if st == 'online' else None,
                         disk_percent=round(random.uniform(5, 90), 1) if st == 'online' else None,
                         temperature=round(random.uniform(40, 75), 1) if st == 'online' else None,
                         load_average='0.4 0.3 0.2' if st == 'online' else None, arch='aarch64',
                         last_error='Authentication failed: check the SSH username and password'
                         if st == 'auth_failed' else None)
                n.password = 'x'
                if st == 'auth_failed':           # the login policy paused them
                    n.login_paused_at = datetime.utcnow()
                    n.login_pause_reason = 'tailscale' if made % 2 else 'password'
                db.session.add(n)
                made += 1
        real_id = None
        if ssh:
            host, _, port = ssh.partition(':')
            n = Node(name='emulated-pi-real-ssh', ip_address=host, ssh_port=int(port or 22), username='pi',
                     node_type='datalogger', status='online', environment='production')
            n.password = 'raspberry'
            db.session.add(n)
            db.session.flush()
            real_id = n.id
        db.session.commit()
        from datetime import datetime
        for n in Node.query.filter_by(status='online').limit(400):
            n.last_seen = datetime.utcnow()
        # two hours of per-minute history, sampled every 10 minutes, for the export
        from datetime import timedelta
        from app.models import SystemMetrics
        t0 = datetime.utcnow() - timedelta(hours=2)
        rows = []
        for n in Node.query.all():
            for k in range(12):
                up = n.status == 'online' or k % 3
                rows.append(dict(node_id=n.id, timestamp=t0 + timedelta(minutes=10 * k),
                                 status='online' if up else n.status, cpu_percent=random.uniform(1, 90) if up else None,
                                 ram_percent=random.uniform(10, 90) if up else None, uptime_seconds=3600 + 600 * k))
        db.session.bulk_insert_mappings(SystemMetrics, rows)
        db.session.commit()
        return made + (1 if ssh else 0), real_id


class Report:
    def __init__(self, out):
        self.out, self.fails, self.passes = out, [], 0
        os.makedirs(out, exist_ok=True)

    def check(self, label, ok, detail='', page=None):
        if ok:
            self.passes += 1
            return True
        self.fails.append(f'{label}: {detail}')
        print(f'  FAIL  {label}: {detail}')
        if page is not None:
            safe = ''.join(c if c.isalnum() else '_' for c in label)[:90]
            try:
                page.screenshot(path=os.path.join(self.out, safe + '.png'))
            except Exception:
                pass
        return False


def settle(page):
    """Wait for fetched content; pages that poll never go fully idle."""
    try:
        page.wait_for_load_state('networkidle', timeout=4000)
    except Exception:
        pass
    page.wait_for_timeout(600)


def login(page, base):
    page.goto(base + '/login')
    page.fill('#username', 'admin')
    page.fill('#password', 'admin123')
    page.click('#submitBtn')
    page.wait_for_url(lambda u: '/login' not in u, timeout=15000)
    settle(page)                                # let the landing page finish its fetches


def run_layout(pw, base, rep, configs):
    for label, w, h, scale in configs:
        css_w, css_h = round(w / scale) if scale < 1 or label.startswith(('laptop', 'desktop')) and scale != 1 else w, \
            round(h / scale) if scale != 1 and label.startswith(('laptop', 'desktop')) else h
        zoom = f' at {int(scale * 100)}% zoom' if label in ('laptop', 'desktop') and scale != 1 else ''
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={'width': css_w, 'height': css_h}, device_scale_factor=scale,
                                  is_mobile=label == 'phone', has_touch=label in ('phone', 'tablet'))
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' and 'favicon' not in m.text
                and 'net::ERR' not in m.text else None)
        login(page, base)
        print(f'== {label}{zoom}: {css_w}x{css_h} CSS px')
        for path in PAGES:
            errors.clear()
            page.goto(base + path)
            settle(page)                 # charts and fetched cards render
            issues = page.evaluate(LAYOUT_JS)
            tag = f'{label}{zoom} {path}'
            rep.check(f'{tag} layout', not issues, '; '.join(issues[:6]), page)
            rep.check(f'{tag} no JavaScript errors', not errors, '; '.join(errors[:3]), page)
        page.goto(base + '/logout')             # at most 4 signed-in devices per user
        browser.close()


def run_interactions(pw, base, rep, total, real_id, label, w, h, scale):
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={'width': w, 'height': h}, device_scale_factor=scale,
                              is_mobile=label == 'phone', has_touch=label == 'phone')
    page = ctx.new_page()
    page.on('dialog', lambda d: d.accept())
    login(page, base)
    print(f'== interactions on {label} ({w}x{h})')

    page.goto(base + '/')
    page.wait_for_selector('.cat-card')
    cards = page.locator('.cat-card')
    rep.check(f'{label}: dashboard shows category cards', cards.count() >= 4, cards.count(), page)
    page.locator('.cat-card', has_text='Floc Detectors').click()
    page.wait_for_timeout(400)
    scope = page.inner_text('#cat-scope')
    rep.check(f'{label}: clicking a category scopes the dashboard', 'Floc Detectors' in scope, scope, page)
    rep.check(f'{label}: the scoped SLA title follows', 'Floc Detectors' in page.inner_text('#sla-title'),
              page.inner_text('#sla-title'), page)
    issues = page.evaluate(LAYOUT_JS)
    rep.check(f'{label}: scoped dashboard layout', not issues, '; '.join(issues[:4]), page)
    page.click('text=Edit categories')
    page.wait_for_selector('#catEditor .cat-row')
    issues = page.evaluate(LAYOUT_JS)
    rep.check(f'{label}: category editor fits the screen', not issues, '; '.join(issues[:4]), page)
    page.click('#catClose')
    rep.check(f'{label}: category editor closes', page.locator('#catEditor').count() == 0, '', page)

    page.goto(base + '/nodes/')
    page.wait_for_selector('.node-card')
    page.check('#selectAll')
    checked = page.evaluate("document.querySelectorAll('.node-checkbox:checked').length")
    rep.check(f'{label}: Select All checks every node', checked == total, f'{checked} of {total}', page)
    rep.check(f'{label}: bulk bar shows the selection', page.is_visible('#bulkBar')
              and str(total) in page.inner_text('#bulkCount'), page.inner_text('#bulkCount'), page)
    page.uncheck('#selectAll')
    rep.check(f'{label}: bulk bar hides with nothing selected', not page.is_visible('#bulkBar'), '', page)

    # Select All under a filter selects only what is shown; bulk dialogs fit
    page.select_option('#catFilter', 'Bio Health Trackers')
    page.check('#selectAll')
    sel = page.evaluate("selectedIds().length")
    want = page.evaluate("[...document.querySelectorAll('.node-card')].filter(c => c.style.display !== 'none').length")
    hidden = page.evaluate("[...document.querySelectorAll('.node-card')].filter(c => c.style.display === 'none' && c.querySelector('.node-checkbox').checked).length")
    rep.check(f'{label}: Select All under a filter selects only the shown nodes', sel == want and hidden == 0 and sel > 0,
              f'{sel} selected, {want} shown, {hidden} hidden checked', page)
    page.click('#bulkLoginBtn')
    page.wait_for_selector('#esBulkDlg #blUser')
    issues = page.evaluate(LAYOUT_JS)
    rep.check(f'{label}: bulk login dialog fits', not issues, '; '.join(issues[:4]), page)
    page.click('#esDlgOk')                                     # nothing filled in: refused in the page
    rep.check(f'{label}: bulk login needs a field', 'Enter a username' in page.inner_text('#esDlgMsg'),
              page.inner_text('#esDlgMsg'), page)
    page.click('#esDlgCancel')
    page.click('#bulkDeleteBtn')
    page.wait_for_selector('#esBulkDlg #esDelNames')
    issues = page.evaluate(LAYOUT_JS)
    rep.check(f'{label}: bulk delete dialog fits', not issues, '; '.join(issues[:4]), page)
    page.click('#esDlgCancel')
    rep.check(f'{label}: cancel deletes nothing', page.locator('.node-card').count() == total, '', page)
    page.click('#bulkClearBtn')
    page.select_option('#catFilter', 'Floc Detectors')
    shown = page.evaluate("[...document.querySelectorAll('.node-card')].filter(c => c.style.display !== 'none').length")
    want = page.evaluate("[...document.querySelectorAll('.node-card')].filter(c => c.dataset.category === 'Floc Detectors').length")
    rep.check(f'{label}: Category filter', shown == want and shown > 0, f'{shown} shown, {want} expected', page)
    page.select_option('#catFilter', '')
    page.select_option('#statusFilter', 'auth_failed')
    bad = page.evaluate("[...document.querySelectorAll('.node-card')].filter(c => c.style.display !== 'none' && c.dataset.status !== 'auth_failed').length")
    rep.check(f'{label}: status filter "login failed"', bad == 0, f'{bad} other cards shown', page)
    page.locator('.es-fix-login:visible').first.click()
    page.wait_for_selector('#esCredsOverlay form')
    issues = page.evaluate(LAYOUT_JS)
    rep.check(f'{label}: Fix login dialog fits', not issues, '; '.join(issues[:4]), page)
    page.click('#esCredsCancel')
    rep.check(f'{label}: Fix login dialog closes', page.locator('#esCredsOverlay').count() == 0, '', page)
    page.select_option('#statusFilter', '')

    # VPN page: filters and sorting, on a fake tailnet of 40 peers
    import json as _json
    from datetime import datetime as _dt, timedelta as _td
    peers = [{'name': f'peer-{i:02d}', 'dns_name': f'peer-{i:02d}.tail.ts.net', 'ip': f'100.100.0.{i + 1}',
              'os': ['linux', 'windows', 'android'][i % 3], 'online': i % 4 != 0,
              'connection': 'direct' if i % 4 == 1 else ('relay blr' if i % 4 == 2 else ''),
              'last_seen': (_dt.utcnow() - _td(days=i)).isoformat() + 'Z', 'rx_bytes': i * 1000, 'tx_bytes': 5,
              'exit_node': False} for i in range(40)]
    fake = {'connected': True, 'backend_state': 'Running', 'self_name': 'edgeshell', 'self_ips': ['100.64.0.1'],
            'nodes': peers, 'online_count': 30, 'version': '1.98.8', 'health': []}
    page.route('**/tailscale/status', lambda route: route.fulfill(status=200, content_type='application/json',
                                                                  body=_json.dumps(fake)))
    page.goto(base + '/tailscale/')
    page.wait_for_selector('#peers-body table')
    page.click('#peer-clear')
    rows = lambda: page.locator('#peers-body tbody tr').count()
    rep.check(f'{label}: VPN list shows every peer', rows() == 40, rows(), page)
    page.select_option('#peer-status', 'offline')
    rep.check(f'{label}: VPN status filter', rows() == 10, rows(), page)
    page.select_option('#peer-status', '')
    page.select_option('#peer-os', 'windows')
    rep.check(f'{label}: VPN OS filter', rows() == 13, rows(), page)
    page.select_option('#peer-os', '')
    page.select_option('#peer-link', 'direct')
    rep.check(f'{label}: VPN link filter', rows() == 10, rows(), page)
    page.select_option('#peer-link', '')
    page.select_option('#peer-seen', 'old')
    rep.check(f'{label}: VPN last-seen filter', rows() == 8, rows(), page)      # offline and 8+ days
    page.click('#peer-clear')
    page.evaluate("vpnSort = {key: 'status', dir: 1}")
    page.click('#peers-body th[data-sort="name"]')
    page.click('#peers-body th[data-sort="name"]')
    first = page.inner_text('#peers-body tbody tr:first-child td:first-child')
    rep.check(f'{label}: VPN sort by name, descending', 'peer-39' in first, first, page)
    issues = page.evaluate(LAYOUT_JS)
    rep.check(f'{label}: VPN page layout with filters', not issues, '; '.join(issues[:4]), page)
    page.unroute('**/tailscale/status')

    # Stats export: dialog, background job with progress, CSV download
    page.goto(base + '/')
    page.wait_for_selector('.cat-card')
    page.click('#exportBtn')
    page.wait_for_selector('#exportDlg #expStart')
    issues = page.evaluate(LAYOUT_JS)
    rep.check(f'{label}: export dialog fits', not issues, '; '.join(issues[:4]), page)
    page.select_option('#expPeriod', 'month')
    page.select_option('#expGrain', 'summary')
    with page.expect_download(timeout=60000) as dl:
        page.click('#expStart')
    d = dl.value
    path = os.path.join(rep.out, f'{label}-{d.suggested_filename}')
    d.save_as(path)
    with open(path, encoding='utf-8') as f:
        lines = f.read().splitlines()
    rep.check(f'{label}: export CSV has a row per node', len(lines) - 1 == total and 'uptime_percent' in lines[0],
              f'{len(lines) - 1} rows', page)
    rep.check(f'{label}: export progress reaches 100%',
              page.get_attribute('#expBar', 'style').replace(' ', '').find('width:100%') >= 0,
              page.get_attribute('#expBar', 'style'), page)
    page.click('#expClose')

    page.goto(base + '/terminal/')
    page.wait_for_selector('#nodeList')
    page.evaluate("""() => { const off = document.querySelector('.node-item[data-status=offline] .node-chk');
                            off.checked = true; off.dispatchEvent(new Event('change', {bubbles: true})); }""")
    page.fill('#cmdInput', 'echo hi')
    page.keyboard.press('Enter')
    page.wait_for_function("document.getElementById('termOut').textContent.includes('Skipped')", timeout=15000)
    box = page.locator('#termOut').bounding_box()
    rep.check(f'{label}: fleet terminal output is visible', box and box['height'] > 120 and box['width'] > 200,
              box, page)
    rep.check(f'{label}: fleet terminal skips offline nodes with a notice',
              'not reachable right now' in page.inner_text('#termOut'), page.inner_text('#termOut')[-200:], page)

    if real_id:
        page.goto(base + '/nodes/')
        page.select_option('#catFilter', '')
        page.fill('#nodeSearch', 'emulated-pi-real-ssh')
        page.evaluate("filterNodes()")
        page.locator(f'.es-shell-btn[data-id="{real_id}"]').click()
        page.wait_for_selector('.es-shell .xterm-rows', timeout=20000)
        page.wait_for_function("document.querySelector('.es-shell .es-state').textContent === 'connected'", timeout=20000)
        page.wait_for_timeout(1500)
        page.locator('.es-shell .xterm-helper-textarea').focus()
        page.keyboard.type('echo UI_OK_$((6*7))')
        page.keyboard.press('Enter')
        try:
            page.wait_for_function("document.querySelector('.es-shell .xterm-rows').innerText.includes('UI_OK_42')",
                                   timeout=15000)
            ok = True
        except Exception:
            ok = False
        rep.check(f'{label}: node terminal runs a command', ok,
                  page.inner_text('.es-shell .xterm-rows')[-200:], page)
        issues = page.evaluate(LAYOUT_JS)
        rep.check(f'{label}: terminal window fits the screen', not issues, '; '.join(issues[:4]), page)
        page.click('.es-shell .es-close')
        rep.check(f'{label}: closing removes the terminal window', page.locator('.es-shell').count() == 0, '', page)
        page.wait_for_timeout(1500)
        from app.routes import shell
        rep.check(f'{label}: closing ends the SSH session on the server', not shell._active, shell._active)
    page.goto(base + '/logout')
    browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--nodes', type=int, default=1500)
    ap.add_argument('--ssh', default=os.environ.get('UI_TEST_SSH', ''))
    ap.add_argument('--out', default='ui-report')
    ap.add_argument('--port', type=int, default=5077)
    ap.add_argument('--quick', action='store_true', help='base screen sizes only, no zoom levels')
    ap.add_argument('--pages', default='', help='comma-separated paths to check (default: all)')
    ap.add_argument('--only', default='', help='interactions only, on this screen (desktop or phone)')
    args = ap.parse_args()

    from app import create_app
    app = create_app('testing')
    app.config['IDLE_MINUTES'] = 0
    total, real_id = seed(app, args.nodes, args.ssh)
    threading.Thread(target=lambda: app.run(port=args.port, threaded=True, use_reloader=False),
                     daemon=True).start()
    time.sleep(1.5)
    base = f'http://127.0.0.1:{args.port}'
    rep = Report(args.out)
    if args.pages:
        PAGES[:] = [p for p in args.pages.split(',') if p]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        if not args.only:
            run_layout(pw, base, rep, SCREENS + ([] if args.quick else ZOOMS))
        if args.only in ('', 'desktop'):
            run_interactions(pw, base, rep, total, real_id, 'desktop', 1920, 1080, 1)
        if args.only in ('', 'phone'):
            run_interactions(pw, base, rep, total, real_id, 'phone', 390, 844, 3)
    print(f'\n{rep.passes} checks passed, {len(rep.fails)} failed')
    if rep.fails:
        print(f'Screenshots of failures: {os.path.abspath(args.out)}')
    return 1 if rep.fails else 0


if __name__ == '__main__':
    sys.exit(main())
