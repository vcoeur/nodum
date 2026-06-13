"""HTTP-level auth tests: the gate, the login/logout flow, and security headers.

Each test that needs an unauthenticated caller builds a fresh ``TestClient`` with
no cookie or header; ``/schema`` stands in for "a protected route".
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from nodum import api, auth


def _anon() -> TestClient:
    """A fresh client with no session cookie and no Authorization header."""
    return TestClient(api.app)


def test_protected_route_requires_auth(auth_configured: None) -> None:
    """A gated route is 401 without any credential."""
    assert _anon().get("/schema").status_code == 401


def test_healthz_is_open(auth_configured: None) -> None:
    """The liveness probe needs no auth."""
    assert _anon().get("/healthz").status_code == 200


def test_login_wrong_password_is_401(auth_configured: None) -> None:
    """Logging in with the wrong password is rejected with 401."""
    assert _anon().post("/auth/login", json={"password": "nope"}).status_code == 401


def test_login_sets_cookie_and_grants_access(auth_configured: None, test_password: str) -> None:
    """A correct login returns a token, sets the cookie, and unlocks gated routes."""
    client = _anon()
    response = client.post("/auth/login", json={"password": test_password})
    assert response.status_code == 200
    assert response.json()["token"]
    assert client.cookies.get(auth.COOKIE_NAME)
    # The cookie now rides in the jar, so the protected route succeeds.
    assert client.get("/schema").status_code == 200


def test_bearer_token_grants_access(auth_configured: None, session_token: str) -> None:
    """A valid Bearer token unlocks a gated route; tampering with it does not."""
    client = _anon()
    ok = client.get("/schema", headers={"Authorization": f"Bearer {session_token}"})
    assert ok.status_code == 200
    bad = client.get("/schema", headers={"Authorization": f"Bearer {session_token}xx"})
    assert bad.status_code == 401


def test_logout_clears_cookie(auth_configured: None, test_password: str) -> None:
    """After logout the session cookie is cleared and gated routes lock again."""
    client = _anon()
    client.post("/auth/login", json={"password": test_password})
    assert client.get("/schema").status_code == 200
    client.post("/auth/logout")
    assert client.get("/schema").status_code == 401


def test_unconfigured_returns_503(auth_configured: None, monkeypatch) -> None:
    """When no password is configured, a gated route returns 503 (not 401)."""
    monkeypatch.setattr(auth, "is_configured", lambda: False)
    assert _anon().get("/schema").status_code == 503


def test_web_root_redirects_to_login_when_anonymous(auth_configured: None) -> None:
    """An unauthenticated browser hitting / is redirected to /login."""
    response = _anon().get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_is_open(auth_configured: None) -> None:
    """The sign-in page itself is reachable without auth."""
    response = _anon().get("/login")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_security_headers_present(client: TestClient) -> None:
    """Every response carries the defence-in-depth headers."""
    response = client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
