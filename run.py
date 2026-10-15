"""
EdgeShell - Edge Fleet Management Platform
Single entry point.

  Linux server (EC2):   python3 run.py            install or update production
                                                   (asks for sudo, idempotent)
  Windows / local dev:  python run.py             dev server (venv, deps, .env)
  Force dev on Linux:   python3 run.py --dev

  Used by systemd (not for manual use):
    run.py --serve         record start event in DB, then exec Gunicorn
    run.py --event stop    record stop event + systemd result in DB
"""

import os
import sys
import subprocess
import platform

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT        = os.path.dirname(os.path.abspath(__file__))
VENV_DIR    = os.path.join(ROOT, "venv")
REQ_FILE    = os.path.join(ROOT, "requirements.txt")
ENV_FILE    = os.path.join(ROOT, ".env")
ENV_EXAMPLE = os.path.join(ROOT, ".env.example")

IS_WIN  = platform.system() == "Windows"
PYTHON  = os.path.join(VENV_DIR, "Scripts", "python.exe") if IS_WIN \
          else os.path.join(VENV_DIR, "bin", "python")
PIP     = os.path.join(VENV_DIR, "Scripts", "pip.exe") if IS_WIN \
          else os.path.join(VENV_DIR, "bin", "pip")

# Minimum SQLAlchemy version that works on Python 3.14+
SQLALCHEMY_MIN_PY314 = (2, 0, 36)

# ── Pretty print helpers ───────────────────────────────────────────────────────
def banner(msg):  print(f"\n  \033[94m▶\033[0m  {msg}")
def ok(msg):      print(f"  \033[92m✔\033[0m  {msg}")
def warn(msg):    print(f"  \033[93m⚠\033[0m  {msg}")
def err(msg):     print(f"  \033[91m✘\033[0m  {msg}")

def run_cmd(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)

def run_pip(*args):
    """Run pip inside the venv quietly."""
    run_cmd([PYTHON, "-m", "pip"] + list(args) + ["--quiet"])

# ── Step 1: Create venv ────────────────────────────────────────────────────────
def ensure_venv():
    if os.path.isfile(PYTHON):
        ok("Virtual environment ready.")
        return
    banner("Creating virtual environment…")
    run_cmd([sys.executable, "-m", "venv", VENV_DIR])
    ok(f"venv created → {VENV_DIR}")

# ── Step 2: Re-exec inside venv ───────────────────────────────────────────────
def reexec_in_venv():
    """Restart this script using the venv Python so all imports come from it."""
    if os.path.realpath(sys.executable) == os.path.realpath(PYTHON):
        return  # already running inside venv
    banner("Switching to venv interpreter...")
    # Always relaunch through subprocess (not os.execv): subprocess quotes the
    # argument list correctly on every platform, so a project path containing
    # spaces (e.g. "...\my folder\edgeshell") is passed intact. os.execv on
    # Windows does not quote spaces and would split the path.
    completed = subprocess.run([PYTHON, os.path.abspath(__file__)] + sys.argv[1:])
    sys.exit(completed.returncode)

# ── Step 3: Install / upgrade dependencies ────────────────────────────────────
def ensure_deps():
    """
    Install deps from requirements.txt.
    On Python 3.14+ also force-upgrade SQLAlchemy if the installed version
    is too old (the bug that causes the 'FastIntFlag' / __firstlineno__ error).
    """
    py = sys.version_info

    # Check whether core packages are importable
    probe = subprocess.run(
        [PYTHON, "-c", "import flask, flask_sqlalchemy, flask_bcrypt, paramiko, cryptography"],
        capture_output=True
    )
    needs_install = probe.returncode != 0

    if needs_install:
        banner("Installing dependencies (first run - this may take a minute)…")
        run_pip("install", "--upgrade", "pip")
        run_pip("install", "-r", REQ_FILE)
        ok("Dependencies installed.")
    else:
        ok("Dependencies present.")

    # ── Python 3.14+ SQLAlchemy compatibility check ──────────────────────────
    if py >= (3, 14):
        check_and_fix_sqlalchemy_py314()


def check_and_fix_sqlalchemy_py314():
    """
    SQLAlchemy < 2.0.36 uses IntFlag/FastIntFlag in a way that breaks on
    Python 3.14 due to PEP 747 changes to __firstlineno__.
    Detect this and upgrade automatically.
    """
    result = subprocess.run(
        [PYTHON, "-c",
         "import sqlalchemy; "
         "v=tuple(int(x) for x in sqlalchemy.__version__.split('.')[:3]); "
         "print('.'.join(str(x) for x in v)); "
         "exit(0 if v >= (2,0,36) else 1)"],
        capture_output=True, text=True
    )

    installed_ver = result.stdout.strip() or "unknown"

    if result.returncode != 0:
        # Version is too old OR SQLAlchemy itself crashes on import (the exact bug)
        warn(
            f"SQLAlchemy {installed_ver} is incompatible with Python {sys.version_info.major}"
            f".{sys.version_info.minor}. Auto-upgrading…"
        )
        run_pip("install", "--upgrade", "sqlalchemy", "flask-sqlalchemy")
        ok("SQLAlchemy upgraded. Re-checking…")

        # Verify the upgrade fixed it
        verify = subprocess.run(
            [PYTHON, "-c", "from flask_sqlalchemy import SQLAlchemy; print('OK')"],
            capture_output=True, text=True
        )
        if verify.returncode != 0:
            err("SQLAlchemy upgrade did not resolve the issue.")
            err(verify.stderr.strip())
            err(
                "\nManual fix: install Python 3.12 and run:\n"
                "  py -3.12 run.py   (Windows)\n"
                "  python3.12 run.py (Linux/macOS)"
            )
            sys.exit(1)
        ok("SQLAlchemy compatibility confirmed.")
    else:
        ok(f"SQLAlchemy {installed_ver} is compatible with Python {sys.version_info.major}.{sys.version_info.minor}.")


