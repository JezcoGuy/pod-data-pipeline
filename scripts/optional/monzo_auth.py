"""
monzo_auth.py
=============
One-time OAuth setup for the Monzo Business API.

Run this on a machine that can use a real browser (Windows desktop is the
intended target — the redirect goes to localhost:9292 on whatever machine
the browser opens on). After it finishes, copy the three updated
MONZO_ACCESS_TOKEN / MONZO_REFRESH_TOKEN / MONZO_TOKEN_EXPIRES lines from
the local .env into the VPS .env.

Flow:
  1. Generate auth URL with a CSRF state token, open it in the browser
  2. A tiny HTTP server on localhost:9292 captures the redirect's ?code=
  3. PAUSE — wait for the user to approve the access request in their
     Monzo registered email (Monzo's SCA step; cannot be skipped)
  4. Exchange the auth code for access + refresh tokens
  5. Write the three MONZO_* token vars back into the .env (preserving
     every other line byte-for-byte)
  6. Verify with GET /ping/whoami

Why port 9292: 8080 is taken by NocoDB on the VPS. The browser-side
machine doesn't have that conflict but we keep one redirect URI everywhere
so the Monzo developer-portal config matches.
"""

import http.server
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ─── Config ───────────────────────────────────────────────────────────────────

PORT             = 9292
CALLBACK_PATH    = "/callback"
MONZO_AUTH_URL   = "https://auth.monzo.com/"
MONZO_TOKEN_URL  = "https://api.monzo.com/oauth2/token"
MONZO_WHOAMI_URL = "https://api.monzo.com/ping/whoami"

# ─── .env helpers ─────────────────────────────────────────────────────────────

def find_env_file():
    """Prefer .env next to this script (Windows-side workflow), then cwd, then /opt/your_brand_id."""
    candidates = [
        Path(__file__).parent / ".env",
        Path.cwd() / ".env",
        Path(".env"),
    ]
    for p in candidates:
        if p.exists():
            return p
    raise SystemExit(
        "ERROR: no .env file found. Place a .env next to this script with at least\n"
        "  MONZO_CLIENT_ID=...\n  MONZO_CLIENT_SECRET=...\n  MONZO_REDIRECT_URI=http://localhost:9292/callback"
    )


def update_env(env_path, updates):
    """
    Replace or append the given keys in env_path. Preserves every other line
    byte-for-byte (important — the existing .env has values like the
    comma-joined SMTP_TO that mustn't be re-quoted).
    """
    pattern = re.compile(r"^\s*(MONZO_(?:ACCESS_TOKEN|REFRESH_TOKEN|TOKEN_EXPIRES))\s*=")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    seen, out = set(), []
    for line in lines:
        m = pattern.match(line)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    # Force Unix line endings — the .env is shared with the Linux VPS.
    with env_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")

# ─── HTTP callback server ─────────────────────────────────────────────────────

