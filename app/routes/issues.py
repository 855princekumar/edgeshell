"""Issue / support ticket portal.

Visibility: a reporter sees only their own tickets; administrators see all.
Each ticket has a threaded message log. New tickets and replies notify the
other party by email through useSend when configured.
"""
from datetime import datetime
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, jsonify)
from flask_login import login_required, current_user
from app import db
from app.models.issue import Issue, IssueMessage, ISSUE_STATUSES
from app.models.user import User
from app.services.activity_service import log_activity
from app.services.email_service import (send_email, get_email_config,
                                        save_email_config)
from app.services.rbac import admin_required

issues_bp = Blueprint('issues', __name__, url_prefix='/issues')


def _admin_emails():
    return [u.email for u in User.query.filter_by(role='administrator', is_active=True).all()
            if u.email]


def _can_view(issue):
    return current_user.is_admin() or issue.reporter_id == current_user.id


@issues_bp.route('/')
@login_required
def index():
    if current_user.is_admin():
        issues = Issue.query.order_by(Issue.updated_at.desc()).all()
    else:
        issues = Issue.query.filter_by(reporter_id=current_user.id) \
            .order_by(Issue.updated_at.desc()).all()
    open_count = sum(1 for i in issues if i.status in ('open', 'in_progress'))
    return render_template('issues/index.html', issues=issues,
                           open_count=open_count, is_admin=current_user.is_admin())


@issues_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        body = request.form.get('body', '').strip()
        category = request.form.get('category', '').strip()
        priority = request.form.get('priority', 'normal').strip()
        if not subject or not body:
            flash('Subject and description are required.', 'danger')
            return render_template('issues/new.html')

        issue = Issue(reporter_id=current_user.id, subject=subject,
                      category=category or None, priority=priority, status='open')
        db.session.add(issue)
        db.session.flush()
        msg = IssueMessage(issue_id=issue.id, author_id=current_user.id,
                           author_name=current_user.username, is_staff=current_user.is_admin(),
                           body=body)
        db.session.add(msg)
        db.session.commit()
        log_activity('Issue reported', response=subject[:200], status='success')

        # Notify admins (and confirm to reporter).
        link = url_for('issues.detail', issue_id=issue.id, _external=True)
        send_email(_admin_emails(), f'[EdgeShell] New issue: {subject}',
                   _email_html('New issue reported', current_user.username, subject, body, link))
        if current_user.email:
            send_email(current_user.email, f'[EdgeShell] We received your issue: {subject}',
                       _email_html('Your issue was received', current_user.username, subject, body, link))

        flash('Your issue has been submitted.', 'success')
        return redirect(url_for('issues.detail', issue_id=issue.id))
    return render_template('issues/new.html')


@issues_bp.route('/<int:issue_id>')
@login_required
def detail(issue_id):
    issue = Issue.query.get_or_404(issue_id)
    if not _can_view(issue):
        abort(403)
    return render_template('issues/detail.html', issue=issue,
                           statuses=ISSUE_STATUSES, is_admin=current_user.is_admin())


@issues_bp.route('/<int:issue_id>/reply', methods=['POST'])
@login_required
def reply(issue_id):
    issue = Issue.query.get_or_404(issue_id)
    if not _can_view(issue):
        abort(403)
    body = request.form.get('body', '').strip()
    if not body:
        flash('Reply cannot be empty.', 'danger')
        return redirect(url_for('issues.detail', issue_id=issue.id))

    is_staff = current_user.is_admin()
    msg = IssueMessage(issue_id=issue.id, author_id=current_user.id,
                       author_name=current_user.username, is_staff=is_staff, body=body)
    issue.updated_at = datetime.utcnow()
    if is_staff and issue.status == 'open':
        issue.status = 'in_progress'
    db.session.add(msg)
    db.session.commit()

    # Notify the other party: admin reply -> reporter; reporter reply -> admins.
    link = url_for('issues.detail', issue_id=issue.id, _external=True)
    if is_staff:
        if issue.reporter and issue.reporter.email:
            send_email(issue.reporter.email,
                       f'[EdgeShell] Reply on your issue: {issue.subject}',
                       _email_html('New reply from support', current_user.username,
                                   issue.subject, body, link))
    else:
        send_email(_admin_emails(),
                   f'[EdgeShell] Reply on issue #{issue.id}: {issue.subject}',
                   _email_html('Reporter replied', current_user.username,
                               issue.subject, body, link))

    flash('Reply sent.', 'success')
    return redirect(url_for('issues.detail', issue_id=issue.id))


@issues_bp.route('/<int:issue_id>/status', methods=['POST'])
@login_required
def set_status(issue_id):
    if not current_user.is_admin():
        abort(403)
    issue = Issue.query.get_or_404(issue_id)
    status = request.form.get('status', '').strip()
    if status not in ISSUE_STATUSES:
        flash('Invalid status.', 'danger')
        return redirect(url_for('issues.detail', issue_id=issue.id))
    issue.status = status
    issue.updated_at = datetime.utcnow()
    db.session.commit()
    log_activity('Issue status', response=f'#{issue.id} -> {status}', status='success')
    # Let the reporter know.
    if issue.reporter and issue.reporter.email:
        link = url_for('issues.detail', issue_id=issue.id, _external=True)
        send_email(issue.reporter.email,
                   f'[EdgeShell] Issue #{issue.id} marked {status}',
                   _email_html(f'Status changed to {status}', current_user.username,
                               issue.subject, f'Your issue is now marked "{status}".', link))
    flash(f'Status set to {status}.', 'success')
    return redirect(url_for('issues.detail', issue_id=issue.id))


@issues_bp.route('/email-settings', methods=['GET', 'POST'])
@login_required
@admin_required
def email_settings():
    if request.method == 'POST':
        save_email_config(
            enabled=request.form.get('email_enabled') == 'on',
            base_url=request.form.get('usesend_base_url', '').strip(),
            sender=request.form.get('usesend_from', '').strip(),
            api_key=(request.form.get('usesend_api_key', '').strip() or None),
        )
        log_activity('Email settings updated', status='success')
        flash('Email settings saved.', 'success')
        if request.form.get('send_test'):
            target = current_user.email
            if not target:
                flash('Set an email on your account to send a test.', 'warning')
            else:
                ok, info = send_email(
                    target, 'EdgeShell test email',
                    '<p>useSend is configured correctly. This is a test from EdgeShell.</p>')
                flash(f'Test email: {"sent" if ok else info}',
                      'success' if ok else 'danger')
        return redirect(url_for('issues.email_settings'))

    cfg = get_email_config()
    return render_template('issues/email_settings.html', cfg=cfg,
                           configured=bool(cfg['api_key']))


def _email_html(headline, who, subject, body, link):
    body_html = (body or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;color:#0f172a">
      <h2 style="margin:0 0 8px">{headline}</h2>
      <p style="margin:0 0 4px"><strong>From:</strong> {who}</p>
      <p style="margin:0 0 12px"><strong>Subject:</strong> {subject}</p>
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;white-space:pre-wrap">{body_html}</div>
      <p style="margin:16px 0 0"><a href="{link}"
         style="background:#1a56db;color:#fff;padding:8px 16px;border-radius:6px;text-decoration:none">
         Open in EdgeShell</a></p>
    </div>"""
