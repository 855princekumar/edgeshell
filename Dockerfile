# check=skip=SecretsUsedInArgOrEnv
# (false positive: ANSIBLE_HOST_KEY_CHECKING is a setting, not a secret)
# EdgeShell control plane. Multi-arch: linux/amd64 and linux/arm64
# (x86 clouds, AWS Graviton, Ampere Altra, Raspberry Pi 4/5).
#
# No secrets are baked in. SECRET_KEY and CREDENTIAL_KEY are supplied at run
# time, and the app refuses to start in production without them. The build
# context is an allowlist (.dockerignore), so .env, databases and keys can
# never be copied into a layer.

ARG PYTHON_VERSION=3.12

# ── Build: compile wheels into a venv (compilers never reach the runtime) ──
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libffi-dev \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r /tmp/requirements.txt

# ── Runtime ──────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="EdgeShell" \
      org.opencontainers.image.description="Self-hosted fleet management for Raspberry Pi and edge devices over SSH and Ansible." \
      org.opencontainers.image.url="https://github.com/855princekumar/edgeshell" \
      org.opencontainers.image.source="https://github.com/855princekumar/edgeshell" \
      org.opencontainers.image.documentation="https://github.com/855princekumar/edgeshell#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="855princekumar" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    FLASK_ENV=production \
    HOST=0.0.0.0 \
    PORT=5000 \
    GUNICORN_THREADS=32 \
    ANSIBLE_HOST_KEY_CHECKING=False \
    ANSIBLE_LOCAL_TEMP=/tmp/.ansible-local \
    EDGESHELL_VERSION=${VERSION}

# ssh client + sshpass for Ansible password auth, curl for the healthcheck,
# tini as PID 1 so signals reach Gunicorn and zombies are reaped.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client sshpass curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home --home-dir /home/edgeshell \
            --shell /usr/sbin/nologin edgeshell

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
# Code is owned by root and read-only to the app user; only data dirs are writable.
COPY app ./app
COPY ansible ./ansible
COPY config.py wsgi.py manage.py VERSION ./
RUN mkdir -p instance uploads ansible/inventory \
 && chown -R edgeshell:edgeshell instance uploads ansible/inventory

USER edgeshell
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# One worker with threads keeps in-memory rate limits consistent (same as the
# systemd deployment). Long timeout: fleet operations can run for minutes.
CMD ["sh", "-c", "exec gunicorn --workers 1 --worker-class gthread --threads \"$GUNICORN_THREADS\" --timeout 300 --graceful-timeout 30 --bind \"$HOST:$PORT\" --access-logfile - --error-logfile - wsgi:app"]
