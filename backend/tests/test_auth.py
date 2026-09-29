from fastapi.testclient import TestClient

from app.main import app


def test_signup_login_me_logout_roundtrip():
    client = TestClient(app)
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": "roundtrip@example.com", "password": "correct-horse-battery", "organization_name": "Roundtrip Co"},
    )
    assert signup.status_code == 201

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "roundtrip@example.com"

    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 204

    me_after_logout = client.get("/api/v1/auth/me")
    assert me_after_logout.status_code == 401


def test_duplicate_signup_rejected():
    client = TestClient(app)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "dup@example.com", "password": "correct-horse-battery", "organization_name": "Dup Co"},
    )
    second = TestClient(app).post(
        "/api/v1/auth/signup",
        json={"email": "dup@example.com", "password": "another-password", "organization_name": "Dup Co 2"},
    )
    assert second.status_code == 409


def test_wrong_password_rejected():
    client = TestClient(app)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "wrongpw@example.com", "password": "correct-horse-battery", "organization_name": "WrongPW Co"},
    )
    login = TestClient(app).post(
        "/api/v1/auth/login", json={"email": "wrongpw@example.com", "password": "not-the-right-password"}
    )
    assert login.status_code == 401


def test_login_rate_limit_trips_after_configured_attempts():
    from app.config import get_settings

    limit = get_settings().LOGIN_ATTEMPTS_PER_15MIN
    client = TestClient(app)
    statuses = []
    for _ in range(limit + 3):
        resp = client.post(
            "/api/v1/auth/login", json={"email": "rate-limit-target@example.com", "password": "wrong"}
        )
        statuses.append(resp.status_code)
    assert 401 in statuses  # legitimate failed attempts still get a normal 401...
    assert 429 in statuses  # ...until the limit trips
    # once tripped, it must STAY tripped, not flap back to 401
    assert statuses[-1] == 429
