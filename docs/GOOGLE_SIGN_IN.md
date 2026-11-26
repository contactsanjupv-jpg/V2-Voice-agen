# Continue with Google

OpenID Connect **authorization-code flow with PKCE**, run entirely by the backend. The browser never sees a Google token.

```
Browser -> GET  {API}/api/v1/auth/google/start        state+nonce+PKCE saved in Redis (10 min, single use); state cookie set
        -> Google (user picks an account / cancels)
        -> GET  {API}/api/v1/auth/google/callback     cookie == state, state consumed, code exchanged with client secret + verifier,
                                                      ID token verified (RS256 signature via Google JWKS, iss, aud, exp/iat, nonce, email_verified)
        -> Google account already linked (matched by `sub`): Atla session cookie -> {FRONTEND}/onboarding
        -> anything else: NO session, NO change. The verified identity waits server-side (15 min, tied to this browser by a
           cookie) and {FRONTEND}/get-started/google asks for the right next step (see "Accounts").
        -> any problem: {FRONTEND}/get-started?google_error=<code>
```

## Google Cloud Console (one time)
1. https://console.cloud.google.com -> pick/create a project -> **APIs & Services -> OAuth consent screen**. User type *External*,
   app name, support email, authorized domain; scopes `openid`, `email`, `profile` (non-sensitive, no Google review needed).
   While the app is in *Testing*, only listed test users can sign in; **Publish** it for everyone.
2. **Credentials -> Create credentials -> OAuth client ID -> Web application**.
3. **Authorized redirect URIs** (exact match, the BACKEND callback):
   - local: `http://localhost:8000/api/v1/auth/google/callback`
   - production: `https://<your-api-host>/api/v1/auth/google/callback`
4. No "Authorized JavaScript origins" are needed (the browser never talks to Google's API).
5. Put the values in `backend/.env` and **restart the backend**:
```
GOOGLE_CLIENT_ID=<...>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<...>
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/auth/google/callback
```
All three or none. Until all three are set the button is hidden and `/google/start` redirects to the login page with
`google_error=not_configured` (and logs an error). In production a partial set, or a non-https redirect URI, stops start-up.

## Accounts: an email match is never proof of ownership
- A returning Google user is matched **only by Google's stable `sub`** (`oauth_identities`). Email is never used to sign anyone in.
- `email_verified=true` means Google verified the address when *that Google account* was made. It does not prove the person controls
  the mailbox today (recycled or reassigned addresses, departed employees, expired domains), so Atla never links, signs in, or
  changes anything because an email matches. Google sign-in never removes a password, never signs other sessions out, and never
  changes an existing account's verification state.
- What happens when a Google account is **not yet linked**:

| Situation | Next step (`GET /google/pending` -> `kind`) |
|---|---|
| No Atla account for that email | `new`: name the business, `POST /google/complete` creates the account (owner) and signs in |
| Atla account **with a password** | `link_password`: enter that password, `POST /google/link/confirm` (5 wrong tries / 15 min per account, then locked out) |
| Atla account, **no password, email never verified** | `link_email`: `POST /google/link/email` mails a link to the account's address; opening it **in the same browser** (`POST /google/link/verify`) proves the mailbox and links. The token only works together with the Google sign-in cookie of the browser that asked |
| Atla account, no password, email already verified (it belongs to a different Google account) | `blocked`: refused. Sign in with the original Google account, or reset the password from the login page |

- **Already signed in:** Settings -> Security -> *Google sign-in* -> **Connect Google**. A session alone is never enough:
  - *Account with a password:* it must be re-entered (`POST /google/link/start`); the response carries the Google URL.
  - *Account without a password:* the response carries **no** URL. A one-time link is emailed to the account's own address
    (`google_link_verify=<token>`, 30 min, single use). It works only for the same user, in the same signed-in session, in the same
    browser that asked (a `atla_g_linkbind` cookie set when it asked), and only then (`POST /google/link/verify-start`) does the
    server create the Google state (single use, browser-bound). A copied link, a stolen session cookie, a new login session, or
    another browser all fail; a failed attempt does not burn the token. The email states that if the owner didn't ask, nothing is
    connected.
  - Either way the callback re-checks that the signed-in user **and the exact session** that started it are still the ones returning.
  The Google email may differ from the account's email. One Google account per user for now: the database enforces it (`uq_oauth_user_provider` on `(user_id, provider)`, next to `uq_oauth_provider_subject` on `(provider, subject)`), so two simultaneous requests with different Google accounts cannot both attach; the loser is told a different Google account is already connected.
- Linking sends the account owner a security-alert email ("Google sign-in was added"); nothing else about the account changes.
- A Google account already connected to one Atla user can never be connected to another.
- Existing organizations, memberships and roles are untouched; a linked sign-in lands in the same organization with the same role.
- Google-only users have no password: password login fails, and change-password / delete-account tell them to set one first with
  "Forgot your password?" (which proves the mailbox by email).
- Linking an existing account never changes its `email_verified_at`, password, or sessions. `email_verified_at` is set only when Google
  creates a brand-new account (Google vouched for the address at sign-up), and it is only ever used to *refuse* email-based linking.
- Someone who forgot the password of an existing account: use "Forgot your password?" first (this also signs out whoever else held
  the account), then log in and connect Google.

## Cookies
`atla_g_state`, `atla_g_pending` and `atla_g_linkbind`: HttpOnly, SameSite=Lax, `Secure` in production, path `/api/v1/auth/google`, 10 / 15 minutes.
The session cookie is the existing one (a new session id on every login).

## Notes
- A server access log records the callback URL (`code`, `state`). Both are single-use and the code is useless without the client
  secret and PKCE verifier, but keep access logs private.
- The security emails (link added, confirm-link) are in the allowed "security" category of the email policy; nothing operational is emailed.
- Run with several backend instances? State lives in Redis, so any instance can serve the callback.
