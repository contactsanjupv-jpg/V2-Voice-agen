# Atla — Frontend

Next.js 16 app: marketing pages (home, how-it-works, industries, pricing), sign-up/login (`/get-started`), the
onboarding wizard, checkout, and the authenticated dashboard (calls, leads, go-live, test call, and settings for
business, receptionist, voice, billing, security and account deletion). Every page talks to the backend in `../backend`.

## Local setup

```bash
npm ci
cp .env.local.example .env.local   # if present; otherwise create .env.local with the variables below
npm run dev
```

Open http://localhost:3000. The backend must be running for anything beyond the static marketing pages.

Environment variables (all `NEXT_PUBLIC_`, so they are visible in the browser; never put a secret behind that prefix):

- `NEXT_PUBLIC_API_BASE_URL`: the backend URL (default `http://localhost:8000`). It is also added to the CSP `connect-src`.
- `NEXT_PUBLIC_PADDLE_CLIENT_TOKEN`: Paddle's client-side token. It must belong to the same environment as the backend's
  `PADDLE_ENV` (`test_` for sandbox, `live_` for live), or checkout refuses to open.

## Checks

```bash
npm test            # node:test unit tests (no browser)
npm run lint
npx tsc --noEmit
npm run build -- --webpack
```

The build downloads Google Fonts, so it needs network access.

## What these tests do not cover

They test the logic (status, billing text, settings, calls and leads), not rendered pages in a browser. Checkout,
the test call (microphone) and the live Paddle/Retell flows have to be checked by hand in a sandbox.

## Security

- X-Frame-Options, Referrer-Policy and Permissions-Policy are set in `next.config.ts`; the Content-Security-Policy is
  built per request with a nonce in `src/proxy.ts` (`src/lib/csp.ts`).
- No secrets in this codebase: the only environment variables are the two public `NEXT_PUBLIC_` values above
  (a URL and a Paddle client token), which are not credentials.
- Session auth relies entirely on the backend's HttpOnly/Secure/SameSite
  cookie — this app never touches or stores the session token itself.
- Form validation is client-side UX only; the backend re-validates and
  is the actual enforcement point (never trust client-side checks alone).
