"""Build the GitHub Wiki from the repository's documentation.

The repository stays the single source: README.md, docs/*.md, the decision
records, SECURITY.md, CONTRIBUTING.md and CHANGELOG.md. This script turns them
into wiki pages (links and images rewritten so they work on the wiki), with a
sidebar and a footer. The workflow .github/workflows/wiki.yml runs it on every
documentation change on main and publishes the result, so the wiki never
drifts from the code.

    python tools/build_wiki.py <output dir> [--check]   (--check: fail on broken links)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = 'https://github.com/855princekumar/edgeshell'
RAW = 'https://raw.githubusercontent.com/855princekumar/edgeshell/main/'

# docs file -> wiki page (title)
DOCS = {
    'docs/GETTING-STARTED.md': 'Getting-Started',
    'docs/DEPLOY-EC2.md': 'Deploy-on-AWS-EC2',
    'docs/ARCHITECTURE.md': 'Architecture',
    'docs/RBAC.md': 'Roles-and-Access',
    'docs/API.md': 'API',
    'docs/EMAIL.md': 'Email',
    'docs/HEARTBEAT.md': 'MQTT-Heartbeat',
    'docs/APPS.md': 'Apps-on-the-Edge',
    'docs/VPN-NETWORK.md': 'VPN-and-Network',
    'docs/MIGRATION.md': 'Database-Migration',
    'docs/ISSUES.md': 'Support-Tickets',
    'docs/TROUBLESHOOTING.md': 'Troubleshooting',
    'SECURITY.md': 'Security',
    'CONTRIBUTING.md': 'Contributing',
    'CHANGELOG.md': 'Changelog',
    'docs/adr/README.md': 'Decision-Records',
}
# README sections that become their own pages (heading -> page)
README_PAGES = {
    'Features': 'Features',
    'Quick start': 'Quick-Start',
    'Validate before you deploy: sandbox with Pi Fleet Emulator': 'Sandbox',
    'Run on any cloud (amd64 and arm64)': 'Run-on-Any-Cloud',
    'Configuration': 'Configuration',
    'Management CLI': 'Management-CLI',
    'Project layout': 'Project-Layout',
    'Development': 'Development',
    'CI/CD and quality gates': 'Testing-and-CI',
    'Roadmap': 'Roadmap',
}
HOME_SECTIONS = ('Why EdgeShell', 'Screenshots', 'The ecosystem')

SIDEBAR = [
    ('Start', ['Home', 'Features', 'Quick-Start', 'Getting-Started', 'Sandbox']),
    ('Deploy', ['Deploy-on-AWS-EC2', 'Run-on-Any-Cloud', 'Configuration', 'Email', 'MQTT-Heartbeat', 'VPN-and-Network',
                'Database-Migration']),
    ('Use', ['Roles-and-Access', 'API', 'Support-Tickets', 'Management-CLI', 'Troubleshooting']),
    ('How it is built', ['Architecture', 'Security', 'Decision-Records', 'Testing-and-CI', 'Project-Layout']),
    ('Project', ['Development', 'Contributing', 'Roadmap', 'Changelog']),
]


def adr_pages():
    out = {}
    for f in sorted(os.listdir(os.path.join(ROOT, 'docs', 'adr'))):
        if re.match(r'\d{4}-.*\.md$', f):
            out['docs/adr/' + f] = 'ADR-' + f[:-3]
    return out


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def page_for(rel, pages):
    """Wiki page for a repository path, if it has one."""
    rel = os.path.normpath(rel).replace('\\', '/')
    return pages.get(rel)


def rewrite(text, src, pages):
    """Make links and images from `src` (a repo path) work on the wiki."""
    base = os.path.dirname(src)

    def fix(match):
        bang, label, target = match.group(1), match.group(2), match.group(3)
        if re.match(r'^[a-z]+:|^#|^mailto:', target):
            return match.group(0)
        path, _, anchor = target.partition('#')
        rel = os.path.normpath(os.path.join(base, path)).replace('\\', '/') if path else src
        if bang:                                             # images: straight from the repository
            return f'{bang}[{label}]({RAW}{rel})'
        page = page_for(rel, pages)
        if page:
            return f'[{label}]({page}' + (f'#{anchor}' if anchor else '') + ')'
        kind = 'tree' if os.path.isdir(os.path.join(ROOT, rel)) else 'blob'
        return f'[{label}]({REPO}/{kind}/main/{rel}' + (f'#{anchor}' if anchor else '') + ')'

    text = re.sub(r'(!?)\[([^\]]*)\]\(([^)\s]+)\)', fix, text)

    # Links whose label is itself an image, like [![CI](badge.svg)](CHANGELOG.md)
    def fix_target(match):
        target = match.group(1)
        path, _, anchor = target.partition('#')
        rel = os.path.normpath(os.path.join(base, path)).replace(chr(92), '/')
        page = page_for(rel, pages)
        if page:
            return '](' + page + (f'#{anchor}' if anchor else '') + ')'
        kind = 'tree' if os.path.isdir(os.path.join(ROOT, rel)) else 'blob'
        return f']({REPO}/{kind}/main/{rel}' + (f'#{anchor}' if anchor else '') + ')'
    text = re.sub(r'\]\(((?![a-z]+:|#)(?!' + re.escape(REPO) + r')[^)\s]+)\)', fix_target, text)
    # HTML images and links (README screenshots and banner)
    text = re.sub(r'(<img[^>]*\ssrc=")(?!https?:)([^"]+)"',
                  lambda m: f'{m.group(1)}{RAW}{os.path.normpath(os.path.join(base, m.group(2))).replace(chr(92), "/")}"',
                  text)
    text = re.sub(r'(<a[^>]*\shref=")(?!https?:|#)([^"]+)"',
                  lambda m: f'{m.group(1)}{REPO}/blob/main/{os.path.normpath(os.path.join(base, m.group(2))).replace(chr(92), "/")}"',
                  text)
    return text


def readme_sections():
    text = read('README.md')
    parts = re.split(r'^## ', text, flags=re.M)
    intro, sections = parts[0], {}
    for p in parts[1:]:
        title, _, body = p.partition('\n')
        sections[title.strip()] = body
    return intro, sections


def banner(src):
    return (f'> Generated from [`{src}`]({REPO}/blob/main/{src}) in the repository. '
            'Edit it there; this wiki is rebuilt on every change.\n\n')


def build(out):
    os.makedirs(out, exist_ok=True)
    pages = dict(DOCS, **adr_pages())
    pages['README.md'] = 'Home'
    written = {}

    for src, page in pages.items():
        if src == 'README.md':
            continue
        written[page] = banner(src) + rewrite(read(src), src, pages)

    intro, sections = readme_sections()
    home = rewrite(intro, 'README.md', pages)
    for title in HOME_SECTIONS:
        if title in sections:
            home += f'\n## {title}\n' + rewrite(sections[title], 'README.md', pages)
    home += '\n## Documentation\n\n' + '\n'.join(
        f'- **{group}**: ' + ', '.join(f'[{p.replace("-", " ")}]({p})' for p in items if p != 'Home')
        for group, items in SIDEBAR) + '\n'
    written['Home'] = banner('README.md') + home
    for title, page in README_PAGES.items():
        if title in sections:
            written[page] = banner('README.md') + f'# {title}\n' + rewrite(sections[title], 'README.md', pages)

    sidebar = ['**[EdgeShell](Home)**', '']
    for group, items in SIDEBAR:
        sidebar.append(f'**{group}**')
        sidebar += [f'- [{p.replace("-", " ")}]({p})' for p in items if p in written]
        sidebar.append('')
    adrs = sorted(p for p in written if p.startswith('ADR-'))
    if adrs:
        sidebar.append('**Decision records**')
        sidebar += [f'- [{p[4:8]}]({p})' for p in adrs]
    written['_Sidebar'] = '\n'.join(sidebar) + '\n'
    written['_Footer'] = (f'[Repository]({REPO}) · [Docker Hub](https://hub.docker.com/r/devprincekumar/edgeshell)'
                          f' · [Report a security problem]({REPO}/blob/main/SECURITY.md) · MIT licence\n')

    for name, text in written.items():
        with open(os.path.join(out, name + '.md'), 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
    return sorted(written)


def broken_links(out):
    """Links between wiki pages that point at no page."""
    names = {f[:-3] for f in os.listdir(out) if f.endswith('.md')}
    bad = []
    for f in sorted(os.listdir(out)):
        with open(os.path.join(out, f), encoding='utf-8') as fh:
            for m in re.finditer(r'\]\(([^)\s#]+)(#[^)]*)?\)', fh.read()):
                if not m.group(1).startswith(('http:', 'https:', 'mailto:')) and m.group(1) not in names:
                    bad.append(f'{f}: {m.group(1)}')
    return bad


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--check']
    if len(args) != 1:
        sys.exit(__doc__)
    names = build(args[0])
    print(f'{len(names)} wiki pages written to {args[0]}')
    bad = broken_links(args[0])
    if '--check' in sys.argv and bad:
        print('Broken wiki links:')
        for line in bad:
            print('  ' + line)
        sys.exit(1)
