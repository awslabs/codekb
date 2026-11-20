# CodeKB container image.
#
# Multi-stage: a Node stage builds the SPA (inlining deploy-time VITE_* values
# passed as --build-arg), a Python builder stage compiles/installs the backend
# and its dependencies into a venv, then a slim runtime stage copies in the
# venv and the SPA. The image therefore ALWAYS serves the UI — it cannot be
# built API-only.
#
# The runtime stage carries NO compilers or dev headers: gcc/g++/make and the
# Python dev headers tree-sitter needs exist only in the builder, which is
# discarded. Dependencies are pinned via backend/constraints.txt so a build is
# reproducible and a fresh upstream release can't silently change what ships.
#
# scripts/build-image.sh builds this locally (--arch sets BASE_PLATFORM), and
# the CDK Service stack builds it via fromAsset with the Cognito build args.
# See "How the image is built" in docs/development.md.

# Global build arg — declared before the first FROM so later FROMs can use it.
ARG BASE_PLATFORM=linux/amd64

# ── Stage 1: build the SPA ───────────────────────────────────────────
# Not platform-pinned: the frontend build emits arch-neutral static files, so
# letting it run on the build host's native arch avoids emulation. The VITE_*
# values are inlined into the bundle at build time (public config, not secrets).
FROM public.ecr.aws/docker/library/node:20 AS web
ARG VITE_BASE=/
ARG VITE_AUTH_ENABLED=false
ARG VITE_COGNITO_DOMAIN=
ARG VITE_COGNITO_CLIENT_ID=
ARG VITE_COGNITO_IDP_NAME=
ARG VITE_CODEKB_VERSION=
ENV VITE_AUTH_ENABLED=${VITE_AUTH_ENABLED} \
    VITE_COGNITO_DOMAIN=${VITE_COGNITO_DOMAIN} \
    VITE_COGNITO_CLIENT_ID=${VITE_COGNITO_CLIENT_ID} \
    VITE_COGNITO_IDP_NAME=${VITE_COGNITO_IDP_NAME} \
    VITE_CODEKB_VERSION=${VITE_CODEKB_VERSION}
WORKDIR /web
# Install deps first for layer caching. --ignore-scripts keeps dependency
# install scripts from running; the explicit build below pins --base.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npx tsc -b && npx vite build --mode production --base "${VITE_BASE}"
RUN find build -iname 'README*' -delete || true

# ── Stage 2: build the backend (has compilers; discarded) ────────────
# Everything that needs a toolchain happens here: tree-sitter's C extensions
# compile against gcc/g++/make and the Python dev headers. The result is a
# self-contained venv at /opt/venv that the runtime stage copies and nothing
# else from this stage ships.
FROM --platform=${BASE_PLATFORM} public.ecr.aws/amazonlinux/amazonlinux:2023 AS builder

RUN dnf install -y python3.11 python3.11-pip python3.11-devel \
    gcc gcc-c++ make && \
    dnf clean all

# Isolated venv so the runtime gets exactly these packages, nothing from the
# system Python.
RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# ── Layer 1: dependencies only (cached unless pyproject/constraints change) ─
COPY backend/pyproject.toml backend/constraints.txt ./
# A stub package, so setuptools can resolve "src" before the real source lands.
# default.yaml is declared as package-data, so it has to exist here too.
RUN mkdir -p src/core && touch src/__init__.py
COPY backend/src/core/default.yaml src/core/default.yaml
# -c constraints.txt pins the fully-resolved tree to exact versions.
RUN pip install --no-cache-dir -c constraints.txt .

# ── Layer 2: the real source (changes often; the install above is cached) ─
COPY backend/src/ src/

# Reinstall with the real source. --no-deps skips re-resolving dependencies.
RUN pip install --no-cache-dir --no-deps . && \
    rm -rf build *.egg-info src/*.egg-info

# ── Stage 3: runtime (no compilers, no dev headers) ──────────────────
FROM --platform=${BASE_PLATFORM} public.ecr.aws/amazonlinux/amazonlinux:2023

# Runtime needs only the Python runtime and git (the app clones repos).
# shadow-utils provides useradd. No gcc/g++/make/devel here — that's the point.
RUN dnf install -y python3.11 git shadow-utils && \
    dnf clean all && \
    ln -sf /usr/bin/python3.11 /usr/bin/python3

# The venv built in stage 2 — its python symlinks resolve to /usr/bin/python3.11,
# which exists here too.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# App source, owned by root (COPY defaults to uid 0). The unprivileged runtime
# user below can read and execute it but cannot modify it.
COPY --from=builder /app/src src

# Bake in the SPA built in stage 1 — this is what makes the image serve the UI.
# backend/src/ is /app/src here, so this lands at /app/src/static/dist, where
# backend/src/api/app.py resolves it (relative to its own package).
COPY --from=web /web/build src/static/dist

# Byte-compile as root so the .pyc files are root-owned and read-only to the app.
RUN python3 -m compileall -q /app/src 2>/dev/null || true

# ── Drop privileges ──────────────────────────────────────────────────
# High, fixed uid (10001) that won't collide with a host user. The app owns
# nothing under /app, so it can't rewrite its own code at runtime.
RUN useradd --uid 10001 --create-home codekb
USER codekb
# boto3 (~/.aws) and git (~/.gitconfig) resolve paths through HOME.
ENV HOME=/home/codekb

EXPOSE 8000

# Liveness: hit the app's own health endpoint using only the stdlib (no curl in
# the image). Non-zero exit marks the container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["python3", "-m", "uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
