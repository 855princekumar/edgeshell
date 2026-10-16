/* Heartbeat history of a node (data from SenseHive through EdgeShell, ADR 0013):
   a chart for the last minute, hour, day, 7 or 30 days, the breaks, and CSV
   export. esHeartbeatHistory(id, name) opens it; esHeartbeatExport(ids, what,
   period) downloads a CSV for one or several nodes. */
(function () {
    'use strict';
    var RANGES = [['1m', '1 min'], ['1h', '1 hour'], ['1d', '1 day'], ['7d', '7 days'], ['30d', '30 days']];
    var PERIOD = { '1m': '1h', '1h': '1h', '1d': '1d', '7d': '7d', '30d': '30d' };

    function el(tag, css, text) {
        var e = document.createElement(tag);
        if (css) e.style.cssText = css;
        if (text != null) e.textContent = text;
        return e;
    }
    function duration(sec) {
        if (sec == null) return '-';
        if (sec < 90) return Math.round(sec) + ' s';
        if (sec < 5400) return Math.round(sec / 60) + ' min';
        if (sec < 172800) return (sec / 3600).toFixed(1) + ' h';
        return (sec / 86400).toFixed(1) + ' d';
    }

    function esHeartbeatExport(ids, what, period, onMessage) {
        var url = '/nodes/heartbeat/export.csv?ids=' + ids.join(',') + '&what=' + encodeURIComponent(what)
            + '&period=' + encodeURIComponent(period);
        return fetch(url, { credentials: 'same-origin' }).then(function (r) {
            if (!r.ok) return r.json().then(function (d) { throw new Error(d.message || ('HTTP ' + r.status)); });
            var cd = r.headers.get('Content-Disposition') || '';
            var m = /filename=([^;]+)/.exec(cd);
            return r.blob().then(function (b) {
                var a = document.createElement('a');
                a.href = URL.createObjectURL(b);
                a.download = m ? m[1].trim() : 'heartbeats.csv';
                document.body.appendChild(a); a.click(); a.remove();
                setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
                if (onMessage) onMessage('Downloaded ' + a.download, false);
            });
        }).catch(function (e) { if (onMessage) onMessage(e.message || 'Export failed', true); });
    }

    function esHeartbeatHistory(id, name) {
        var old = document.getElementById('esHbOverlay');
        if (old) old.remove();
        var o = el('div', 'position:fixed;inset:0;z-index:99997;background:rgba(15,23,42,.55);display:flex;'
            + 'align-items:center;justify-content:center;padding:12px');
        o.id = 'esHbOverlay';
        var box = el('div', 'background:#fff;border-radius:14px;width:100%;max-width:860px;display:flex;flex-direction:column;'
            + 'max-height:calc(100vh - 24px);max-height:calc(100dvh - 24px);overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,.25);'
            + 'font-size:14px;color:#374151');
        box.id = 'esHbBox';
        box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true');
        box.setAttribute('aria-label', 'Heartbeat history of ' + name);

        var head = el('div', 'display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:16px 20px;'
            + 'border-bottom:1px solid #e5e7eb;flex-shrink:0');
        var titles = el('div', 'min-width:0');
        titles.appendChild(el('div', 'font-size:17px;font-weight:700;color:#111827;overflow-wrap:anywhere', name));
        titles.appendChild(el('div', 'color:#6b7280;font-size:12.5px', 'MQTT heartbeat history (from SenseHive)'));
        var x = el('button', 'border:0;background:none;font-size:22px;line-height:1;color:#6b7280;padding:2px 6px;cursor:pointer', '×');
        x.type = 'button'; x.title = 'Close'; x.id = 'esHbClose';
        head.appendChild(titles); head.appendChild(x);

        var body = el('div', 'flex:1 1 auto;min-height:0;overflow-y:auto;padding:16px 20px');
        var tabs = el('div', 'display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px');
        tabs.setAttribute('role', 'tablist');
        var chart = el('div', 'width:100%;height:260px');
        chart.id = 'esHbChart';
        var msg = el('div', 'color:#6b7280;padding:8px 0;min-height:20px');
        msg.id = 'esHbMsg';
        var stats = el('div', 'display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-top:12px');
        stats.id = 'esHbStats';
        var breaks = el('div', 'margin-top:12px');
        body.appendChild(tabs); body.appendChild(msg); body.appendChild(chart); body.appendChild(stats); body.appendChild(breaks);

        var foot = el('div', 'display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;align-items:center;padding:12px 20px;'
            + 'border-top:1px solid #e5e7eb;flex-shrink:0');
        var fmsg = el('span', 'margin-right:auto;font-size:12.5px;color:#6b7280');
        var exp = el('button', 'padding:8px 14px;border-radius:8px;border:0;background:#1a56db;color:#fff;font-weight:600;cursor:pointer',
            'Export CSV');
        exp.type = 'button'; exp.id = 'esHbExport';
        var close = el('button', 'padding:8px 14px;border-radius:8px;border:1.5px solid #d1d5db;background:#fff;font-weight:600;cursor:pointer',
            'Close');
        close.type = 'button';
        foot.appendChild(fmsg); foot.appendChild(exp); foot.appendChild(close);

        box.appendChild(head); box.appendChild(body); box.appendChild(foot);
        o.appendChild(box);
        document.body.appendChild(o);

        var ec = null, range = '1h', timer = null;
        function shut() {
            clearInterval(timer);
            if (ec) ec.dispose();
            window.removeEventListener('resize', onResize);
            document.removeEventListener('keydown', onKey);
            o.remove();
        }
        function onResize() { if (ec) ec.resize(); }
        function onKey(e) { if (e.key === 'Escape') shut(); }
        x.addEventListener('click', shut);
        close.addEventListener('click', shut);
        o.addEventListener('click', function (e) { if (e.target === o) shut(); });
        window.addEventListener('resize', onResize);
        document.addEventListener('keydown', onKey);

        RANGES.forEach(function (r) {
            var b = el('button', '', r[1]);
            b.type = 'button'; b.setAttribute('role', 'tab'); b.setAttribute('data-range', r[0]);
            b.addEventListener('click', function () { range = r[0]; load(true); });
            tabs.appendChild(b);
        });
        function paintTabs() {
            Array.prototype.forEach.call(tabs.children, function (b) {
                var on = b.getAttribute('data-range') === range;
                b.setAttribute('aria-selected', on ? 'true' : 'false');
                b.style.cssText = 'padding:6px 12px;border-radius:8px;font-weight:600;cursor:pointer;white-space:nowrap;border:1.5px solid '
                    + (on ? '#1a56db;background:#1a56db;color:#fff' : '#e5e7eb;background:#f9fafb;color:#374151');
            });
        }
        function stat(label, value) {
            var c = el('div', 'background:#f9fafb;border-radius:8px;padding:8px 10px;min-width:0');
            c.appendChild(el('div', 'font-size:12px;color:#6b7280', label));
            c.appendChild(el('div', 'font-weight:700;color:#111827;overflow:hidden;text-overflow:ellipsis;white-space:nowrap', value));
            stats.appendChild(c);
        }
        function render(d) {
            msg.textContent = '';
            stats.innerHTML = ''; breaks.innerHTML = '';
            stat('Uptime', d.uptime + ' %');
            var cur = d.current_break;
            if (cur) {
                // the outage still going on; it may have begun before this range
                var down = el('div', 'font-size:13px;color:#b91c1c;background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:8px 10px;margin-bottom:10px');
                down.id = 'hbDownNow';
                down.appendChild(el('b', '', (cur.status === 'offline' ? 'Offline' : 'Silent') + ' for ' + duration(cur.seconds)));
                down.appendChild(document.createTextNode(' since the last heartbeat at ' + new Date(cur.from * 1000).toLocaleString()
                    + (cur.lwt_at ? '; last will received ' + new Date(cur.lwt_at * 1000).toLocaleString() : '')
                    + (cur.from < d.since ? ' (before this range)' : '')));
                breaks.appendChild(down);
            }
            if (d.kind === 'beats') {
                stat('Heartbeats received', String(d.received));
                stat('Typical interval', d.interval ? d.interval + ' s' : '-');
                stat('Longest break', duration(d.longest_break));
                stat('Breaks', String(d.breaks.length));
                stat('Delay (median)', d.lag_ms == null ? '-' : d.lag_ms + ' ms');
                if (d.breaks.length) {
                    breaks.appendChild(el('div', 'font-weight:700;color:#111827;margin-bottom:4px', 'Breaks'));
                    d.breaks.slice(-20).reverse().forEach(function (b) {
                        var line = el('div', 'font-size:13px');
                        line.appendChild(el('span', 'color:#dc2626;font-weight:600;margin-right:8px', duration(b.seconds)));
                        line.appendChild(el('span', 'color:#6b7280', new Date(b.from * 1000).toLocaleString() + (b.to ? '' : ' (still going)')
                            + (b.lwt_at ? ', last will ' + new Date(b.lwt_at * 1000).toLocaleTimeString() : '')));
                        breaks.appendChild(line);
                    });
                }
            } else {
                stat('Hours with a gap', String(d.points.filter(function (p) { return p[2] && p[1] < p[2]; }).length));
            }
            if (!window.echarts) { msg.textContent = 'Charts did not load.'; return; }
            if (!ec) ec = window.echarts.init(chart);
            var opt;
            if (d.kind === 'beats') {
                var pts = d.points.filter(function (p) { return p[1] !== 2; });
                var gaps = [];
                for (var i = 1; i < pts.length; i++) gaps.push([pts[i][0] * 1000, +(pts[i][0] - pts[i - 1][0]).toFixed(1)]);
                opt = {
                    grid: { left: 48, right: 16, top: 16, bottom: 36 },
                    tooltip: { trigger: 'axis', valueFormatter: function (v) { return v + ' s'; } },
                    xAxis: { type: 'time', min: d.since * 1000, max: d.until * 1000 },
                    yAxis: { type: 'value', name: 's between', nameGap: 8, nameTextStyle: { fontSize: 10 } },
                    series: [{
                        type: 'line', name: 'gap', data: gaps, showSymbol: gaps.length < 200, symbolSize: 5,
                        lineStyle: { width: 1.5, color: '#1a56db' }, itemStyle: { color: '#1a56db' },
                        markArea: { itemStyle: { color: 'rgba(220,38,38,0.12)' },
                            data: d.breaks.map(function (b) { return [{ xAxis: Math.max(b.from, d.since) * 1000 }, { xAxis: (b.to || d.until) * 1000 }]; }) },
                        markLine: { symbol: 'none', silent: true, data: [].concat(
                            d.interval ? [{ yAxis: d.interval, lineStyle: { type: 'dashed', color: '#1a56db' }, label: { formatter: 'usual' } }] : [],
                            d.points.filter(function (p) { return p[1] === 2; }).map(function (p) {
                                return { xAxis: p[0] * 1000, lineStyle: { color: '#dc2626' }, label: { formatter: 'last will' } }; })) }
                    }]
                };
            } else {
                opt = {
                    grid: { left: 48, right: 16, top: 16, bottom: 36 },
                    tooltip: { trigger: 'axis', valueFormatter: function (v) { return v + ' %'; } },
                    xAxis: { type: 'time', min: d.since * 1000, max: d.until * 1000 },
                    yAxis: { type: 'value', min: 0, max: 100, name: 'uptime %', nameGap: 8, nameTextStyle: { fontSize: 10 } },
                    series: [{
                        type: 'bar', name: 'uptime', barCategoryGap: '10%',
                        data: d.points.map(function (p) {
                            var pct = p[2] ? Math.round(1000 * p[1] / p[2]) / 10 : 0;
                            return { value: [p[0] * 1000, pct], itemStyle: { color: pct >= 99 ? '#059669' : pct >= 50 ? '#d97706' : '#dc2626',
                                opacity: p[2] ? 0.9 : 0.15 } };
                        })
                    }]
                };
            }
            ec.setOption(opt, true);
            ec.resize();
        }
        function load(fresh) {
            paintTabs();
            if (fresh) { msg.textContent = 'Loading...'; }
            fetch('/nodes/' + id + '/heartbeat/history?range=' + range, { credentials: 'same-origin' })
                .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
                .then(function (res) {
                    if (!res.ok || !res.d.success) {
                        msg.textContent = res.d.message || 'Could not load the history.';
                        stats.innerHTML = ''; breaks.innerHTML = '';
                        if (ec) ec.clear();
                        return;
                    }
                    render(res.d);
                })
                .catch(function () { msg.textContent = 'The server did not answer.'; });
            clearInterval(timer);
            timer = setInterval(function () { load(false); }, range === '1m' ? 5000 : 30000);
        }
        exp.addEventListener('click', function () {
            var what = (range === '7d' || range === '30d') ? 'minutes' : 'beats';
            fmsg.style.color = '#6b7280'; fmsg.textContent = 'Preparing...';
            esHeartbeatExport([id], what, PERIOD[range], function (text, bad) {
                fmsg.style.color = bad ? '#dc2626' : '#059669'; fmsg.textContent = text;
            });
        });
        load(true);
    }

    window.esHeartbeatHistory = esHeartbeatHistory;
    window.esHeartbeatExport = esHeartbeatExport;
    // Heartbeat badges on node cards open the history
    document.addEventListener('click', function (e) {
        var b = e.target.closest && e.target.closest('[data-hb-node]');
        if (b) esHeartbeatHistory(b.getAttribute('data-hb-node'), b.getAttribute('data-hb-name'));
    });
    document.addEventListener('keydown', function (e) {
        if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('[data-hb-node]')) {
            e.preventDefault();
            esHeartbeatHistory(e.target.getAttribute('data-hb-node'), e.target.getAttribute('data-hb-name'));
        }
    });
})();
