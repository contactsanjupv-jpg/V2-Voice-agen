# Browser (end-to-end) tests

`frontend/e2e/browser.test.mjs` drives headless Chromium (Playwright) against a **running** stack: the real Next.js app, the real FastAPI app,
Postgres and Redis. **No provider is called**: Paddle checkout/cancel/refund, Retell calls/numbers and Google sign-in are NOT covered here and
still need the sandbox procedures in `docs/PADDLE_SANDBOX_TEST.md`, `docs/RETELL_VERIFICATION.md`.

Covered: signup (+validation, duplicate), login (wrong/right password), sign-out, expired session, the notification drawer (dialog, Escape,
preferences inside it), preference persistence across reload, the critical-alert explanation, per-person independence, error/retry states when
the API is unreachable, forgot-password (identical answer, no false success), reset-password (bad token, and end to end: new password works, old
password and old session do not, token is single-use).

## Run it (scratch database only)
```
createdb atla_e2e && DATABASE_URL=postgresql+psycopg://…/atla_e2e alembic upgrade head
SIGNUP_ATTEMPTS_PER_HOUR_PER_IP=200 FRONTEND_URL=http://localhost:3000 DATABASE_URL=… uvicorn app.main:app --port 8000   # the default limit is 5/hour/IP and the tests sign up many accounts
cd frontend && NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npx next dev -p 3000
npx playwright install chromium      # once
E2E_BASE_URL=http://localhost:3000 E2E_API_URL=http://localhost:8000 \
E2E_TOKEN_CMD='cd ../backend && DATABASE_URL=… python -c "import sys;from app.auth.reset_tokens import create_reset_token;print(create_reset_token(sys.argv[1]))" "$1"' \
npm run e2e
```
`E2E_TOKEN_CMD` receives the user id as `$1` and prints a fresh reset token (the real flow only emails it). Without it that one test is skipped.
Never point this at production: it creates accounts. `npm test` does not run these (they need the stack).