# ── Step 4: Bootstrap .env ────────────────────────────────────────────────────
def ensure_env():
    if os.path.isfile(ENV_FILE):
        ok(".env file found.")
        return

    if not os.path.isfile(ENV_EXAMPLE):
        warn(".env.example missing - using OS environment variables only.")
        return

    banner("Generating .env from template…")
    import secrets
    with open(ENV_EXAMPLE, "r") as f:
        content = f.read()

    content = content.replace(
        "CHANGE_ME_generate_a_32_char_random_secret_key", secrets.token_hex(32)
    ).replace(
        "CHANGE_ME_generate_a_32_char_credential_key", secrets.token_hex(32)
    )

    with open(ENV_FILE, "w") as f:
        f.write(content)

    ok(".env created with auto-generated SECRET_KEY and CREDENTIAL_KEY.")
    warn("Review .env before going to production.")


# ── Step 5: Start Flask ───────────────────────────────────────────────────────
def start_app():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except ImportError:
        pass

    # Simple CLI flag parsing
    args  = sys.argv[1:]
    host  = os.getenv("HOST", "0.0.0.0")
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    i = 0
    while i < len(args):
        if args[i] in ("--port", "-p") and i + 1 < len(args):
            port = int(args[i + 1]); i += 2
        elif args[i] in ("--host", "-H") and i + 1 < len(args):
            host = args[i + 1]; i += 2
        elif args[i] in ("--debug", "-d"):
            debug = True; i += 1
        else:
            i += 1

    display_host = "localhost" if host == "0.0.0.0" else host

    print(f"""
  \033[94m╔═══════════════════════════════════════╗
  ║       EdgeShell Fleet Manager         ║
  ║   Edge Node Control Plane v1.1.0      ║
  ╚═══════════════════════════════════════╝\033[0m
  URL   : \033[92mhttp://{display_host}:{port}\033[0m
  Python: {sys.version.split()[0]}
  Debug : {debug}
  Login : \033[93madmin / admin123\033[0m  (change on first login)
    """)

    from app import create_app
    application = create_app(os.getenv("FLASK_ENV", "development"))
    # The Werkzeug debugger runs arbitrary code for whoever reaches it, so it is
    # never turned on here; `debug` only enables the auto-reloader.
    application.run(host=host, port=port, debug=False, use_reloader=debug)


# ── Production: systemd hooks ─────────────────────────────────────────────────
def _load_prod_env():
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE, override=True)
    os.environ["FLASK_ENV"] = "production"


def _record(kind):
    """Write a start/stop row to service_events. Never blocks the service."""
    try:
        from app import create_app
        from app.services import service_events as se
        application = create_app("production")
        with application.app_context():
            ev = se.record_start() if kind == "start" else se.record_stop()
            print(f"edgeshell: service {ev.event} recorded ({ev.reason}): {ev.detail}", flush=True)
    except Exception as e:  # DB down etc.: log to journal and carry on
        print(f"edgeshell: could not record {kind} event: {e}", file=sys.stderr, flush=True)


def serve():
    """systemd ExecStart: record the start, then become Gunicorn (same PID)."""
    _load_prod_env()
    _record("start")
    gunicorn = os.path.join(VENV_DIR, "bin", "gunicorn")
    bind = f'{os.getenv("HOST", "127.0.0.1")}:{os.getenv("PORT", "5000")}'
    # One worker + threads: in-memory rate limits and SSH state stay consistent.
    argv = [gunicorn, "--chdir", ROOT, "--workers", "1", "--worker-class", "gthread",
            "--threads", os.getenv("GUNICORN_THREADS", "16"), "--bind", bind,
            "--timeout", "300", "--graceful-timeout", "30",
            "--forwarded-allow-ips", "127.0.0.1",
            "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
    os.execv(gunicorn, argv)


def production():
    """Linux: hand over to the provisioner as root."""
    if os.geteuid() != 0:
        print("  EdgeShell production setup needs root. Re-running with sudo...")
        os.execvp("sudo", ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    sys.path.insert(0, os.path.join(ROOT, "deploy"))
    import provision
    provision.main({"yes": "--yes" in sys.argv})


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--serve" in argv:
        serve()
    elif "--event" in argv:
        _load_prod_env()
        _record(argv[argv.index("--event") + 1] if len(argv) > argv.index("--event") + 1 else "stop")
    elif platform.system() == "Linux" and "--dev" not in argv:
        production()
    else:
        print("\n  \033[1mEdgeShell Bootstrap (dev)\033[0m")
        print("  " + "─" * 38)
        ensure_venv()       # 1. Create venv if missing
        reexec_in_venv()    # 2. Switch to venv Python
        ensure_deps()       # 3. pip install + Python 3.14 SQLAlchemy fix
        ensure_env()        # 4. Auto-generate .env
        start_app()         # 5. Boot Flask
