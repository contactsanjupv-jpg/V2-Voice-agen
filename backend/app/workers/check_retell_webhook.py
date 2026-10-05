"""
Read-only diagnostics for "Retell's webhooks get 401". Creates no calls, writes
nothing to the database, stores no events.

    python -m app.workers.check_retell_webhook

It separates the possible causes, so the fix is known before spending a test call:
  1. Which key this process loaded (last 4 characters only).
  2. Clock skew between this machine and Retell (signatures older/newer than 5 min are rejected).
  3. Whether each Retell agent's webhook_url points at THIS server's public URL.
  4. Whether the PUBLIC URL actually reaches this backend (/health, and the route list).
  5. Whether a request signed with the configured key is accepted through your PUBLIC URL,
     plus a wrong-signature control that must be rejected.
If 1-4 are all fine, the only remaining cause is that Retell signs with a DIFFERENT key
than RETELL_API_KEY, i.e. the key does not carry the dashboard's webhook badge.
"""
import hashlib
import hmac
import time
from email.utils import parsedate_to_datetime

import httpx

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.voice_agent import Agent

OK, WARN, FAIL = "  [ OK ]", "  [WARN]", "  [FAIL]"


def _sign(body: bytes, key: str) -> str:
    ts = str(int(time.time() * 1000))
    return f"v={ts},d={hmac.new(key.encode(), body + ts.encode(), hashlib.sha256).hexdigest()}"


def _describe(r: httpx.Response) -> str:
    ngrok = r.headers.get("ngrok-error-code")
    snippet = r.text.strip().replace("\n", " ")[:90]
    return f"HTTP {r.status_code} content-type={r.headers.get('content-type', '-').split(';')[0]} ngrok-error-code={ngrok or '-'} body={snippet!r}"


def main() -> None:
    s = get_settings()
    key = s.RETELL_API_KEY
    public = s.BACKEND_PUBLIC_URL.rstrip("/")
    hook = f"{public}/webhooks/retell"

    print("\n1. Key this process loaded")
    if not key:
        print(f"{FAIL} RETELL_API_KEY is empty")
        return
    print(f"{OK} length={len(key)} last4={key[-4:]}  <- compare with the key that has the WEBHOOK badge in the Retell dashboard")

    print("\n2. Clock skew vs Retell (limit is 300 s)")
    try:
        r = httpx.get(f"{s.RETELL_API_BASE_URL}/list-voices", headers={"Authorization": f"Bearer {key}"}, timeout=15)
        server_time = parsedate_to_datetime(r.headers["date"]).timestamp()
        skew = time.time() - server_time
        flag = OK if abs(skew) < 60 else (WARN if abs(skew) < 300 else FAIL)
        print(f"{flag} this machine is {skew:+.1f}s relative to Retell (API answered {r.status_code})")
    except Exception as e:  # noqa: BLE001
        print(f"{WARN} could not reach Retell to compare clocks: {type(e).__name__}")

    print("\n3. Agent webhook URLs (expected: %s)" % hook)
    try:
        db = SessionLocal()
        try:
            agents = db.query(Agent).filter(Agent.retell_agent_id.isnot(None)).order_by(Agent.updated_at.desc()).limit(3).all()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        agents = []
        print(f"{WARN} could not read agents from the database ({type(e).__name__}); is DATABASE_URL pointing at your dev database?")
    else:
        if not agents:
            print(f"{WARN} no agents with a Retell id in the database")
    for a in agents:
        try:
            r = httpx.get(f"{s.RETELL_API_BASE_URL}/get-agent/{a.retell_agent_id}", headers={"Authorization": f"Bearer {key}"}, timeout=15)
            url = (r.json() or {}).get("webhook_url") if r.status_code == 200 else f"(HTTP {r.status_code})"
            print(f"{OK if url == hook else WARN} {a.retell_agent_id}: webhook_url={url}")
        except Exception as e:  # noqa: BLE001
            print(f"{WARN} {a.retell_agent_id}: could not read agent ({type(e).__name__})")

    print("\n4. Does the PUBLIC URL reach THIS backend? (before testing signatures)")
    if "localhost" in public or "127.0.0.1" in public:
        print(f"{WARN} BACKEND_PUBLIC_URL is {public}: Retell cannot reach this. Set it to your ngrok HTTPS URL.")
    hdrs = {"ngrok-skip-browser-warning": "1"}
    reaches_backend = False
    try:
        h = httpx.get(f"{public}/health", headers=hdrs, timeout=15)
        reaches_backend = h.status_code == 200 and "ok" in h.text
        print(f"{OK if reaches_backend else FAIL} GET /health -> {_describe(h)}")
        o = httpx.get(f"{public}/openapi.json", headers=hdrs, timeout=15)
        if o.status_code == 200:
            has_route = "/webhooks/retell" in o.json().get("paths", {})
            print(f"{OK if has_route else FAIL} the app behind the URL {'HAS' if has_route else 'does NOT have'} POST /webhooks/retell")
            if not has_route:
                reaches_backend = False
        else:
            print(f"{WARN} GET /openapi.json -> {_describe(o)} (docs are off outside development; not an error)")
    except Exception as e:  # noqa: BLE001
        print(f"{FAIL} could not reach {public}: {type(e).__name__}: {e}")

    print("\n5. Signed request (stores nothing: the payload is deliberately incomplete)")
    body = b'{"event":"diagnostic"}'
    try:
        good = httpx.post(hook, content=body, headers={**hdrs, "x-retell-signature": _sign(body, key), "content-type": "application/json"}, timeout=15)
        bad = httpx.post(hook, content=body, headers={**hdrs, "x-retell-signature": _sign(body, key + "x"), "content-type": "application/json"}, timeout=15)
        print(f"{OK if good.status_code == 400 else FAIL} correctly signed -> {_describe(good)}  (400 = signature ACCEPTED; 401 = REJECTED)")
        print(f"{OK if bad.status_code == 401 else FAIL} wrongly signed   -> {_describe(bad)}  (must be 401)")
    except Exception as e:  # noqa: BLE001
        print(f"{FAIL} could not reach {hook}: {type(e).__name__}: {e}")
        good = bad = None

    print("\nVerdict:")
    if not reaches_backend:
        print("  The public URL is NOT reaching this backend, so signatures cannot be judged yet. Look at step 4:")
        print("   - an HTML page / `ngrok-error-code` => ngrok itself answered (tunnel offline, wrong port, or a different ngrok endpoint).")
        print("   - JSON {\"detail\":\"Not Found\"} or no /webhooks/retell in the app => a DIFFERENT or OLDER server process is behind the tunnel.")
        print("   Fix the tunnel first: it must forward to the port THIS uvicorn listens on.")
    elif good is not None and good.status_code == 400 and bad is not None and bad.status_code == 401:
        print("  Our side is correct end to end: a correctly signed request is accepted through your public URL.")
        print("  If steps 1-3 were fine, Retell must be signing with a DIFFERENT key than RETELL_API_KEY.")
        print("  Put the webhook badge on the key whose last4 is shown in step 1, or set RETELL_API_KEY to the badged key.")
    else:
        print("  The tunnel reaches the backend but the signature check misbehaves; paste this output.")


if __name__ == "__main__":
    main()