_captured       = {"code": None, "state": None, "error": None, "error_description": None}
_captured_event = threading.Event()


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(url.query)
        _captured["code"]              = (q.get("code")              or [None])[0]
        _captured["state"]             = (q.get("state")             or [None])[0]
        _captured["error"]             = (q.get("error")             or [None])[0]
        _captured["error_description"] = (q.get("error_description") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if _captured["code"]:
            body = (
                "<html><body style='font-family:system-ui;padding:40px'>"
                "<h2>Auth code received.</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            )
        else:
            err  = _captured.get("error") or "no code in callback"
            desc = _captured.get("error_description") or ""
            body = (
                "<html><body style='font-family:system-ui;padding:40px'>"
                f"<h2>Auth failed: {err}</h2><p>{desc}</p></body></html>"
            )
        self.wfile.write(body.encode("utf-8"))
        _captured_event.set()

    def log_message(self, *args, **kwargs):
        pass  # silence the default access log


def start_callback_server():
    server = http.server.HTTPServer(("127.0.0.1", PORT), CallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server

# ─── Main flow ────────────────────────────────────────────────────────────────

def main():
    env_path = find_env_file()
    load_dotenv(env_path, override=True)
    print(f"Using .env: {env_path}")

    client_id     = os.getenv("MONZO_CLIENT_ID")
    client_secret = os.getenv("MONZO_CLIENT_SECRET")
    redirect_uri  = os.getenv("MONZO_REDIRECT_URI", f"http://localhost:{PORT}/callback")

    missing = [k for k, v in (("MONZO_CLIENT_ID", client_id),
                              ("MONZO_CLIENT_SECRET", client_secret)) if not v]
    if missing:
        sys.exit(f"ERROR: {', '.join(missing)} not set in {env_path}")

    if f":{PORT}" not in redirect_uri:
        sys.exit(
            f"ERROR: MONZO_REDIRECT_URI={redirect_uri!r} but this script binds port {PORT}.\n"
            f"Update both the .env and the Monzo developer portal to: http://localhost:{PORT}/callback"
        )

    state = secrets.token_urlsafe(24)
    auth_params = {
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "state":         state,
    }
    auth_url = MONZO_AUTH_URL + "?" + urllib.parse.urlencode(auth_params)

    print(f"Starting callback server on http://localhost:{PORT}{CALLBACK_PATH} ...")
    server = start_callback_server()

    print("Opening Monzo auth URL in your browser:")
    print(f"  {auth_url}")
    print()
    webbrowser.open(auth_url)

    print("Waiting for the OAuth redirect (5 min timeout) ...")
    if not _captured_event.wait(timeout=300):
        server.shutdown()
        sys.exit("ERROR: no callback received within 5 minutes.")
    server.shutdown()

    if _captured.get("error"):
        sys.exit(f"OAuth error: {_captured['error']} — {_captured.get('error_description') or ''}")

    code        = _captured.get("code")
    state_back  = _captured.get("state")
    if not code:
        sys.exit("ERROR: callback returned no auth code.")
    if state_back != state:
        sys.exit(f"ERROR: CSRF state mismatch. expected={state!r} got={state_back!r}")

    print()
    print("✅ Auth code received. Now check your Monzo registered email and click Approve.")
    print("   Press Enter here when done.")
    try:
        input()
    except EOFError:
        sys.exit("ERROR: no stdin available to wait for email approval — run interactively.")

    print()
    print("Exchanging auth code for tokens ...")
    r = requests.post(
        MONZO_TOKEN_URL,
        data={
            "grant_type":    "authorization_code",
            "client_id":     client_id,
            "client_secret": client_secret,
            "redirect_uri":  redirect_uri,
            "code":          code,
        },
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"Token exchange failed ({r.status_code}): {r.text}")
    body          = r.json()
    access_token  = body["access_token"]
    refresh_token = body.get("refresh_token") or ""
    expires_in    = int(body.get("expires_in", 0))
    expires_at    = int(time.time()) + expires_in
    expires_at_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)

    print(f"  access_token  : {access_token[:24]}... ({len(access_token)} chars)")
    print(f"  refresh_token : {'present' if refresh_token else 'NOT RETURNED (refresh will be unavailable)'}")
    print(f"  expires_in    : {expires_in}s  ({expires_in // 3600}h {(expires_in % 3600) // 60}m)")
    print(f"  expires_at    : {expires_at}  ({expires_at_dt.isoformat()})")

    print()
    print(f"Writing token vars into {env_path} ...")
    update_env(env_path, {
        "MONZO_ACCESS_TOKEN":  access_token,
        "MONZO_REFRESH_TOKEN": refresh_token,
        "MONZO_TOKEN_EXPIRES": str(expires_at),
    })
    print("  done.")

    print()
    print("Verifying with GET /ping/whoami ...")
    r = requests.get(
        MONZO_WHOAMI_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    print(f"  HTTP {r.status_code}")
    print(f"  Body: {r.text}")
    if r.status_code != 200:
        print()
        print("⚠️  whoami did not return 200.")
        print("    If 403: the Monzo email approval has not landed yet. Approve it, then")
        print("    re-run a whoami call manually with the saved MONZO_ACCESS_TOKEN.")
        sys.exit(1)

    print()
    print("✅ Monzo auth complete.")
    print(f"   Token expires at {expires_at_dt.isoformat()}")
    print(f"   ({expires_in // 3600}h {(expires_in % 3600) // 60}m from now)")
    print()
    print("Next steps:")
    print(f"  1. Copy these three lines from {env_path} to the VPS .env at .env:")
    print( "       MONZO_ACCESS_TOKEN=...")
    print( "       MONZO_REFRESH_TOKEN=...")
    print( "       MONZO_TOKEN_EXPIRES=...")
    print( "  2. The main sync script will rotate access tokens automatically using")
    print( "     the refresh token — you should not need to re-auth unless the refresh")
    print( "     token expires or is revoked.")


if __name__ == "__main__":
    main()
