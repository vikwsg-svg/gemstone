"""The console is served by the API with a strict security policy."""

from tests.enterprise.helpers import BASE


def test_console_is_served_with_a_strict_policy(client):
    r = client.get(f"{BASE}/console/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert '<script src="app.js"></script>' in r.text and "<script>" not in r.text

    js = client.get(f"{BASE}/console/app.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert "innerHTML" not in js.text  # API values are always written as text
    assert client.get(f"{BASE}/console/app.css").headers["content-type"].startswith("text/css")


def test_console_path_without_slash_redirects(client):
    r = client.get(f"{BASE}/console", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "console/"


def test_console_is_not_in_the_api_spec(client):
    spec = client.get(f"{BASE}/openapi.json").json()
    assert not any(path.startswith("/console") for path in spec["paths"])
