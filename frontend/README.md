# Atla — Frontend

Next.js 16 marketing site + auth. 5 pages: home, how-it-works, industries,
pricing, get-started (real signup/login, wired to the backend).

## Local setup

```bash
npm install
cp .env.local.example .env.local   # points at your backend, default localhost:8000
npm run dev
```

Open http://localhost:3000. The backend (see ../backend) must be running
for signup/login on /get-started to actually work — everything else is
static marketing content.

## What's real vs not yet

- Real: signup/login form on /get-started, wired to the live backend
  session/auth system, with client-side validation and real error
  mapping (409 email taken, 401 wrong password, 429 rate limited).
- Not yet: the onboarding wizard (website import → review → voice →
  number → activate) and the authenticated dashboard. Signing up today
  successfully creates a real account and session but lands on a page
  that says so plainly — nothing is faked to look more finished than it is.

## Security

- CSP, X-Frame-Options, Referrer-Policy, Permissions-Policy set in
  `next.config.ts` for every route.
- No secrets in this codebase — `NEXT_PUBLIC_API_BASE_URL` is the only
  env var, and it's a public URL, not a credential (anything prefixed
  `NEXT_PUBLIC_` is bundled into client JS and visible to anyone — never
  put a real secret behind that prefix in this or any Next.js app).
- Session auth relies entirely on the backend's HttpOnly/Secure/SameSite
  cookie — this app never touches or stores the session token itself.
- Form validation is client-side UX only; the backend re-validates and
  is the actual enforcement point (never trust client-side checks alone).
