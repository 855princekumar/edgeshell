/* Hardware lifecycle dialogs (ADR 0015), shared by the Node Fleet and Hardware pages.
   esHardwareHistory(id, name)   a node's parts, changes, faults and timeline
   esReportIssue(ids, names)     report a fault on one or more nodes (every role)
   esDecideChange(change)        confirm or reject a detected change (operators)
   esFaultDialog(id)             a fault's timeline; operators move it on or resolve it
   esReplacePrompt(id, name)     "Is this a replacement for an existing node?" (operators)
   Everything is built with textContent: values from nodes and users never become HTML.
   window.ES_HW_CAN_RESOLVE tells the page whether to offer operator actions; the
   server checks the role on every request anyway. */
(function () {
    'use strict';
    var canResolve = function () { return !!window.ES_HW_CAN_RESOLVE; };
    var cats = null;

    function el(tag, css, text) {
        var e = document.createElement(tag);
        if (css) e.style.cssText = css;
        if (text != null) e.textContent = text;
        return e;
    }
    function when(iso) { return iso ? new Date(iso).toLocaleString() : '-'; }
    function days(n) { return n == null ? '-' : (n < 1 ? '<1 day' : Math.round(n) + ' days'); }
    function short(v) { v = v || ''; return v.length > 22 ? v.slice(0, 8) + '...' + v.slice(-6) : v; }
    function send(url, body) {
        return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin',
            body: JSON.stringify(body || {}) }).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (d) {
                if (!r.ok || d.success === false) throw new Error(d.message || ('HTTP ' + r.status));
                return d;
            });
        });
    }
    function get(url) {
        return fetch(url, { credentials: 'same-origin' }).then(function (r) {
            return r.json().then(function (d) { if (!r.ok) throw new Error(d.message || ('HTTP ' + r.status)); return d; });
        });
    }
    function categories() {
        if (cats) return Promise.resolve(cats);
        return get('/hardware/api/categories').then(function (d) { cats = d.categories; return cats; });
    }
    function label(key) {
        var c = (cats || []).filter(function (x) { return x.key === key; })[0];
        return c ? c.label : key;
    }
    var BADGE = { open: ['#fee2e2', '#991b1b'], in_progress: ['#fef3c7', '#92400e'], resolved: ['#dcfce7', '#166534'],
                  pending: ['#fef3c7', '#92400e'], confirmed: ['#dcfce7', '#166534'], rejected: ['#fee2e2', '#991b1b'],
                  reverted: ['#f3f4f6', '#374151'] };
    function badge(status) {
        var c = BADGE[status] || ['#f3f4f6', '#374151'];
        return el('span', 'display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;'
            + 'background:' + c[0] + ';color:' + c[1], status.replace('_', ' '));
    }

    /* A dialog that always fits the screen: fixed header and footer, scrolling middle. */
    function modal(id, title, subtitle, width) {
        var old = document.getElementById(id);
        if (old) old.remove();
        var o = el('div', 'position:fixed;inset:0;z-index:99996;background:rgba(15,23,42,.55);display:flex;'
            + 'align-items:center;justify-content:center;padding:12px');
        o.id = id;
        var box = el('div', 'background:#fff;border-radius:14px;width:100%;max-width:' + (width || 720) + 'px;display:flex;'
            + 'flex-direction:column;max-height:calc(100vh - 24px);max-height:calc(100dvh - 24px);overflow:hidden;'
            + 'box-shadow:0 10px 40px rgba(0,0,0,.25);font-size:14px;color:#374151');
        box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true'); box.setAttribute('aria-label', title);
        box.className = 'es-hw-box';
        var head = el('div', 'display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:16px 20px;'
            + 'border-bottom:1px solid #e5e7eb;flex-shrink:0');
        var t = el('div', 'min-width:0');
        t.appendChild(el('div', 'font-size:17px;font-weight:700;color:#111827;overflow-wrap:anywhere', title));
        if (subtitle) t.appendChild(el('div', 'color:#6b7280;font-size:12.5px;overflow-wrap:anywhere', subtitle));
        var x = el('button', 'border:0;background:none;font-size:22px;line-height:1;color:#6b7280;padding:2px 6px;cursor:pointer', '×');
        x.type = 'button'; x.setAttribute('aria-label', 'Close');
        head.appendChild(t); head.appendChild(x);
        var body = el('div', 'padding:16px 20px;overflow:auto;flex:1;min-height:0');
        var foot = el('div', 'display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;align-items:center;padding:12px 20px;'
            + 'border-top:1px solid #e5e7eb;flex-shrink:0');
        var msg = el('div', 'margin-right:auto;font-size:13px;min-width:0');
        foot.appendChild(msg);
        box.appendChild(head); box.appendChild(body); box.appendChild(foot);
        o.appendChild(box);
        function close() { o.remove(); document.removeEventListener('keydown', esc); }
        function esc(e) { if (e.key === 'Escape') close(); }
        x.onclick = close;
        o.addEventListener('click', function (e) { if (e.target === o) close(); });
        document.addEventListener('keydown', esc);
        document.body.appendChild(o);
        return { body: body, foot: foot, close: close, say: function (text, bad) {
            msg.textContent = text || ''; msg.style.color = bad ? '#b91c1c' : '#166534'; } };
    }
    function button(text, kind, onclick) {
        var b = el('button', '', text);
        b.type = 'button';
        b.className = 'btn btn-sm ' + (kind || 'btn-outline');
        b.onclick = onclick;
        return b;
    }
    function field(labelText, input, hint) {
        var w = el('label', 'display:block;margin-bottom:12px');
        w.appendChild(el('div', 'font-weight:600;font-size:13px;margin-bottom:4px;color:#111827', labelText));
        w.appendChild(input);
        if (hint) w.appendChild(el('div', 'font-size:12px;color:#6b7280;margin-top:3px', hint));
        return w;
    }
    function select(options, value) {
        var s = el('select', 'width:100%'); s.className = 'form-select form-select-sm';
        options.forEach(function (o) {
            var opt = el('option', '', o[1]); opt.value = o[0];
            if (o[0] === value) opt.selected = true;
            s.appendChild(opt);
        });
        return s;
    }
    function input(type, placeholder, max) {
        var i = el(type === 'textarea' ? 'textarea' : 'input', 'width:100%');
        if (type !== 'textarea') i.type = type; else i.rows = 3;
        i.className = 'form-control form-control-sm';
        if (placeholder) i.placeholder = placeholder;
        if (max) i.maxLength = max;
        return i;
    }
    function section(parent, title) {
        parent.appendChild(el('div', 'font-weight:700;color:#111827;margin:16px 0 6px', title));
    }
    function table(parent, heads, rows, wrap) {         // cells stay on one line unless their column is in `wrap`
        var box = el('div', 'overflow-x:auto');
        var t = el('table', 'width:100%;border-collapse:collapse;font-size:13px');
        var tr = el('tr');
        heads.forEach(function (h) { tr.appendChild(el('th', 'text-align:left;padding:5px 8px;border-bottom:1px solid #e5e7eb;'
            + 'color:#6b7280;font-weight:600;white-space:nowrap', h)); });
        t.appendChild(tr);
        rows.forEach(function (r) {
            var row = el('tr');
            r.forEach(function (c, i) {
                var td = el('td', 'padding:5px 8px;border-bottom:1px solid #f3f4f6;vertical-align:top;'
                    + ((wrap || []).indexOf(i) >= 0 ? 'min-width:160px;overflow-wrap:anywhere' : 'white-space:nowrap'));
                if (c instanceof Node) td.appendChild(c); else td.textContent = c == null ? '' : c;
                row.appendChild(td);
            });
            t.appendChild(row);
        });
        box.appendChild(t);
        parent.appendChild(box);
    }
    function refresh() { if (typeof window.esHwRefresh === 'function') window.esHwRefresh(); }

    /* ---------------------------------------------------------------- report */
    function esReportIssue(ids, names) {
        if (!ids || !ids.length) return;
        var m = modal('esHwReport', 'Report a hardware issue',
            ids.length === 1 ? names[0] : ids.length + ' nodes: ' + names.slice(0, 6).join(', ') + (ids.length > 6 ? '...' : ''), 560);
        categories().then(function (list) {
            var comp = select(list.map(function (c) { return [c.key, c.label]; }), 'power');
            var symptom = input('text', 'e.g. reboots at random, no lights, read-only file system', 160);
            var sev = select([['low', 'Low'], ['normal', 'Normal'], ['high', 'High'], ['critical', 'Critical']], 'normal');
            var started = input('datetime-local');
            var desc = input('textarea', 'What you saw, what you tried (optional)', 2000);
            m.body.appendChild(field('Component', comp));
            m.body.appendChild(field('Symptom', symptom));
            m.body.appendChild(field('Severity', sev));
            m.body.appendChild(field('Started (if known)', started, 'Leave empty if you only noticed it now.'));
            m.body.appendChild(field('Details', desc));
            comp.id = 'esHwComp'; symptom.id = 'esHwSymptom';
            var go = button('Report', 'btn-primary', function () {
                if (!symptom.value.trim()) { m.say('Describe the symptom.', true); symptom.focus(); return; }
                go.disabled = true;
                send('/hardware/api/report', { node_ids: ids, component: comp.value, symptom: symptom.value,
                    severity: sev.value, description: desc.value,
                    started_at: started.value ? new Date(started.value).toISOString() : null })
                    .then(function (d) { m.say('Reported (' + d.faults.length + ').'); setTimeout(m.close, 900); refresh(); })
                    .catch(function (e) { go.disabled = false; m.say(e.message, true); });
            });
            go.id = 'esHwReportGo';
            m.foot.appendChild(button('Cancel', '', m.close));
            m.foot.appendChild(go);
            symptom.focus();
        }).catch(function (e) { m.say(e.message, true); });
    }

    /* ---------------------------------------------------------------- change */
    function esDecideChange(change, done) {
        var m = modal('esHwChange', change.summary, change.node_name + ', detected ' + when(change.detected_at), 600);
        table(m.body, ['Part', 'On record', 'Now'], (change.changes || []).map(function (c) {
            return [c.label, (c.old_model ? c.old_model + ' ' : '') + short(c.old),
                    (c.new_model ? c.new_model + ' ' : '') + short(c.new) + (c.seen_on ? ' (was on ' + c.seen_on + ')' : '')];
        }), [1, 2]);
        if (!canResolve()) {
            m.body.appendChild(el('p', 'margin-top:12px;color:#6b7280', 'An operator or administrator confirms or rejects this change.'));
            m.foot.appendChild(button('Close', '', m.close));
            return;
        }
        var reason = select([['fault', 'A part failed (counts as a fault)'], ['upgrade', 'Upgrade'],
            ['maintenance', 'Planned maintenance'], ['other', 'Other']], 'fault');
        var fault = select([['', 'Record a new fault for it']], '');
        var note = input('text', 'e.g. card worn out, engineer name, ticket number', 500);
        var faultField = field('Fault', fault, 'Pick an open report of this node to resolve it with the replacement.');
        m.body.appendChild(el('div', 'height:12px'));
        m.body.appendChild(field('Why was it changed?', reason));
        m.body.appendChild(faultField);
        m.body.appendChild(field('Note', note));
        reason.onchange = function () { faultField.style.display = reason.value === 'fault' ? '' : 'none'; };
        get('/hardware/api/faults?status=unresolved&node_id=' + change.node_id).then(function (d) {
            d.faults.forEach(function (f) {
                var o = el('option', '', label(f.component) + ': ' + f.symptom); o.value = f.id; fault.appendChild(o);
            });
        }).catch(function () {});
        function decide(decision) {
            send('/hardware/api/changes/' + change.id, { decision: decision, reason: reason.value,
                fault_id: fault.value ? Number(fault.value) : null, note: note.value })
                .then(function () { m.say(decision === 'confirm' ? 'Confirmed.' : 'Rejected: the node is flagged.');
                    setTimeout(m.close, 900); refresh(); if (done) done(); })
                .catch(function (e) { m.say(e.message, true); });
        }
        var rej = button('Reject: unexpected', 'btn-danger', function () {
            if (confirm('Reject this change? The node is flagged as having unexpected hardware until it matches the record again.')) decide('reject');
        });
        var ok = button('Confirm replacement', 'btn-primary', function () { decide('confirm'); });
        ok.id = 'esHwConfirm';
        m.foot.appendChild(rej); m.foot.appendChild(ok);
    }

    /* ---------------------------------------------------------------- fault */
    function esFaultDialog(id) {
        var m = modal('esHwFault', 'Fault #' + id, '', 620);
        categories().then(function () { return get('/hardware/api/faults/' + id); }).then(function (f) {
            m.body.textContent = '';
            var top = el('div', 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px');
            top.appendChild(badge(f.status));
            top.appendChild(el('b', 'color:#111827', f.node_name));
            top.appendChild(el('span', '', label(f.component) + ': ' + f.symptom));
            top.appendChild(el('span', 'color:#6b7280;font-size:12.5px', f.severity));
            m.body.appendChild(top);
            if (f.description) m.body.appendChild(el('p', 'white-space:pre-wrap;margin:6px 0', f.description));
            if (f.undervoltage_hint) m.body.appendChild(el('p', 'color:#92400e;margin:6px 0',
                'The board reported low power in the week before this report.'));
            if (f.status === 'resolved') m.body.appendChild(el('p', 'margin:6px 0',
                'Resolved ' + when(f.resolved_at) + ' by ' + f.resolved_by + ': ' + (f.resolution || '').replace(/_/g, ' ')
                + (f.new_serial ? ', new part ' + f.new_serial : '') + (f.hours_to_fix != null ? ' (' + f.hours_to_fix + ' h to fix' : '')
                + (f.downtime_min != null ? ', ' + f.downtime_min + ' min down)' : f.hours_to_fix != null ? ')' : '')));
            section(m.body, 'Timeline');
            (f.timeline || []).forEach(function (e) {
                var row = el('div', 'font-size:13px;padding:3px 0;border-bottom:1px solid #f3f4f6');
                row.appendChild(el('span', 'color:#6b7280;margin-right:8px', when(e.at)));
                row.appendChild(el('span', '', (e.user ? e.user + ': ' : '') + e.message));
                m.body.appendChild(row);
            });
            if (!canResolve()) { m.foot.appendChild(button('Close', '', m.close)); return; }
            var note = input('text', 'Note (optional)', 500);
            m.body.appendChild(el('div', 'height:10px'));
            if (f.status !== 'resolved') {
                var res = select([['replaced', 'Replaced'], ['repaired', 'Repaired'], ['reseated', 'Reseated or reconnected'],
                    ['no_fault_found', 'No fault found'], ['other', 'Other']], 'replaced');
                var serial = input('text', 'Serial of the new part (optional)', 128);
                m.body.appendChild(field('Resolution', res));
                m.body.appendChild(field('New part', serial, 'Starts the service life of parts EdgeShell cannot read, like a power supply.'));
                m.body.appendChild(field('Note', note));
                var act = function (action) {
                    send('/hardware/api/faults/' + id, { action: action, resolution: res.value, new_serial: serial.value, note: note.value })
                        .then(function () { refresh(); esFaultDialog(id); }).catch(function (e) { m.say(e.message, true); });
                };
                if (f.status === 'open') m.foot.appendChild(button('Mark in progress', '', function () { act('progress'); }));
                m.foot.appendChild(button('Add note', '', function () { act('note'); }));
                var r = button('Resolve', 'btn-primary', function () { act('resolve'); });
                r.id = 'esHwResolve';
                m.foot.appendChild(r);
            } else {
                m.body.appendChild(field('Note', note));
                m.foot.appendChild(button('Reopen', '', function () {
                    send('/hardware/api/faults/' + id, { action: 'reopen', note: note.value })
                        .then(function () { refresh(); esFaultDialog(id); }).catch(function (e) { m.say(e.message, true); });
                }));
            }
        }).catch(function (e) { m.say(e.message, true); });
    }

    /* ---------------------------------------------------------------- history */
    function esHardwareHistory(id, name) {
        var m = modal('esHwHistory', name, 'Hardware history', 860);
        m.body.appendChild(el('div', 'color:#6b7280', 'Loading...'));
        categories().then(function () { return get('/hardware/api/node/' + id); }).then(function (d) {
            m.body.textContent = '';
            var n = d.node;
            var facts = el('div', 'display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:#4b5563');
            facts.appendChild(el('span', '', 'Model: ' + (n.model || 'not read yet')));
            if (n.os) facts.appendChild(el('span', '', 'OS: ' + n.os));
            facts.appendChild(el('span', '', 'Last read: ' + when(n.scanned_at)));
            m.body.appendChild(facts);
            if (d.lineage.length > 1) m.body.appendChild(el('p', 'margin:8px 0 0', 'Replaces ' + d.lineage.slice(1).map(function (x) {
                return x.name + (x.retired_at ? ' (retired ' + when(x.retired_at) + ')' : ''); }).join(', then ')));
            if (d.replaced_by) m.body.appendChild(el('p', 'margin:8px 0 0;color:#92400e', 'Retired: replaced by ' + d.replaced_by.name));
            if (n.alert) m.body.appendChild(el('p', 'margin:8px 0 0;color:#b91c1c;font-weight:600',
                'Unexpected hardware: a change was rejected. It clears when the hardware matches the record again.'));
            if (n.undervoltage_at) m.body.appendChild(el('p', 'margin:8px 0 0;color:#92400e',
                'Low power (under-voltage) last reported ' + when(n.undervoltage_at) + ': check the power supply and cable.'));
            var pending = d.changes.filter(function (c) { return c.status === 'pending'; })[0];
            if (pending) {
                var p = el('div', 'margin-top:10px;padding:10px 12px;border:1px solid #fcd34d;background:#fffbeb;border-radius:10px;'
                    + 'display:flex;gap:10px;align-items:center;flex-wrap:wrap');
                p.appendChild(el('span', 'font-weight:600;color:#92400e', 'Hardware changed: ' + pending.summary));
                p.appendChild(button(canResolve() ? 'Review' : 'Details', 'btn-warning', function () { esDecideChange(pending, function () { esHardwareHistory(id, name); }); }));
                m.body.appendChild(p);
            }
            if (d.candidates && d.candidates.length) {
                var q = el('div', 'margin-top:10px;padding:10px 12px;border:1px solid #bfdbfe;background:#eff6ff;border-radius:10px;'
                    + 'display:flex;gap:10px;align-items:center;flex-wrap:wrap');
                q.appendChild(el('span', 'font-weight:600;color:#1e40af', 'New hardware: is this a replacement for an existing node?'));
                q.appendChild(button('Answer', 'btn-primary', function () { esReplacePrompt(id, name); }));
                m.body.appendChild(q);
            }
            section(m.body, 'Parts in service');
            var many = d.lineage.length > 1;               // the Node column only when replaced nodes are included
            var live = d.components.filter(function (c) { return c.in_service; });
            if (live.length) table(m.body, ['Part', 'Model', 'Identifier', 'Since', 'In service'].concat(many ? ['Node'] : []), live.map(function (c) {
                return [c.label === c.kind ? label(c.kind) : c.label, c.model || '', short(c.identifier),
                        (c.installed_known ? '' : 'before ') + when(c.installed_at), days(c.service_days)].concat(many ? [c.node_name] : []);
            }), [1]);
            else m.body.appendChild(el('div', 'color:#6b7280', 'Not read yet: it is read with the next successful check of the node.'));
            var past = d.components.filter(function (c) { return !c.in_service; });
            if (past.length) {
                section(m.body, 'Past parts');
                table(m.body, ['Part', 'Model', 'Identifier', 'Served', 'Removed', 'Why'].concat(many ? ['Node'] : []), past.map(function (c) {
                    return [c.label === c.kind ? label(c.kind) : c.label, c.model || '', short(c.identifier),
                            (c.installed_known ? '' : 'at least ') + days(c.service_days), when(c.removed_at), c.removed_reason || ''].concat(many ? [c.node_name] : []);
                }), [1]);
            }
            section(m.body, 'Faults');
            if (d.faults.length) table(m.body, ['Reported', 'Component', 'Symptom', 'Status', ''], d.faults.map(function (f) {
                var open = el('a', 'cursor:pointer', 'Open'); open.onclick = function () { esFaultDialog(f.id); };
                return [when(f.created_at), label(f.component), f.symptom + (f.node_name !== name ? ' (' + f.node_name + ')' : ''), badge(f.status), open];
            }), [2]);
            else m.body.appendChild(el('div', 'color:#6b7280', 'No faults reported.'));
            section(m.body, 'Timeline');
            d.events.slice(0, 120).forEach(function (e) {
                var row = el('div', 'font-size:13px;padding:3px 0;border-bottom:1px solid #f3f4f6');
                row.appendChild(el('span', 'color:#6b7280;margin-right:8px;white-space:nowrap', when(e.at)));
                row.appendChild(el('span', '', (e.node_name !== name ? '[' + e.node_name + '] ' : '') + (e.user ? e.user + ': ' : '') + e.message));
                m.body.appendChild(row);
            });
            var rep = button('Report issue', 'btn-warning', function () { esReportIssue([id], [name]); });
            m.foot.appendChild(rep);
            m.foot.appendChild(button('Close', '', m.close));
        }).catch(function (e) { m.body.textContent = ''; m.say(e.message, true); });
    }

    /* ---------------------------------------------------------------- replacement */
    function esReplacePrompt(id, name) {
        var m = modal('esHwReplace', 'Is ' + name + ' a replacement?', 'For a node whose board, SD card or both were swapped', 640);
        Promise.all([categories(), get('/hardware/api/node/' + id + '/candidates')]).then(function (r) {
            var list = r[1].candidates;
            m.body.appendChild(el('p', 'margin:0 0 10px', 'If this hardware replaces a broken node, its history continues here and the old node is retired (kept, not deleted).'));
            var choice = null;
            var group = el('div', '');
            function option(value, text, sub) {
                var w = el('label', 'display:flex;gap:10px;align-items:flex-start;padding:8px 10px;border:1px solid #e5e7eb;'
                    + 'border-radius:10px;margin-bottom:6px;cursor:pointer');
                var rb = el('input'); rb.type = 'radio'; rb.name = 'esHwRep'; rb.value = value;
                rb.onchange = function () { choice = value; failedBox.style.display = value ? '' : 'none'; };
                w.appendChild(rb);
                var t = el('div', 'min-width:0');
                t.appendChild(el('div', 'font-weight:600;color:#111827', text));
                if (sub) t.appendChild(el('div', 'font-size:12.5px;color:#6b7280', sub));
                w.appendChild(t);
                group.appendChild(w);
            }
            list.forEach(function (c) {
                option(String(c.id), c.name, [c.location, c.status, c.reasons.join(', ')].filter(Boolean).join(' · '));
            });
            option('', 'No, it is a new node', 'It will not be asked again.');
            m.body.appendChild(group);
            var failedBox = el('div', 'display:none;margin-top:10px');
            failedBox.appendChild(el('div', 'font-weight:600;font-size:13px;margin-bottom:4px', 'What failed on the old node?'));
            var checks = [];
            r[0].forEach(function (c) {
                var w = el('label', 'margin-right:14px;font-size:13px;white-space:nowrap');
                var cb = el('input'); cb.type = 'checkbox'; cb.value = c.key;
                w.appendChild(cb); w.appendChild(document.createTextNode(' ' + c.label));
                checks.push(cb); failedBox.appendChild(w);
            });
            var note = input('text', 'e.g. water damage, ticket number', 500);
            failedBox.appendChild(el('div', 'height:8px'));
            failedBox.appendChild(field('Note', note));
            m.body.appendChild(failedBox);
            var save = button('Save', 'btn-primary', function () {
                if (choice === null) { m.say('Choose one.', true); return; }
                var p = choice ? send('/hardware/api/node/' + id + '/replaces', { replaces_node_id: Number(choice),
                    failed: checks.filter(function (c) { return c.checked; }).map(function (c) { return c.value; }), note: note.value })
                    : send('/hardware/api/node/' + id + '/not-replacement');
                p.then(function () { m.say('Saved.'); setTimeout(function () { m.close(); location.reload(); }, 700); })
                    .catch(function (e) { m.say(e.message, true); });
            });
            save.id = 'esHwReplaceSave';
            m.foot.appendChild(button('Later', '', m.close));
            m.foot.appendChild(save);
        }).catch(function (e) { m.say(e.message, true); });
    }

    window.esHardwareHistory = esHardwareHistory;
    window.esReportIssue = esReportIssue;
    window.esDecideChange = esDecideChange;
    window.esFaultDialog = esFaultDialog;
    window.esReplacePrompt = esReplacePrompt;
})();
