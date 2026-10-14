"""Commands EdgeShell refuses to run on nodes (fleet terminal and API).

Two lists: built-in patterns that are always refused (wiping the root
filesystem, writing over a disk, making a filesystem, fork bombs), and the
administrator's list in Settings, Security ("Blocked commands", one per line,
matched as text). Matching ignores case and runs of whitespace.

This is a guard rail against accidents and careless automation, not a
sandbox: an operator can always write a command differently. Real control
comes from roles, scopes, API_ALLOW_COMMANDS and the audit log.
"""
import re

_END = r'(?=\s|;|&|\||$)'
BUILT_IN = (
    ('fork bomb', re.compile(r':\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:')),
    ('rm -rf /', re.compile(r'\brm\s+(?:-[a-z]*\s+)*-[a-z]*(?:r[a-z]*f|f[a-z]*r)[a-z]*\s+(?:-\S+\s+)*/\*?' + _END)),
    ('rm -r -f /', re.compile(r'\brm\s+(?:-[a-z]+\s+)*(?:-r\s+-f|-f\s+-r)\s+(?:-\S+\s+)*/\*?' + _END)),
    ('rm --no-preserve-root', re.compile(r'--no-preserve-root')),
    ('mkfs', re.compile(r'(?:^|[;&|(]\s*|\bsudo\s+(?:-\S+\s+)*)mkfs(?:\.[a-z0-9]+)?(?=\s|$)')),
    ('dd onto a disk', re.compile(r'\bdd\b[^;&|]*\bof=/dev/(?:sd|hd|vd|xvd|nvme|mmcblk)')),
    ('write onto a disk', re.compile(r'>\s*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk)')),
    ('chmod -R 777 /', re.compile(r'\bchmod\s+-r\s+0?777\s+/' + _END)),
)


def _norm(s):
    return re.sub(r'\s+', ' ', (s or '').strip().lower())


def admin_list():
    from app.models import Settings
    row = Settings.query.filter_by(key='blocked_commands').first()
    text = row.value if row and row.value is not None else ''
    return [line for line in (_norm(x) for x in text.splitlines()) if line]


def blocked(command):
    """A description of the rule that blocks this command, or None."""
    c = _norm(command)
    for name, rx in BUILT_IN:
        if rx.search(c):
            return name
    for pat in admin_list():
        if pat in c:
            return pat
    return None
