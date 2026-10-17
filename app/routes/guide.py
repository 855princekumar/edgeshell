"""The field guide: an animated, full-screen tour for site technicians and
operators (report a fault, start and close a job, confirm a swapped part,
replace a dead node). Every signed-in user can open it from the (i) button in
the top bar. It is a page of the app, so it always matches the screens of
this version."""
from urllib.parse import urlsplit

from flask import Blueprint, render_template, request, url_for
from flask_login import login_required

guide_bp = Blueprint('guide', __name__, url_prefix='/guide')


def _back_url():
    """Where the close button returns to: the EdgeShell page the guide was
    opened from, never another site."""
    ref = request.referrer or ''
    parts = urlsplit(ref)
    if ref and parts.netloc == request.host and parts.scheme in ('http', 'https') \
            and parts.path.startswith('/') and not parts.path.startswith(('//', '/\\', '/guide')):
        return parts.path + ('?' + parts.query if parts.query else '')
    return url_for('dashboard.index')


@guide_bp.route('/')
@login_required
def field():
    return render_template('guide/field.html', back=_back_url(),
                           cfg={'hardware': url_for('hardware.index')})
