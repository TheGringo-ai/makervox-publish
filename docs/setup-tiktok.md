# Setting up a TikTok developer app for `makervox_publish`

You bring your own TikTok app. This document is the part that isn't code: registering
the app, choosing products and scopes, surviving the OAuth exact-match rules, and
understanding exactly what an *unaudited* app is and is not allowed to do.

Everything here was learned by doing it. Where a claim was verified by a failing API
call or a portal error, the error text is quoted verbatim, because **the symptom almost
never resembles the cause** and the error string is what you will actually be searching
for at 1am.

Read [The two tiers](#the-two-tiers-decide-this-before-you-touch-the-portal) first. It
decides how much of this document applies to you: Tier 1 is an afternoon, Tier 2 is a
project.

---

## The two tiers — decide this before you touch the portal

The Content Posting API has two modes, and the gap between them is the single most
important fact on this page.

| | **Tier 1 — Upload to inbox** | **Tier 2 — Direct Post** |
|---|---|---|
| Scope | `video.upload` | `video.publish` |
| Where the video lands | the account's TikTok **drafts/inbox** | the account's **public profile** |
| Caption set by API | ❌ no — the human types/pastes it | ✅ yes |
| Needs TikTok to audit your app | **no** | **yes** |
| Works the day you create the app | ✅ yes | ❌ no |
| Throughput | ~5 pending uploads, then it refuses | normal |

**Tier 1 is not "posting".** The upload succeeds, your logs say `ok`, TikTok returns
`SEND_TO_USER_INBOX` — and nothing is public. Somebody has to open the TikTok app on a
phone and tap Post, per video. In the pipeline this package was extracted from, **196
Tier-1 uploads over four weeks produced exactly zero public posts.** The logs were
green the whole time. If your success metric is "the API returned ok", you will believe
you have an automated TikTok channel for a month before you discover you have a
notification system.

**Tier 2 is gated on a human review of your posting UI**, not on a form. Budget days,
not an afternoon. Details in [Getting audited](#getting-audited-tier-2).

`makervox_publish` defaults to `mode = "inbox"` for exactly this reason: the honest default is
the one that works without permission.

---

## Before you start

You need, and cannot proceed without:

- A TikTok account you're willing to use as the **developer** account. It is separate
  from the brand accounts you will publish to.
- Each brand account's **login**. Not "access to the phone" — the actual username and
  password or the ability to complete a login in a desktop browser. Authorization
  happens in a browser and cannot be automated. This is the step that has parked more
  integrations than any API problem.
- A **privacy policy URL** and a **terms of service URL** that are live and publicly
  reachable. Required fields. Reviewers open them.
- Only for Tier 2: a **domain you control and can serve a static file from**, and an
  app icon at exactly **1024×1024**, ≤5 MB.

Notably you do **not** need a domain for Tier 1. `makervox_publish auth tiktok <account>` runs a
loopback listener on `127.0.0.1`, and TikTok accepts a loopback redirect URI. See
[Redirect URI](#redirect-uri-the-exact-match-rules).

---

## Step 1 — Create the app

1. Go to `https://developers.tiktok.com/` and log in with the developer account.
2. **Manage apps** → **Connect an app**.
3. Give it a name. This name is shown to users on the OAuth consent screen and to
   reviewers, so use the product name, not `test-app-2`.

You now have an app with **two sides**: **Sandbox** and **Production**, on adjacent
tabs. They are not two views of one thing. They have **different client keys**,
different configuration, and different verified-URL lists.

- **Sandbox** client keys start with `sbaw…`.
- **Production** client keys start with `aw…`.

> **Trap.** Using the production client key against a sandbox-authorized account (or
> vice versa) fails at the OAuth step with a generic `client_key` error that reads like
> a typo or a copy-paste problem. It is not. It is the wrong side of the app. Check the
> prefix of the key in your config before you debug anything else.

**One developer app per platform, not per brand.** A single TikTok app serves every
brand account you publish as; the per-brand thing is the *authorization*, not the app.
If you register one app per client you will do the entire audit once per client.

### Which side do I develop against?

**Sandbox.** And you cannot skip it: the portal requires an app that has never been
approved to demonstrate its integration in the sandbox environment. Guides that tell
you to swap in production keys before filming your demo have it exactly backwards — the
production key swap is an **after-approval** step.

---

## Step 2 — Add products

App page → **Add products**. Add exactly two:

- **Login Kit** — this is what gives you OAuth at all.
- **Content Posting API** — and inside it, tick **Direct Post** if you intend to pursue
  Tier 2. (Ticking it does not grant it; it declares intent, and it is what makes the
  `video.publish` scope selectable.)

**Do not add Share Kit or Webhooks.** They are not needed and they add configuration
surface that will mislead you — see the redirect-URI trap below. If they're already on
the app, remove them.

> **Trap.** **Scopes do not appear in the UI until the product that offers them is
> added.** If the scope list looks short or empty, you have not added the product yet.
> People conclude their account "isn't allowed" scopes; nothing is wrong.

---

## Step 3 — Scopes

Select these under the app's scope list:

| Scope | What it actually gives you | Needed for |
|---|---|---|
| `user.info.basic` | **identity only**: `open_id`, display name, avatar | everything (baseline) |
| `user.info.stats` | `follower_count`, `following_count`, `likes_count`, `video_count` | follower tracking |
| `video.list` | your own posts with view/like/comment/share counts | performance analysis |
| `video.upload` | Tier 1 — upload to the inbox | posting (unaudited) |
| `video.publish` | Tier 2 — Direct Post, and `creator_info` | posting publicly (audited) |
| `user.info.profile` | profile fields (bio, link, profile deep link) | optional |

`video.upload`, `video.list`, `user.info.basic` and `user.info.stats` are available
**immediately with no review**. `video.publish` is selectable immediately and will be
*granted in the token* immediately — but it does not do what you think until the app is
audited. That is the single most expensive misunderstanding on this platform; see
[The two lies](#the-two-lies-that-will-convince-you-tier-2-is-working).

### Two things about scopes that cost real time

**1. `user.info.basic` returns identity only. Asking it for `follower_count` is an
ERROR, not a null field.**

```
GET /v2/user/info/?fields=follower_count   with a user.info.basic token
→ error: scope_not_authorized
```

You need `user.info.stats`. This is not obvious from the field list, and the failure
mode is a hard error in the middle of a metrics job rather than a missing key you could
have defaulted.

**2. 🔑 SCOPES ARE FROZEN AT AUTHORIZATION. A token refresh does NOT widen them.**

Adding a scope in the developer portal, or changing `scopes` in `makervox-publish.toml`, does
**nothing** to any account that is already authorized. The token carries the scope set
that was requested at the moment the user clicked Authorize, forever. Every refresh
returns the same set.

The fix is a **browser re-authorization** of every account:

```sh
makervox_publish auth tiktok moonlit          # prints the URL; authorize in a browser
```

The symptom, if you forget, is `scope_not_authorized` on a call you "just enabled".
`makervox_publish` says this out loud in the error rather than making you infer it:

> TikTok denied video.list for 'moonlit': … Scopes are fixed at authorization —
> re-auth the account: `makervox_publish auth tiktok moonlit`

Corollary: authorize with the **full** scope set you might plausibly want, the first
time. Adding `user.info.stats` a year later means chasing down every brand account's
password again. In the source pipeline, one account carried
`user.info.basic,video.upload` and another carried five scopes, purely because they
were authorized on different dates — and the narrow one silently had no follower
history at all.

---

## Redirect URI — the exact-match rules

### Where it goes

**Login Kit → Configure for Web → Redirect URI.**

> **Trap that costs an afternoon.** There is *also* a URL field under **Webhooks**. It
> is not the OAuth redirect. Putting your callback there does nothing for OAuth, and
> the portal will POST-test it and show you a `405`, which reads like your server is
> broken. It isn't. Wrong field.

### The matching rules

The `redirect_uri` you send in the authorize URL and again in the token exchange must
be **byte-identical** to one registered on the app. Not equivalent — identical.

These are all *different* URIs as far as TikTok is concerned:

```
https://example.com/tiktok/callback
https://example.com/tiktok/callback/      ← trailing slash
http://example.com/tiktok/callback        ← scheme
https://www.example.com/tiktok/callback   ← host
```

And it must be sent **twice, identically**: once as a query parameter on the authorize
URL, once as a form field in the `POST /v2/oauth/token/` exchange. A mismatch between
those two is rejected at exchange time, after the user has already authorized, which
makes it look like the *code* is bad.

`makervox_publish` reads the value once from config and uses that same string in both places,
which removes the whole class of bug. It has **no default** — defaulting a redirect URI
to somebody else's domain is both an identity leak and a silent misconfiguration — so
an enabled TikTok platform with no `redirect_uri` raises `ConfigError` at load, not at
publish.

### You don't need a domain (for Tier 1)

Register a loopback URL on the app and use it:

```
http://127.0.0.1:8722/tiktok/callback
```

`makervox_publish auth tiktok <account>` starts a local listener on that host/port, catches the
`?code=`, and exchanges it. Nothing is exposed to the internet. Use `localhost` **or**
`127.0.0.1` consistently in both places — they are different strings, so they are
different URIs.

### 🔑 The trailing-slash trap, again, somewhere else

The same exact-match pedantry applies to **verified URL properties**, and there it
produces an error message that sends you off re-verifying a domain that was never
broken:

> **"This URL is not verified."**

Cause: the verified property is the prefix `https://example.com/` (with slash) and the
app's Web/Desktop URL field contains `https://example.com` (without). Adding the slash
clears the error instantly. Nothing was wrong with the verification. This cost an hour
and a near-miss re-verification of two working domains.

---

## Step 4 — Sandbox target users

A sandbox app can only act on accounts explicitly added to it.

App → **Sandbox** → **Target users** → add each brand account by username.

> **Trap.** Adding a target user is confirmed on the **mobile app** by the currently
> active account. If you have multiple accounts on one phone, the account that is
> *active* when you scan/approve is the one that gets added — regardless of which
> username you typed. Switch accounts first, verify after.

Symptom when this is wrong: OAuth completes, tokens are stored, and every API call
fails in a way that reads like broken auth. It isn't auth. It's an account that is not
on the allow-list.

---

## Step 5 — Credentials into config

Put the **names** of your secrets in the config file, and the values in environment
variables (or a keyring, or a secret manager). `makervox_publish` never wants a secret value in
a config file:

```sh
export TIKTOK_CLIENT_KEY='sbawEXAMPLEEXAMPLE00'          # fake
export TIKTOK_CLIENT_SECRET='EXAMPLEsecretEXAMPLEsecret' # fake
```

Both are on the app page under **App details** (Sandbox tab for the sandbox key,
Production tab for the production key — they are different values).

---

## Step 6 — Authorize each account

```sh
makervox_publish auth tiktok moonlit
# → opens/prints the authorize URL, catches the callback on 127.0.0.1:8722,
#   exchanges the code, stores the token set under the name "moonlit"
```

### 🚨 The cross-account trap — the worst silent failure on this platform

**TikTok issues a token for whichever account the BROWSER is signed into, not the one
named on your command line.**

Running `makervox_publish auth tiktok dailyverse` while your browser is still logged in as
`moonlit` stores **moonlit's** credentials under the key `dailyverse`. Nothing errors.
Then:

- every `dailyverse` video uploads into **moonlit's** drafts inbox;
- both brands drain **one** pending-share quota, so the ~5-draft cap fills twice as
  fast and you get `spam_risk_too_many_pending_share` in half the expected time;
- the person who owns `dailyverse` reports "my videos aren't arriving" and you go
  looking at the network layer.

This happened for weeks in the source pipeline before anyone noticed, and the reported
symptom (`spam_risk`) was two causal steps away from the real problem.

`makervox_publish` refuses the save when the returned account id already belongs to a different
local account name (`cli.auth_listener.refuse_identity_clash = true`, on by default):

> refusing to save: this authorization is for the SAME TikTok account already stored as
> 'moonlit' (open_id 6b2f9a3c1d0e…). You were signed into 'moonlit' in the browser. Log
> out of TikTok (or use a private window), sign in as the 'dailyverse' account, and run:
> `makervox_publish auth tiktok dailyverse`

**Procedure: use a private/incognito window per account.** Then verify the stored
`open_id` values differ. Do not skip the verify — this is exactly the failure that
looks fine.

---

## Step 7 — Post something (Tier 1)

```sh
makervox_publish publish tiktok moonlit ./reel.mp4
```

What happens, and what each part will bite you on:

1. **Size check.** Anything over ~60 MiB is transcoded first. TikTok returns `403` with
   `invalid request id` partway through the upload on oversized files — a **110 MB /
   44 s** reel reproduces it reliably. The error is not a size error and does not
   mention size.
2. **Init** → `POST /v2/post/publish/inbox/video/init/` returns an `upload_url` and a
   `publish_id`.
3. **Chunked PUT** to the `upload_url` with `Content-Range`.
4. **Confirm** → poll `/v2/post/publish/status/fetch/`.

### The chunk arithmetic is not what you'd write

TikTok's documented per-chunk ceiling is 64 MiB. The relationship it enforces is:

```
total_chunk_count == floor(video_size / chunk_size)      # NOT ceil
```

and **the last chunk absorbs the remainder** (so the final chunk is larger than
`chunk_size`, not smaller). Ceil-based math — the obvious implementation — returns
`invalid chunk count` on any file above the ceiling.

Separately: 64 MiB is the documented ceiling, **20 MiB is the empirically reliable
chunk size**. Both numbers are real. Don't "correct" one from the other; `makervox_publish`
ships 20 MiB (`upload.max_chunk_bytes`) with the reason in a comment for exactly this
reason.

### `206` is success

The chunk PUT returns `200`, `201` **or `206`**. Treating `206` as a failure produces a
retry that re-uploads the whole video, which is how you get duplicate drafts.

### Do not trust your own success flag

`ok: true` in your log means *the API accepted the upload*. It does not mean posted.
Poll for status and record what you actually got:

- `SEND_TO_USER_INBOX` — in the drafts inbox, **not public**, awaiting a human tap
- `PUBLISH_COMPLETE` — actually published (Tier 2 only)
- `PROCESSING_UPLOAD` / `PROCESSING_*` — accepted, still finalizing
- `FAILED`, `EXPIRED` — dead

`makervox_publish` polls (`publish.confirm`, 6 attempts × 4 s) and treats a still-`PROCESSING`
result as accepted rather than reporting a false failure — but it records the literal
status, so a later audit of your ledger can answer "how many of these were ever
public?" That question is the whole reason the field exists.

---

## The two lies that will convince you Tier 2 is working

Both of these were tested against a live account. Both are traps that produce a
confident, wrong conclusion.

### Lie 1 — `video.publish` appears in your granted scopes

```
scopes GRANTED : user.info.basic,user.info.stats,video.list,video.publish,video.upload
```

A scope in the token proves only that it was **requested** at authorization. It is not
evidence that the audit gate has lifted.

### Lie 2 — `creator_info` offers `PUBLIC_TO_EVERYONE`

```
creator_info.privacy_level_options
  = ['PUBLIC_TO_EVERYONE', 'MUTUAL_FOLLOW_FRIENDS', 'SELF_ONLY']
```

This looks exactly like permission. It is not.

🔑 **`privacy_level_options` describes what the CREATOR's account permits. It says
nothing about what your APP may do.** The creator's account is public, so
`PUBLIC_TO_EVERYONE` is listed. Your app is unaudited, so it may not use it. Two
different subjects, one field.

The actual attempt, on an app with the scope granted and the option offered:

```
POST /v2/post/publish/video/init/   privacy_level="PUBLIC_TO_EVERYONE"
→ {'error': {'code': 'unaudited_client_can_only_post_to_private_accounts', ...}}
```

Refused at **init**, before a single byte is uploaded.

### And the sting in that error message

`unaudited_client_can_only_post_to_private_accounts` means **the ACCOUNT must be
private** — not the post. Passing `privacy_level="SELF_ONLY"` does **not** satisfy it;
tested, refused identically. To film a working Direct Post demo you must temporarily
switch the brand account to private (**Settings → Privacy → Private account**), post,
then switch back. That hides the account's content while it's set, so it is a real
decision, not a config change.

A useful tell for which state you're in: **a private account's
`privacy_level_options` swaps `PUBLIC_TO_EVERYONE` for `FOLLOWER_OF_CREATOR`.**

---

## Getting audited (Tier 2)

### What TikTok actually audits: your UI, not your code

This is the part nobody writes down. TikTok's content-sharing guidelines require a
**human review screen shown before every post**, and the audit checks that screen. A
headless cron job cannot pass, however correct its API calls are.

The screen must show, per post:

- the creator's **nickname and avatar**, fetched live from `creator_info`;
- a **privacy dropdown with NO pre-selected default** — they check this specifically.
  Ship a single empty `<option value="">` with no `selected`, and refuse server-side
  when `privacy_level` is missing rather than defaulting to `SELF_ONLY`;
- **comment / duet / stitch** toggles, with the ones the creator has disabled **greyed
  out** per `creator_info`;
- **commercial content disclosure** (`brand_organic_toggle` / `brand_content_toggle`)
  with the legal declaration, and the sub-options disabled until the parent is ticked;
- a **video preview** and an **editable caption**;
- an explicit **Post** action (consent).

Also enforced by the API, so enforce it locally and fail fast: **branded content may
not be posted with `SELF_ONLY` visibility.** `makervox_publish` refuses that combination before
upload (`publish.refuse_branded_private`) rather than after you've pushed 40 MB.

`creator_info` must be **fetched live and never cached** — a creator can flip their
account to private at any moment, and a cached `PUBLIC_TO_EVERYONE` would post against
their current setting. `makervox_publish` defaults `creator_info_cache_s = 0`. Don't raise it.

⚠️ `creator_info` itself requires `video.publish`. With a `video.upload`-only token it
returns `scope_not_authorized` and the screen renders empty — so **scope + re-auth must
happen before you record the demo**, not after.

### ⛔ Never describe your integration as "unattended" or "fully automated"

The submission's justification text is read against the requirement being audited. A
justification that brags about full automation directly contradicts the per-post human
review, preview and consent that Direct Post demands. That framing alone can sink an
otherwise-correct submission.

Compliant framing: **"scheduled publishing with per-post human approval"** — the model
audited schedulers use. Note this is a genuine operating commitment, not wording:
rendering stays automatic, publishing becomes something a person approves.

### Verifying a URL property (a signature file, not DNS)

App header → **URL properties** → **Verify properties** → choose **URL prefix**.

- **URL prefix** = serve a signature file. No DNS involved.
- **Domain** = a DNS record. A *different* property kind, and **not** what the
  Web/Desktop URL field is checked against. Verifying the Domain type will not clear
  "This URL is not verified".

TikTok names a file. Serve it at the prefix root:

```
path:    https://example.com/tiktok<TOKEN>.txt
content: tiktok-developers-site-verification=<TOKEN>
```

Then click Verify. Verification is permanent and server-side.

> **Trap.** If your site has a catch-all route for `/{something}.txt` (an IndexNow key
> file handler is the usual culprit), it will shadow the verification file and return
> 404 for anything it doesn't recognise. Put the TikTok route **above** the catch-all.
> The same shadowing quietly broke `robots.txt` on the site where this was found.
>
> **Trap.** Sandbox and Production have **separate** verified-URL lists. A property
> verified on the sandbox does not exist on production. This produces "This URL is not
> verified" on a URL you are certain you verified — and you did, on the other side.

### The submission form is all-or-nothing

Production → fill in the form → **Submit for review**. Things that are true and
unpleasant:

1. **`Import` → "Import from Sandbox"** fills the entire production config in one
   click: category, description, ToS/privacy URLs, platforms, redirect URI, products
   and all scopes. **Do not hand-type those fields.** The confirmation warns it will
   replace the existing configuration — harmless on an empty draft.
2. **Save refuses while any validation error exists**, and **nothing partial persists**.
   > "Please correct all errors before you save changes, or submit changes for review."

   Reload and everything is gone — the import, the uploaded icon, the typed
   explanation, all client-side only. **This is one sitting, not a series of small
   steps.** Have the demo video finished *before* you open the form.
3. **The real errors are in a summary banner, not inline.** Reading only the inline
   error nodes will show you one error while the banner lists the two that are actually
   blocking. Two that hid there:
   - *"Redirect uri for Desktop is required"* — you ticked the Desktop platform without
     giving it a redirect URI. Untick Desktop if you're submitting Web.
   - *"Review description is invalid"* — it was **1211 characters against a 1000
     limit**. The cap is enforced and never surfaced inline. Count your characters.
4. **After clicking Save, look at the screen.** Do not pattern-match the DOM: a check
   for "saved changes" matches the substring inside "un**saved changes**", which is how
   a failed save gets reported as a success.

Fields the form wants: icon 1024×1024 ≤5 MB · category · description ≤120 chars ·
ToS URL · privacy policy URL · platforms · products · scopes · review explanation
≤1000 chars · **demo video mp4/mov ≤50 MB**.

Your privacy policy must list **every** platform you publish to. Reviewers open the
live URL. Submitting a policy that omits a platform your app posts to is a citable
rejection you'll discover a week later.

### The demo video

A bad demo fails a correct integration.

- **Film against the SANDBOX.** The portal requires a never-approved app to demonstrate
  there. Production key swap is post-approval.
- **The domain visible on screen must match the website URL you submit.** Which means
  the browser's URL bar has to be in frame.
- **Drive with a browser automation tool, film with the OS screen recorder.** The
  automation tool's own recorder captures the viewport only — no browser chrome, no URL
  bar, no domain. Region-crop the recording to the browser window; never record the
  full screen (your desktop is in that frame).
- **Basic auth freezes automation.** A native 401 dialog blocks every subsequent
  command. Pass HTTP credentials to the driver instead.
- **Native `<select>` popups ignore synthetic key events.** Set the value through the
  driver, not by sending arrow keys.
- **Hide the bookmarks bar and close extra tabs first.** A stranger is watching this.
- **The demo must show a COMPLETED post**, not a filled-in form. A recording that ends
  on a disabled Post button, an empty creator avatar, or a red "this account hasn't
  granted the posting permission yet" banner is not submittable — those are all
  symptoms of recording before the token carried `video.publish`.

---

## Tokens: lifetime, refresh, and what expires silently

| Token | Lifetime | Behaviour |
|---|---|---|
| `access_token` | **~24 h** (`expires_in` ≈ 86400) | refresh before expiry |
| `refresh_token` | **~365 days** (`refresh_expires_in`) | **rotates on every use** |

### 🔑 The refresh token rotates ON USE, and the loss is unrecoverable

A successful refresh **spends** the old refresh token and returns a new one. Two
processes refreshing the same account concurrently race destructively: the second
presents a token TikTok has already retired, fails, and that account now needs a
**manual browser re-auth**. You cannot recompute your way out of it — unlike a lost
cache entry, the value is simply gone, and recovering it means finding whoever has the
brand account's password.

This is a realistic failure, not a theoretical one: five scheduled publishes a day plus
a metrics job plus a health check is enough overlap.

`makervox_publish` ships three defences, all on by default:

1. **`lock_refresh`** — a file lock serializes refreshes on this host.
2. **`reread_inside_lock`** — re-read the token store *inside* the lock: if whoever
   held it first already refreshed, **adopt their token** instead of spending ours.
   This is what makes the lock worth having; a lock that only serializes still has the
   second caller spend a token that is now stale.
3. **`adopt_remote_on_reject`** — two machines cannot share a local lock, so on a
   rejected refresh, re-read the shared store once and adopt a token another host just
   rotated rather than failing the post. ⚠️ A genuinely dead token still raises —
   adopting must never hide a real need to re-auth.

The cache is invalidated before refreshing. Refreshing against a cached snapshot spends
a token that has already been retired, which is the same bug wearing a hat.

### What expires silently

- **The refresh token's ~365-day window.** Nothing warns you. The access token keeps
  refreshing happily for a year and then one day the refresh is rejected and the
  scheduler stops. Record `refresh_expires_in` when you store the token and alert on
  days-remaining; the code this was extracted from stored only the access token's
  expiry, so the annual re-auth would have arrived as an outage.
- **Revocation.** A user can remove your app under *Settings → Security → Apps and
  websites* at any time. Identical symptom, no notice.

### Where tokens should live

Default: a `0600` local JSON file. Correct for one machine.

If **anything else** will ever need them — a second host, a review console, a
container — put them in a shared secret store with the **local file as cache and
offline fallback**, and note two non-negotiables:

- **Write local first, then remote.** A freshly rotated refresh token is unrecoverable;
  it lands on disk before anything that can time out, and the mirror write never raises.
- **The offline fallback is load-bearing.** A scheduled publish must not die because a
  remote secret read timed out or the host's cloud credentials went stale.

💸 And a billing note if you use a cloud secret manager: many bill **per enabled version
per month**. A daily-rotating token unpruned is ~365 paid versions a year for one small
file. Keep a few for rollback, destroy the rest — and make pruning best-effort so it
can never fail a token write.

Also: the token is read on **every** API request. An uncached remote read adds ~1 s to
every call. Cache it in-process for ~60 s (`tokens.cache_ttl_s`), and invalidate before
refreshing.

---

## Failure modes: symptom → cause → fix

| Symptom | Cause | Fix |
|---|---|---|
| `spam_risk_too_many_pending_share` | Tier-1 drafts pile up unposted. The pending-share cap is **~5**. Nothing you do in code drains it. | A human publishes or deletes the pending drafts. Then keep daily uploads under ~5, or get audited. |
| Same, arriving twice as fast as expected | Two brand names share one TikTok identity — an auth done in a browser signed into the wrong account. Both drain one quota. | Compare stored `open_id`s. Re-auth the wrong one in a private window. |
| `scope_not_authorized` on a call you just enabled | Scopes are frozen at authorization; a refresh doesn't add them. | `makervox_publish auth tiktok <account>` — full browser re-auth. |
| `scope_not_authorized` from `creator_info` | `creator_info` needs `video.publish`, not `video.upload`. | Re-auth with `video.publish` in the scope string. |
| `user/info` errors when asking for `follower_count` | `user.info.basic` is identity-only. | Add `user.info.stats`, re-auth. |
| `unaudited_client_can_only_post_to_private_accounts` | Your app is not audited. The **account** must be private — `SELF_ONLY` does not satisfy it. | Tier 1, or get audited. To film a demo, temporarily make the account private. |
| `403 invalid request id` mid-upload | Video too large (~110 MB reproduces). Not a size error message. | Transcode below ~60 MiB before upload. |
| `invalid chunk count` | Ceil-based chunk arithmetic. | `total_chunk_count = floor(size/chunk_size)`, last chunk absorbs the remainder. |
| Chunk upload "fails" but the video appears anyway | `206` treated as an error, retry re-uploads → duplicate drafts. | Accept `200/201/206`. |
| OAuth error naming `client_key` | Sandbox key used against production or vice versa. | Check the prefix: `sbaw…` = sandbox, `aw…` = production. |
| Portal POSTs your callback and shows `405` | You put the redirect URI under **Webhooks**. | Move it to **Login Kit → Configure for Web**. |
| Token exchange rejects a fresh code | `redirect_uri` differs between the authorize URL and the exchange (often a trailing slash). | Send one identical string in both. |
| "This URL is not verified" on a domain you verified | Trailing-slash mismatch, or verified on the other side (sandbox vs production), or you verified the *Domain* property instead of the *URL prefix*. | Add the slash; verify on Production; use URL prefix. |
| Verification file 404s | A catch-all `.txt` route shadows it. | Route the TikTok file above the catch-all. |
| Scope list looks empty in the portal | Product not added yet. | Add Login Kit + Content Posting API. |
| OAuth succeeds, every call fails, looks like broken auth | Account not on the sandbox **target users** allow-list — or added as the wrong account because a different one was active on the phone. | Add/verify the target user. |
| Save in the submission form "succeeds", nothing persists | A validation error blocks the save; the real errors are in the summary banner, not inline. | Read the banner; fix all errors; save once. |
| Your logs say posted; nothing is public | Tier 1. `ok` means "accepted into a drafts inbox". | Record the literal status. `SEND_TO_USER_INBOX` ≠ published. |
| Publish job "stalls" for days with no error line | An uncaught `SystemExit`-class exception in one content type stops a rotation loop entirely. `except Exception` does not catch it. | Catch `BaseException` at the scheduler boundary, and alert on *nothing moved* rather than on error text. |

---

## Rate limits and caps that bite in production

**Posting**

- **~5 pending shares** in the Tier-1 inbox. This is the practical throughput limit of
  an unaudited app, and it is *structural*: at 5 uploads/day into a 5-deep queue, the
  queue fills in a day or two and every subsequent upload is refused. Splitting brands
  onto separate accounts buys days, not a fix.
- **Caption:** 2200 characters.
- **Video duration:** whatever `creator_info.max_video_post_duration_sec` says for that
  creator. Read it; don't assume.
- **File size:** transcode above ~60 MiB. Per-chunk ceiling 64 MiB documented, 20 MiB
  reliable.

**Reading**

- `video/list` caps `max_count` at **20** per page. Page with the cursor; cap your total
  so a large account can't loop forever.
- Metrics you get: `view_count`, `like_count`, `comment_count`, `share_count`,
  `duration`, `create_time`, `share_url`. There is no reach/impression figure and no
  traffic-source breakdown — those exist only in the in-app analytics export.

**Submission**

- App icon 1024×1024, ≤5 MB · description ≤120 chars · review explanation ≤**1000**
  chars (enforced, error text does not say why) · demo video ≤50 MB.

**Product realities that no code will fix** — worth knowing before you build a strategy
on this API:

- **The API cannot attach a trending sound.** That is in-app only, and on TikTok it is
  the single largest reach lever. An API-posted video forfeits it.
- **Converting a Creator account to a Business account restricts the sound library to
  the Commercial Music Library**, losing most trending sounds. If a portal modal offers
  to "level up your account", understand that trade before clicking.
- **Reach appears to be capped per day, not per post.** Posting 4× a day rather than 1×
  did not multiply reach in the data behind this package; it multiplied the pending-share
  problem.

---

## Config snippet

Every value below is a **fake placeholder**. Secrets are named, never inlined.

```toml
# makervox-publish.toml
version = 1

[platforms.tiktok]
enabled = true
client_key_credential   = "TIKTOK_CLIENT_KEY"      # name of an env var, not the key
client_secret_credential = "TIKTOK_CLIENT_SECRET"

# REQUIRED, no default. Must byte-match a redirect URI registered on YOUR app
# under Login Kit -> Configure for Web. The loopback listener means you do not
# need to own a domain. Mind the trailing slash — or its absence.
redirect_uri = "http://127.0.0.1:8722/tiktok/callback"

# FROZEN AT AUTHORIZATION. Editing this line does nothing until every account
# re-authorizes in a browser. Ask for everything you might want, once.
scopes = "user.info.basic,user.info.stats,video.upload,video.publish,video.list"

[platforms.tiktok.tokens]
store          = "tiktok_tokens"
cache_ttl_s    = 60          # the token is read on EVERY request
expiry_skew_s  = 60
refresh_lock   = "tiktok-refresh"
lock_refresh          = true # 1. serialize refreshes on this host
reread_inside_lock    = true # 2. adopt the lock holder's token, don't spend ours
adopt_remote_on_reject = true # 3. adopt a token another host just rotated

[platforms.tiktok.upload]
max_chunk_bytes   = 20971520   # 20 MiB empirical; 64 MiB is the documented ceiling
max_video_bytes   = 62914560   # 60 MiB — above this, 403 "invalid request id"
accepted_statuses = [200, 201, 206]   # 206 IS success
caption_max_chars = 2200

[platforms.tiktok.publish]
mode = "inbox"                 # "direct" requires an AUDITED app
creator_info_cache_s   = 0     # never cache: a creator can go private any moment
refuse_branded_private = true  # branded content + SELF_ONLY is refused by the API
auto_publish_disabled_accounts = []   # ships empty; no account names in this package

[platforms.tiktok.publish.confirm]
attempts = 6
delay_s  = 4
processing_is_accepted = true

[platforms.tiktok.metrics]
list_page_size = 20            # the API caps video/list max_count at 20
max_videos     = 400

[platforms.tiktok.metrics.follower_log]
enabled = false                # opt-in; writes nothing by default
path    = "~/.local/state/makervox_publish/tiktok_followers.csv"
one_row_per_day = true         # a job that runs twice must not fake a datapoint

# --- accounts -------------------------------------------------------------
[accounts.moonlit]
display_name = "Moonlit Example"
platforms    = ["tiktok"]

[accounts.moonlit.tiktok]
# Per-account scope override. Still frozen at authorization.
scopes = "user.info.basic,user.info.stats,video.upload,video.publish,video.list"

[accounts.dailyverse]
display_name = "Daily Verse Example"
platforms    = ["tiktok"]

[accounts.dailyverse.tiktok]
scopes       = "user.info.basic,video.upload"
auto_publish = false           # this one uploads to the inbox; a human taps publish

# --- token store ----------------------------------------------------------
[token_stores.tiktok_tokens]
impl = "makervox_publish.state.token_store:FileTokenStore"

[token_stores.tiktok_tokens.options]
path         = "~/.local/state/makervox_publish/tiktok_tokens.json"
atomic_write = true
lock         = "tiktok-tokens"

# --- CLI ------------------------------------------------------------------
[cli.auth_listener]
host = "127.0.0.1"
port = 8722
refuse_identity_clash = true   # refuse to save an auth for an account already stored
```

Secrets live outside this file:

```sh
export TIKTOK_CLIENT_KEY='sbawEXAMPLEEXAMPLE00'           # fake, sandbox-prefixed
export TIKTOK_CLIENT_SECRET='EXAMPLEsecretEXAMPLEsecret'  # fake
```

---

## An honest summary

- **Tier 1 works today, costs an afternoon, and is not automated posting.** It is a
  delivery mechanism to a human's drafts inbox with a ~5-deep queue. If that's enough,
  stop here — you're done, and everything after this line is optional.
- **Tier 2 is a project.** You must build a compliant human-review posting UI, verify a
  URL property (watch the trailing slash), temporarily make an account private to film
  a working demo, and complete an all-or-nothing form whose real errors hide in a
  banner. Then wait for a human review.
- **The gate is not technical.** Anyone can write the upload code — the API is
  straightforward and this package implements all of it. The moat is the review.
  Plan for it, or design your product so Tier 1 is genuinely sufficient.
