"""
Security Header Audit — CI/CD compatible.
Tests all response headers without requiring external infrastructure (Postgres, Redis, MinIO).
Uses Starlette TestClient to simulate requests.
Exit code 0 = all headers present, non-zero = failure.
"""
import os
import sys

os.environ["JWT_SECRET"] = "this-is-a-32-char-secret-for-testing-only!!"
os.environ.setdefault("APP_ENV", "production")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.testclient import TestClient

from app.main import app

client = TestClient(app)
errors = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [OK]  {name}")
    else:
        print(f"  [FAIL]  {name}" + (f" — {detail}" if detail else ""))
        errors.append(name)


def header_check(name, header_key, expected_prefix=""):
    """Check a header is present and optionally starts with expected_prefix."""
    r = client.get("/health")
    headers = {k.lower(): v for k, v in r.headers.items()}
    value = headers.get(header_key.lower())
    if not value:
        check(f"{name} ({header_key})", False, "MISSING")
    elif expected_prefix and not value.startswith(expected_prefix):
        check(f"{name} ({header_key})", False, f"Expected prefix '{expected_prefix}', got '{value[:60]}'")
    else:
        check(f"{name} ({header_key})", True)


# ============================================================
print("=" * 60)
print("  Security Header Audit")
print("=" * 60)

# ── 1. REQUIRED HEADERS ──
print("\n── Required Security Headers ──")

header_check("Content Security Policy",
             "content-security-policy",
             "default-src 'self'")

header_check("X-Content-Type-Options",
             "x-content-type-options",
             "nosniff")

header_check("X-Frame-Options",
             "x-frame-options",
             "DENY")

header_check("Referrer-Policy",
             "referrer-policy",
             "strict-origin-when-cross-origin")

header_check("X-XSS-Protection",
             "x-xss-protection",
             "1; mode=block")

header_check("Permissions-Policy",
             "permissions-policy",
             "accelerometer=()")

header_check("Reporting-Endpoints",
             "reporting-endpoints",
             "csp-endpoint=")

header_check("Cross-Origin-Resource-Policy",
             "cross-origin-resource-policy",
             "same-origin")

# ── 2. CSP CONTENT CHECKS ──
print("\n── CSP Content Checks ──")
r = client.get("/health")
csp = r.headers.get("content-security-policy", "")

check("CSP contains 'self'", "'self'" in csp)
check("CSP does NOT allow unpkg.com", "unpkg.com" not in csp)
check("CSP script-src has no 'unsafe-inline'", "script-src 'self' 'unsafe-inline'" not in csp)
check("CSP contains WebTorrent trackers",
      all(t in csp for t in ["tracker.webtorrent.dev", "tracker.openwebtorrent.com"]))
check("CSP contains report-uri", "report-uri " in csp and ("/csp-report" in csp or "/api/csp-report" in csp))
check("CSP contains report-to", "report-to csp-endpoint" in csp)
check("CSP contains worker-src blob:", "worker-src 'self' blob:" in csp)
check("CSP contains base-uri 'self'", "base-uri 'self'" in csp)

# ── 3. NON-DEV ORIGINS (should not appear in CI / production) ──
print("\n── Dev Origins (should NOT be present in CI) ──")
check("No localhost:9000 in CSP", "localhost:9000" not in csp)
check("No cdn.jsdelivr.net in CSP", "cdn.jsdelivr.net" not in csp)

# ── 4. AUTH ENFORCEMENT ──
print("\n── Authentication Checks ──")
for method, path in [("GET", "/auth/me"), ("GET", "/files"), ("POST", "/upload")]:
    r = client.request(method, path)
    check(f"{method} {path} requires auth", r.status_code in (401, 403),
          f"Got {r.status_code}")

r = client.get("/auth/me", headers={"Authorization": "Bearer invalid"})
check("Invalid token rejected", r.status_code in (401, 403),
      f"Got {r.status_code}")

# ── 5. CORS ──
print("\n── CORS Checks ──")
r = client.options("/health", headers={
    "Origin": "http://evil.com",
    "Access-Control-Request-Method": "GET",
})
check("Blocks unknown origin",
      r.headers.get("access-control-allow-origin", "") != "http://evil.com")

r = client.options("/health", headers={
    "Origin": "http://localhost:3000",
    "Access-Control-Request-Method": "GET",
})
check("Allows localhost:3000",
      r.headers.get("access-control-allow-origin") == "http://localhost:3000")

# ── 6. HTTP METHODS ──
print("\n── HTTP Method Restrictions ──")
for method in ["PUT", "DELETE", "PATCH", "TRACE", "CONNECT"]:
    r = client.request(method, "/health")
    check(f"{method} returns 405", r.status_code == 405, f"Got {r.status_code}")

# ── 7. NO SERVER DISCLOSURE ──
print("\n── Server Disclosure ──")
r = client.get("/health")
check("No server header leak", not r.headers.get("server", ""),
      f"Server: '{r.headers.get('server', '')}'")

# ── 8. PUBLIC ENDPOINTS ──
print("\n── Public Endpoints ──")
r = client.get("/health")
check("Health endpoint accessible", r.status_code == 200, f"Got {r.status_code}")

# ── 9. API DOCS DISABLED IN PRODUCTION ──
print("\n── API Docs (production) ──")
for path in ["/docs", "/redoc", "/openapi.json"]:
    r = client.get(path)
    check(f"{path} disabled (404)", r.status_code == 404, f"Got {r.status_code}")

# ── 10. SECURITY CONTACT ──
print("\n── Security Contact ──")
r = client.get("/.well-known/security.txt")
check("security.txt served", r.status_code == 200, f"Got {r.status_code}")
check("security.txt has Contact", "Contact:" in r.text, f"Got: {r.text[:80]}")

# ============================================================
print(f"\n{'='*60}")
if errors:
    print(f"  [FAIL]  FAILED — {len(errors)} check(s) failed:")
    for e in errors:
        print(f"       • {e}")
    print(f"{'='*60}")
    sys.exit(1)
else:
    print("  [OK]  ALL CHECKS PASSED")
    print(f"{'='*60}")
    sys.exit(0)
