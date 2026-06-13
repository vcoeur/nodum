# nodum — the full-app image: API + built React UI + schema bootstrap.
# Multi-stage: the node stage builds the SPA; the python stage installs the
# package (the wheel, no UI) and serves the bundle from NODUM_WEB_DIST. Installers
# of the image need neither Node nor pip.

# ── Stage 1: build the React SPA ──────────────────────────────────────────────
FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: python runtime ───────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# hatch-vcs derives the version from the git tag, but .git is not in the build
# context — pass it in (CI sets --build-arg NODUM_VERSION=$tag); setuptools_scm
# (under hatch-vcs) honours SETUPTOOLS_SCM_PRETEND_VERSION.
ARG NODUM_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${NODUM_VERSION}

WORKDIR /app
COPY pyproject.toml README.md ./
COPY nodum/ ./nodum/
RUN pip install --no-cache-dir .

COPY --from=frontend /build/dist /app/web-dist
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV NODUM_WEB_DIST=/app/web-dist \
    NODUM_API_HOST=0.0.0.0 \
    NODUM_API_PORT=8600
EXPOSE 8600
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
