"""Runtime configuration, loaded from the environment and an optional .env file.

The only required value is the PostgreSQL connection string. Everything is
resolved once into an immutable :class:`Settings`; the service layer reads it
through :func:`load_settings` on each connection.
"""

from __future__ import annotations

from dataclasses import dataclass

from environs import Env

# Defaults match docker-compose.yml. Postgres host port stays in the 54xx band
# (per the conception dev-port scheme); the API/web view serves on 8600.
DEFAULT_DATABASE_URL = "postgresql://nodum:nodum@localhost:5436/nodum"
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8600


@dataclass(frozen=True)
class Settings:
    """Effective configuration for a single process."""

    database_url: str
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    # Mark the session cookie Secure (HTTPS-only). Off by default for local HTTP
    # dev; set NODUM_COOKIE_SECURE=1 behind a TLS-terminating reverse proxy.
    cookie_secure: bool = False
    # Filesystem path to the built SPA (the React bundle). Set in the Docker image
    # (the full-app distribution); unset for a bare `pip install` (CLI/library),
    # where no UI is served. See nodum.web.
    web_dist: str | None = None


def load_settings() -> Settings:
    """Read configuration from the process environment, layered over a local .env."""
    env = Env()
    env.read_env()  # no-op when no .env file is present
    return Settings(
        database_url=env.str("NODUM_DATABASE_URL", DEFAULT_DATABASE_URL),
        api_host=env.str("NODUM_API_HOST", DEFAULT_API_HOST),
        api_port=env.int("NODUM_API_PORT", DEFAULT_API_PORT),
        cookie_secure=env.bool("NODUM_COOKIE_SECURE", False),
        web_dist=env.str("NODUM_WEB_DIST", None),
    )
