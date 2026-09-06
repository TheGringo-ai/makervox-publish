# Instagram: registering the developer app

**Scope of this document.** Everything between "I want to post a Reel from a script" and "a Reel
appeared on the account." The posting code is thirty lines. The registration is the part that
takes a weekend, and almost none of it is in the official docs in the order you need it.

Every value in this document is a **fake placeholder**. Substitute your own.

> Written against Graph API `v21.0`. Meta renames console sections roughly every other quarter.
> Where a click-path has moved, the *shape* of the step is still right: find the thing that does
> what the step describes. Every claim here that could rot is paired with a `curl` that tells you
> the truth from the API rather than from a screenshot.

---

## 0. The one decision that determines everything else

There are **two completely different ways** to publish to Instagram, with different apps,
different scopes, different token lifetimes, and different failure text. Choosing wrong is the
single most expensive mistake in this integration, because you find out three hours in.

| | **Path A — Facebook Page (recommended)** | **Path B — Instagram Login (legacy here)** |
|---|---|---|
| Host | `graph.facebook.com/{ig-user-id}` | `graph.instagram.com/{ig-user-id}` |
| Token | the **Page** access token you already have for Facebook | a separate Instagram user token |
| Scopes | `instagram_basic`, `instagram_content_publish` (+ Page scopes) | `instagram_business_basic`, `instagram_business_content_publish` |
| Token life | **non-expiring** (Page token from a long-lived user token) | 60 days, refreshable, **dies silently** |
| Prereq | IG account linked to a Facebook Page | IG Professional account, no Page needed |
| App type | Meta app with Facebook Login + Instagram use cases | Meta app with "Instagram API with Instagram Login" |

**Take Path A if you are already posting to a Facebook Page.** You get Instagram publishing for
zero additional console work — the same Page token carries it, once the two Instagram permissions
are on the token. This is not a theoretical preference: the original integration this package was
extracted from burned ~90 minutes down the Path B maze (App Roles → Test Users → Graph API
Explorer → "FB Login for Business" settings, all dead ends, all producing
**`Insufficient Developer Role`**) and then discovered the Page token it already had would do the
job unchanged.

Take Path B only if the account genuinely has no Facebook Page and you will not create one.

`makervox_publish` implements both and prefers A:

```toml
[platforms.instagram]
auth_path = "facebook_page"       # facebook_page | instagram_login
```

It resolves the IG user id live from the Page's `instagram_business_account` field, falls back to
a stored Instagram-login token, and returns a **safe no-op** — not an exception — when neither is
wired. Wiring this in before you have done the console work is quiet, not fatal.

---

## 1. Prerequisites you cannot do from code

Do these first. Each one, skipped, produces a failure that looks like broken auth.

### 1.1 The Instagram account must be **Professional**

Instagram app → Settings → Account type and tools → **Switch to professional account** → Business
or Creator. A personal account is invisible to the publishing API entirely.

### 1.2 The Instagram account must be **linked to the Facebook Page** (Path A)

**This link has no API.** You cannot create it, and you cannot detect its absence except by the
Page returning no `instagram_business_account`. It is a Meta-UI action:

Meta Business Suite → **Settings → Accounts → Instagram accounts** → *Add* → connect the IG
account → then attach it to the Page. (Also reachable from the Page itself: Page settings →
*Linked accounts* → Instagram.)

> **Real incident.** A second brand's Instagram auto-post shipped with **zero code changes and
> zero extra Meta console work** — the publisher had been silently no-op'ing for weeks purely
> because nobody had made this one UI link. The single missing piece was a checkbox in Business
> Suite.

**Verify it, don't trust the UI:**

```sh
curl -s "https://graph.facebook.com/v21.0/100000000000001\
?fields=name,instagram_business_account,connected_instagram_account\
&access_token=$PAGE_TOKEN"
```

You want a non-empty `instagram_business_account`.

🔑 **`connected_instagram_account` is not the same field and will not work.** A Page can have a
"connected" Instagram account (the older, lighter link used for ads and comment sync) while
`instagram_business_account` is absent. The publishing API only accepts the latter. Symptom: your
resolver returns `None`, your code reports "no IG for this account", and the Page settings screen
shows Instagram as connected. Fix: redo the link from **Business Suite**, not from the Instagram
app's "Share to Facebook" toggle.

### 1.3 You must be an admin of both

The token you mint has to belong to a person with a full admin role on the Page and on the
Instagram account. Partial roles (Editor, Moderator) return the Page in `/me/accounts` but produce
`(#200) Permissions error` at publish time.

---

## 2. Register the app — click by click

**One developer app per PLATFORM, not per brand.** One Meta app serves every Page and every
Instagram account you will ever publish to; the per-brand thing is the *account*, which authorizes
that one app. Getting this backwards means repeating every review below, per client, forever.

### 2.1 Create it

1. <https://developers.facebook.com/apps> → **Create app**.
2. App name — this string is shown to anyone who authorizes, so use a product name, not
   `test-app-3`.
3. **App type: Business.** (Meta's newer flow asks "What do you want your app to do?" instead —
   choose the option that mentions managing business assets / Pages. The result is the same
   Business-type app.)
4. Attach it to a **Business portfolio**. If you have several, pick deliberately — moving an app
   between portfolios later is possible but re-triggers verification state, and it is how a small
   estate ends up with three Pages spread across three portfolios and no single verified entity.

### 2.2 Add the products / use cases

In the app dashboard:

- Add **Facebook Login** (needed to mint a user token that yields Page tokens).
- Under **Use cases**, add **"Manage a Page"** *(newer consoles: "Manage everything on your Page"
  / "Instagram publishing")*.

### 2.3 🔑 The permission you need isn't in the picker until you enable it in a Use Case

**The gotcha that costs an hour on a Business-type app:** the Graph API Explorer's permission
dropdown will not offer `pages_manage_posts` — or the Instagram permissions — until they are
enabled inside a Use Case. The dropdown is not a list of what exists; it is a list of what your app
has switched on.

Path: **App dashboard → Use cases → [your use case] → Customize → Permissions** → click **Add**
next to each one.

Enable, at minimum:

| Permission | Why |
|---|---|
| `pages_show_list` | `/me/accounts` returns your Pages at all |
| `pages_read_engagement` | read Page fields, including `instagram_business_account` |
| `pages_manage_posts` | posting to the Page (needed for the FB side; harmless here) |
| **`instagram_basic`** | resolve and read the linked IG account |
| **`instagram_content_publish`** | **create and publish media containers — this is the one** |

Optional, add now if you will ever want them (adding later means re-minting every token):

| Permission | Why |
|---|---|
| `pages_manage_engagement` | posting the first comment under a Reel |
| `pages_manage_metadata` | setting the Page's about/cover |
| `read_insights` | reel plays / reach |

For **Path B** the scope names are different and are *not* interchangeable:
`instagram_business_basic`, `instagram_business_content_publish`. Feeding Path B scope names to a
Facebook-Page token, or vice versa, is a large fraction of the confusion in this integration.
`instagram_business_*` scopes are **only** offered under the Instagram-Login product — if you go
hunting for them in the Facebook Graph API Explorer you will not find them, and that dead end is
exactly what produced the `Insufficient Developer Role` wild goose chase.

---

## 3. Redirect URIs — and the exact-match trap

You need a redirect URI only if you are running a real OAuth flow. **If you are self-hosting for
your own accounts, you can skip OAuth entirely** and mint the token in the Graph API Explorer
(§4.1). That is the fastest correct path and it needs no domain at all.

If you do run OAuth: **App dashboard → Facebook Login → Settings → Valid OAuth Redirect URIs.**

Rules, all of them enforced, none of them forgiving:

1. **Exact string match. Strict mode is always on** and cannot be turned off. There are no
   wildcards, no prefix matching, no path globbing.
2. 🔑 **A trailing slash is a different URI.** `https://publish.example.com/oauth/instagram/callback`
   and `https://publish.example.com/oauth/instagram/callback/` are two unrelated strings to Meta.
   Register the one your framework actually redirects to — and note that several web frameworks
   *append* a trailing slash on their own via a 301, so the URI you typed into your code is not
   necessarily the one the browser lands on. This has cost real hours. Symptom:
   `URL Blocked: This redirect failed because the redirect URI is not whitelisted in the app's
   Client OAuth Settings`, with a URI in the error that looks identical to the one you registered.
   Copy both into a diff, character by character.
3. **HTTPS only**, with a valid certificate. Not self-signed.
4. **No query parameters.** Put your state in the `state` parameter, which is matched separately.
   A redirect URI carrying `?brand=moonlit` is rejected.
5. **Scheme, host and path are case-sensitive in practice** for the path segment. Register the
   exact casing.
6. **`http://localhost` is a development-mode-only escape hatch.** Meta accepts loopback redirects
   while the app is in Development mode; when you flip to Live it stops working. Do not build your
   only auth path on it — but it is genuinely useful for the first token, and it is why `makervox_publish`
   ships a loopback listener:

   ```toml
   [cli.auth_listener]
   host = "127.0.0.1"
   port = 8722
   ```

7. **The App Domain field is separate** (Settings → Basic → App Domains) and must contain the
   registered domain of your redirect URI. Mismatch there produces the same "URL Blocked" text as
   the redirect list itself, which is why people fix the wrong field for twenty minutes.

Also under Settings → Basic, and required before you can go Live: **Privacy Policy URL** and
**Terms of Service URL**, both publicly reachable. Meta fetches them. A 404 or a page behind auth
blocks the submission.

---

## 4. Getting a token that actually carries the scopes

### 4.1 The fast path (own accounts, no OAuth)

1. <https://developers.facebook.com/tools/explorer/>
2. **Meta App:** select your app. (Not "Graph API Explorer", which is Meta's own app and will
   mint you a useless token.)
3. **User or Page:** *User Token*.
4. **Permissions:** tick every permission from §2.3.
5. **Generate Access Token** → complete the dialog → **grant access to the Page and the Instagram
   account** in the picker. The picker defaults to "all", but if you deselect and re-select, an
   asset left out is silently missing from the token.

🔑 **A token is a snapshot.** Adding a permission to the app does **not** upgrade tokens that
already exist. Every time you change the permission set, you must generate a **fresh** token and
re-run your connect step. Symptom of forgetting: you enabled `instagram_content_publish`, the
console shows it enabled, and the API still returns
`(#10) Application does not have permission for this action`.

### 4.2 Turn it into something durable

The short-lived user token from the Explorer lasts ~1–2 hours. The chain is:

```
short-lived user token
  --(GET /oauth/access_token?grant_type=fb_exchange_token)-->  long-lived user token (60 days)
  --(GET /me/accounts)-->                                       PAGE tokens (non-expiring)
```

```sh
# 1. short -> long-lived user token (60 days)
curl -s "https://graph.facebook.com/v21.0/oauth/access_token\
?grant_type=fb_exchange_token\
&client_id=1234567890123456\
&client_secret=$META_APP_SECRET\
&fb_exchange_token=$SHORT_TOKEN"

# 2. long-lived user token -> per-Page tokens
curl -s "https://graph.facebook.com/v21.0/me/accounts\
?fields=name,id,access_token&access_token=$LONG_TOKEN"
```

🔑 **The Page token is only non-expiring if you derived it from a *long-lived* user token.**
Derive it from the short-lived one and you get a Page token that dies in an hour, with no
indication at mint time that anything is different. Do the exchange first, always.

### 4.3 Verify what you actually hold

Never trust the console. Ask the token what it is:

```sh
curl -s "https://graph.facebook.com/v21.0/debug_token\
?input_token=$PAGE_TOKEN&access_token=$META_APP_ID|$META_APP_SECRET"
```

Check three things in the response:

- `"expires_at": 0` → non-expiring. **Anything else is a time bomb.**
- `scopes` contains `instagram_basic` **and** `instagram_content_publish`.
- `type` is `PAGE`, and `profile_id` is the Page you meant.

---

## 5. What needs review, and what works right now

This is the part people get wrong in both directions — either building a review submission they
did not need, or shipping something that dies the moment a second person uses it.

**"Ready for testing" / Standard Access is enough to publish to your own accounts.**
With the app in **Development mode**, permissions at Standard Access work for anyone with a role
on the app (Admin, Developer, Tester) acting on Pages and Instagram accounts **they administer**.
No App Review. No Business Verification. No demo video. A self-hoster posting to their own brand
accounts can stop here, permanently.

**You need Advanced Access — meaning App Review, and Business Verification first — when:**

- anyone who is *not* on your app's role list authorizes it, i.e. the moment you have users;
- you flip the app to **Live** mode and expect third parties to work.

| Permission | Own accounts, Dev mode | Third-party accounts |
|---|---|---|
| `pages_show_list` | works now | Advanced Access |
| `pages_read_engagement` | works now | Advanced Access |
| `pages_manage_posts` | works now | App Review |
| `instagram_basic` | works now | App Review |
| `instagram_content_publish` | works now | App Review |

**Business Verification gates App Review, and it needs a legal entity** — registered business name,
address, and a document or phone/domain check that matches public records. Plan for this as a
weeks-long item involving paperwork, not an afternoon. If you are an individual with no registered
entity, Advanced Access is effectively closed to you, and staying in Development mode with your own
accounts is not a workaround — it is the correct architecture for self-hosting.

App Review, when you do it, wants a **screencast demonstrating the permission in use**, with your
app's domain visible on screen. See the demo-video notes in the main guide: record with the OS, not
with a headless browser's own recorder, because reviewers need to see the URL bar.

---

## 6. Media staging — Instagram will not accept your bytes

🔑 **The Instagram publishing API PULLS media by URL. There is no byte upload.** You hand it
`video_url` / `image_url`, and Meta's fetcher goes and gets it. This one design decision is the
source of most of the operational pain below.

Requirements for that URL:

- publicly readable, **no authentication**, no redirect to a login;
- served with a correct `Content-Type` (`video/mp4`);
- reachable from Meta's infrastructure (not `localhost`, not a VPN-only host, not an IP allowlist).

🔑 **Facebook CDN `source` URLs do NOT work as Instagram ingestion URLs.** The obvious shortcut —
post the Reel to the Page, read back its `source` URL, hand that to Instagram — fails. Symptom: the
container reaches `status_code: ERROR` in about **eight seconds**, with no useful message on the
`status_code` field alone. Don't spend an evening on it; it is a closed door.

### 6.1 Object storage (what `makervox_publish` ships)

Stage the file, publish, delete:

```toml
[platforms.instagram.staging]
impl = "makervox_publish.platforms.meta.staging.gcs:GcsStager"

[platforms.instagram.staging.options]
bucket = "example-media-staging"   # REQUIRED. There is no default.
prefix = "makervox_publish-temp"
public_mode = "object_acl"         # object_acl | signed_url
chunk_bytes = 8388608              # 8 MiB — must be a multiple of 256 KiB
upload_timeout_s = 300             # PER CHUNK, not per file
delete_after_publish = true
```

Shipped alternatives: `…staging.s3:S3Stager` (S3/R2, presigned) and
`…staging.http_dir:StaticDirStager` (drop the file into any web root you already serve).

### 6.2 🔑 Uniform bucket-level access breaks per-object public ACLs

**Symptom:** the upload succeeds and then making the object public raises a 403 —
*"Cannot get legacy ACL for an object when uniform bucket-level access is enabled."*

**Cause:** modern GCS buckets are created with **uniform bucket-level access ON by default**, which
disables per-object ACLs entirely. The "make just this one object public" trick requires it OFF.

**Fix, pick one:**
- create the staging bucket with uniform access **disabled**, and keep it dedicated to staging so
  nothing sensitive can be made public by accident; or
- set `public_mode = "signed_url"` and hand Instagram a time-limited signed URL instead. This works
  — Meta's fetcher does not care about query parameters — but note that anything sniffing your
  media type from the URL must strip the query string first, which the publisher does.

`makervox_publish` detects this precondition and raises a named `StagingPreconditionError` explaining it,
rather than surfacing a raw 403 you have to decode.

### 6.3 🔑 The single-shot upload timeout that ate three posts

**Symptom:** `RetryError: Timeout of 120.0s exceeded` part-way through the upload. Intermittent.
Correlates with file size and with time of day.

**Cause:** without an explicit `chunk_size`, the GCS client does a **single-shot** upload, and the
client's default 120-second timeout then covers the **entire transfer**. A ~30–40 MB Reel on a
domestic uplink simply does not finish in 120 seconds. It is not a network fault and no amount of
retrying helps, because each retry starts over.

**Fix:** set `chunk_bytes`. That switches the client to a **resumable** upload where the timeout
applies **per chunk**, so a slow link *stretches* the upload instead of failing it.

**Cost when unfixed:** three silently dropped Instagram posts in one month, plus a separate
brand hitting the identical wall on 40 MB files weeks earlier — the same bug, found twice, because
the first fix was applied to a caller instead of the shared stager.

**Retry safety:** retrying the staged upload is safe *specifically because* the object name is
deterministic and the object is deleted after publishing — a retried chunk overwrites rather than
creating a second object. Do not generalise that to the publish call itself (§7.4).

### 6.4 The cleanup nobody notices failing

If deletion fails after publishing, you have left a **publicly readable** object in a bucket. Log
that at ERROR with the full URI. Swallowing it silently is how a staging bucket accumulates
months of public video.

---

## 7. The publish flow, and every way it breaks

Three calls, identical on both auth paths:

```
POST /{ig-user-id}/media          -> creation_id      (create the container)
GET  /{creation_id}?fields=status_code,status         (poll until FINISHED)
POST /{ig-user-id}/media_publish  -> media id         (publish it)
```

For a Reel, `media_type=REELS` and `video_url=…`. For an image, `image_url=…` and no media type.

### 7.1 Failure table

| Symptom | Cause | Fix |
|---|---|---|
| `(#10) Application does not have permission for this action` | token predates the permission, or `instagram_content_publish` was never enabled in a Use Case | enable in Use Case (§2.3), **re-mint the token** (§4.1) |
| Resolver returns nothing; "no IG for this account" | Page has no `instagram_business_account` | do the Business Suite link (§1.2); check you are not reading `connected_instagram_account` |
| `Insufficient Developer Role` | you are on Path B with the app in Development mode and the IG account has no app role | switch to Path A. Seriously. Or add the account under App Roles → Testers *and* accept the invite from inside that account |
| Container reaches `ERROR` in ~8s | ingestion URL is not fetchable — auth required, wrong content type, or a Facebook CDN URL | stage to real public object storage (§6) |
| Container reaches `ERROR` after ~30–60s | the media itself is rejected — codec, duration, aspect ratio, or size | fetch `fields=status_code,status` and read the actual message (§7.2) |
| `reel processing timed out` | your poll budget expired; the container may still be processing | raise `poll_attempts`; **do not re-create the container** (§7.4) |
| `(#190)` / `OAuthException` code 190 | token invalidated: password change, app removed, permission revoked, or app secret rotated | full re-auth. `debug_token` tells you which |
| `Media ID is not available` at publish | you published before `FINISHED` | poll properly; the status gate is not optional for video |
| `(#200) Permissions error` | the user behind the token is not a full admin of the Page/IG account | fix the role, re-mint |
| `The user is not an Instagram Business Account` | the IG account is still Personal | switch it to Professional (§1.1) |
| Grid renders as solid black tiles | see §8 — this is the big one | send `thumb_offset` |

### 7.2 🔑 Poll `status`, not just `status_code`

`status_code` is one of `IN_PROGRESS` / `FINISHED` / `ERROR` / `EXPIRED`. On `ERROR` it tells you
nothing about *why*, which is how "reel processing error" becomes an unfalsifiable guess.

The sibling field **`status`** carries the human-readable reason. Ask for both:

```sh
curl -s "https://graph.facebook.com/v21.0/$CREATION_ID\
?fields=status_code,status&access_token=$PAGE_TOKEN"
```

Log the `status` string on every failure. It is the difference between "Instagram rejected it" and
"the audio track is not AAC".

### 7.3 Poll budget

The reference implementation polls **30 times at 6-second intervals = 3 minutes**, which covers a
typical 60-second vertical Reel comfortably. Long or large files need more:

```toml
[platforms.instagram.publish]
poll_attempts = 30
poll_interval_s = 6
```

### 7.4 🔑 A timeout is not a failure — do not retry the whole publish

If the poll budget expires, the container is *still processing on Meta's side*. Re-running your
publish function from the top creates a **second container** from a second upload, and if the first
one finishes and gets published by a retry, you have posted twice.

The rules that keep this safe:

- **Retry inside the operation, reusing the same `creation_id`** — never re-run the create step.
- **Media containers expire after ~24 hours.** An abandoned container is harmless; it disappears.
  Leaking one costs nothing. Publishing one twice costs reach.
- A per-account **lock serialises callers; it does not deduplicate them.** Caller #2 waits for
  caller #1 and then cheerfully publishes the same Reel. The delivery re-check must happen
  **inside** the lock — a check made before acquiring it is a TOCTOU whose snapshot goes stale
  while it blocks.
- **A filename is not an identity.** A scheduler that renders the same content type twice in one
  day overwrites the first file, so two genuinely different videos share one name. Dedupe on
  `(account, date, key, slot, content hash)`.

This exact class of bug — retry re-minting a media id after the platform had already committed —
produced tight clusters of duplicate posts on the sibling Facebook publisher, at exactly `tries=3`,
and duplicate uploads get **reach-suppressed**, so a duplicate does not merely waste an upload: it
burns the slot.

---

## 8. 🔑 The black grid

**Symptom:** every post on the profile renders as a solid black tile. The account looks broken or
empty. The Reels themselves play fine.

**Cause, measured rather than guessed:** Instagram defaults a Reel's cover to **frame 0**. Video
that fades in from black has a frame 0 whose mean luminance is literally **0.0**. Sampling a real
Reel at 64×64 greyscale: `t=0 → 0.0`, `t=0.5 → 63.2`. Nothing is wrong with the video and nothing
is wrong with the API call. The default is just the worst possible frame for this style of content.

**Fix:** scan the opening seconds, find the first frame that is clearly past the fade, and send its
offset in **milliseconds** as `thumb_offset` on the REELS container.

```toml
[media.cover]
enabled = true
impl = "makervox_publish.media.cover:BrightnessScanCoverPicker"

[media.cover.options]
scan_until_s = 6.0
step_s = 0.25
brightness_floor = 10.0
min_offset_s = 0.75      # never pick a cover before this
fallback_floor = 5.0     # legitimately dark art still beats pure black
take_first_qualifying = true   # match the opening beat, not a random bright flash
```

Two details that matter:

- **The fallback floor is load-bearing.** Some content is legitimately dark and never clears the
  main threshold (a real example peaked at 17.6 across its whole opening). Returning "no offset"
  there sends no `thumb_offset` and Instagram falls back to frame 0 — pure black — which is the
  exact bug the picker exists to prevent. Anything clearly above black beats the default.
- **A cover is never worth failing a post over.** If `ffmpeg` is missing or the scan finds nothing,
  omit `thumb_offset` and publish anyway. Log the swallow with its reason at WARNING; a silent
  swallow is how a missing binary becomes an unexplained regression.

Reusable diagnostic for "why is my thumbnail black":

```sh
ffmpeg -ss 0.5 -i clip.mp4 -frames:v 1 -vf "scale=64:64,format=gray" -f rawvideo - \
  | python3 -c "import sys;d=sys.stdin.buffer.read();print(sum(d)/len(d))"
```

🚨 **This fixes new posts only.** The Graph API **cannot change the cover of an already-published
Instagram Reel.** Existing posts must be fixed by hand in the app (Edit → Cover), or deleted and
reposted. A backlog of 45 black tiles is 45 manual edits.

> Note the asymmetry with Facebook, which *does* accept a new preferred thumbnail on an
> already-published video (`POST /{video-id}/thumbnails` with `is_preferred=true`), letting you
> repair history in place with no deletes and no lost engagement. Instagram gives you one shot, at
> publish time. Get it right the first time.

---

## 9. Rate limits and caps that bite in production

| Limit | Value | What happens when you hit it |
|---|---|---|
| **Content publishing** | **50 published posts per rolling 24 hours**, per IG account | publish calls are rejected. Check it *before* you publish, don't discover it |
| Caption length | 2,200 characters | rejected outright |
| Hashtags | 30 per post | rejected outright |
| @-mentions | 20 per post | rejected outright |
| Reel duration | ~3 s minimum, up to 15 minutes | container `ERROR` |
| File size | 1 GB practical ceiling for video | container `ERROR`, or an ingestion timeout first |
| Image format | JPEG, ~8 MB | container `ERROR` |
| Graph API calls | app-level throttling, reported in the `X-App-Usage` / `X-Business-Use-Case-Usage` headers | HTTP 429 / error 4 or 17 |

Query the publishing budget rather than guessing:

```sh
curl -s "https://graph.facebook.com/v21.0/17000000000000000/content_publishing_limit\
?fields=config,quota_usage&access_token=$PAGE_TOKEN"
```

**Never retry an API *rejection*.** Retry transport failures — connect/read timeouts, DNS, resets —
and nothing else. Retrying a rate-limit or spam rejection makes it worse:

```toml
[http.retry]
attempts = 3
backoff_s = 3
retry_on = ["transport"]     # not 5xx, not 429
```

Practical note: at four posts a day the 50/24h cap is not the constraint you will meet. The
constraint you will meet is the poll budget on large files and your own upstream bandwidth.

---

## 10. Token lifetime, and what expires silently

### Path A (Facebook Page token)

**Non-expiring — but not un-invalidatable.** `debug_token` reports `expires_at: 0`. It still dies
when:

- the admin changes their Facebook password;
- the admin removes the app, or revokes a permission;
- **you rotate the app secret** (this invalidates every token the app ever issued — a genuinely
  surprising blast radius);
- Meta invalidates it for a policy or security reason with no notification of any kind.

Every one of these presents identically: error **190**, mid-schedule, at 3 a.m. There is no
warning email. **Monitor it** — a weekly `debug_token` check that alerts on `is_valid: false` is
twenty lines and it is the difference between noticing in a day and noticing in a month.

### Path B (Instagram Login token)

- Short-lived token → exchange with the app secret for a **60-day long-lived token**.
- Refresh with `GET /refresh_access_token?grant_type=ig_refresh_token` — **no app secret needed**
  for the refresh, only for the initial exchange.
- 🔑 **The token must be at least 24 hours old before it can be refreshed**, and the account must
  have been active within the 60 days. Refresh too early and the call is rejected.
- 🔑 **Past 60 days it is simply dead.** There is no grace period and no recovery path except a
  full browser re-authorization by a human. A scheduler that runs unattended will just stop, and
  the only symptom is an absence of posts.

Refresh well before the edge:

```toml
[platforms.instagram.tokens]
store = "meta_tokens"        # SAME store as facebook — one file, one lock
refresh_after_s = 4320000    # 50 days; the token lives 60
```

🔑 **Facebook and Instagram must share one token store *and* one lock.** They live in the same
file; two publishers doing an unlocked read-modify-write on it will lose a freshly refreshed token
— the classic silent row-dropping race. One store, one lock, atomic write (temp file +
`os.replace`, never truncate-in-place).

---

## 11. Product limits that are not bugs

Do not spend a weekend trying to route around these. They are deliberate.

- 🔑 **The publishing API cannot attach a trending sound.** Trending audio is in-app only. Since
  trending sound is one of the largest reach levers on the platform, a fully automated post is
  structurally the *weaker* version of the post. Treat automated Instagram publishing as a **safety
  net so the account is never dark**, and hand-post hero content with a sound when reach matters.
  This is a real, measured trade-off, not a counsel of despair — automated volume on Instagram does
  drive reach — but budget your expectations accordingly.
- **Links in captions are not clickable.** Ever. Put the link in the bio or in a first comment.
- **Cover art is settable only at publish time** (§8).
- **Insights lag.** Metrics for a fresh post are not immediately meaningful; do not build alerting
  that treats a five-minute-old post's zeros as a failure.

---

## 12. Copy-pasteable config

`makervox-publish.toml` — **every value below is a fake placeholder.** The file holds secret *names*, never
secret *values*.

```toml
version = 1

[state]
dir = "~/.local/state/makervox_publish"
file_mode = 0o600

# ── the account ──────────────────────────────────────────────────────────────
[accounts.moonlit]
display_name = "Moonlit Example"
platforms = ["facebook", "instagram"]

[accounts.moonlit.facebook]
page_id = "100000000000001"          # fake

[accounts.moonlit.instagram]
# Omit ig_user_id to resolve it live from the Page's instagram_business_account.
# Pin it only if you want the publish to fail loudly when the link is broken,
# rather than silently no-op'ing.
# ig_user_id = "17000000000000000"   # fake

# ── the Meta app (shared by both Meta platforms) ─────────────────────────────
[platforms.facebook]
enabled = true
app_id_credential = "META_APP_ID"        # NAME of an env var, not the value
app_secret_credential = "META_APP_SECRET"
api_version = "v21.0"
graph_base = "https://graph.facebook.com"

[platforms.facebook.tokens]
store = "meta_tokens"

# ── Instagram ────────────────────────────────────────────────────────────────
[platforms.instagram]
enabled = true
auth_path = "facebook_page"              # facebook_page | instagram_login
api_version = "v21.0"
graph_base = "https://graph.facebook.com"
instagram_graph_base = "https://graph.instagram.com"   # Path B only

[platforms.instagram.tokens]
store = "meta_tokens"                    # SAME store as facebook — one file, one lock
refresh_after_s = 4320000                # 50d; Path B tokens live 60d

[platforms.instagram.staging]
impl = "makervox_publish.platforms.meta.staging.gcs:GcsStager"

[platforms.instagram.staging.options]
bucket = "example-media-staging"         # REQUIRED. No default. Must allow per-object ACLs
                                         # (uniform bucket-level access DISABLED) unless you
                                         # use public_mode = "signed_url".
project = "example-project-123"          # omit to infer from application default credentials
prefix = "makervox_publish-temp"
public_mode = "object_acl"               # object_acl | signed_url
chunk_bytes = 8388608                    # 8 MiB — resumable upload; timeout becomes PER CHUNK
upload_timeout_s = 300
delete_after_publish = true

[platforms.instagram.publish]
poll_attempts = 30                       # 30 × 6s = 3 minutes
poll_interval_s = 6
send_thumb_offset = true

# ── cover picker (shared with the Facebook publisher) ────────────────────────
[media.cover]
enabled = true
impl = "makervox_publish.media.cover:BrightnessScanCoverPicker"

[media.cover.options]
scan_until_s = 6.0
step_s = 0.25
brightness_floor = 10.0
min_offset_s = 0.75
fallback_floor = 5.0
take_first_qualifying = true

[http.retry]
attempts = 3
backoff_s = 3
retry_on = ["transport"]                 # never retry an API rejection

# ── token store ──────────────────────────────────────────────────────────────
[token_stores.meta_tokens]
impl = "makervox_publish.state.token_store:FileTokenStore"

[token_stores.meta_tokens.options]
path = "~/.local/state/makervox_publish/meta_tokens.json"
atomic_write = true
lock = "meta-tokens"
```

Secrets go in the environment, never in the file:

```sh
# fake values
export META_APP_ID="1234567890123456"
export META_APP_SECRET="00000000000000000000000000000000"
```

---

## 13. Smoke test, in order

Run these top to bottom. Each one fails for exactly one reason, which is the point.

```sh
PAGE_ID=100000000000001
APP=1234567890123456

# 1. Is the token what I think it is? Want expires_at:0 and both instagram_* scopes.
curl -s "https://graph.facebook.com/v21.0/debug_token\
?input_token=$PAGE_TOKEN&access_token=$APP|$META_APP_SECRET"

# 2. Is the Instagram account actually linked? Want a non-empty instagram_business_account.
curl -s "https://graph.facebook.com/v21.0/$PAGE_ID\
?fields=name,instagram_business_account&access_token=$PAGE_TOKEN"

IG=17000000000000000   # the id from step 2

# 3. Can I read the IG account at all? (proves instagram_basic)
curl -s "https://graph.facebook.com/v21.0/$IG\
?fields=username,followers_count&access_token=$PAGE_TOKEN"

# 4. How much publishing budget is left today?
curl -s "https://graph.facebook.com/v21.0/$IG/content_publishing_limit\
?fields=config,quota_usage&access_token=$PAGE_TOKEN"

# 5. Is my staged URL actually public? Must be 200 with Content-Type: video/mp4,
#    from a machine that is NOT logged in to your cloud provider.
curl -sI "https://storage.googleapis.com/example-media-staging/makervox_publish-temp/clip.mp4"

# 6. Create a container. (proves instagram_content_publish)
curl -s -X POST "https://graph.facebook.com/v21.0/$IG/media" \
  -d "media_type=REELS" \
  -d "video_url=https://storage.googleapis.com/example-media-staging/makervox_publish-temp/clip.mp4" \
  -d "caption=test" -d "thumb_offset=750" -d "access_token=$PAGE_TOKEN"

# 7. Poll BOTH fields until FINISHED. `status` is where the real error text lives.
curl -s "https://graph.facebook.com/v21.0/$CREATION_ID\
?fields=status_code,status&access_token=$PAGE_TOKEN"

# 8. Publish.
curl -s -X POST "https://graph.facebook.com/v21.0/$IG/media_publish" \
  -d "creation_id=$CREATION_ID" -d "access_token=$PAGE_TOKEN"
```

Then **open the profile grid on a phone** and look at the tile. A green `ok` in your own logs is
not the deliverable; the post appearing, with a cover that is not black, is.

---

## 14. The short version

1. Path A. Publish through the Facebook Page token. Path B is a maze with a worse token.
2. Link the Instagram account to the Page **in Business Suite** — there is no API for it, and
   `connected_instagram_account` is the wrong field.
3. Enable the permissions **inside a Use Case** before you look for them in the Explorer, then
   **re-mint the token**, because a token is a snapshot.
4. Exchange for a long-lived user token **before** pulling Page tokens, or your "non-expiring"
   token expires in an hour.
5. Your own accounts in Development mode need **no App Review and no Business Verification**.
   Third-party users need both, and Business Verification needs a legal entity.
6. Instagram **pulls** media by URL. Stage it publicly, set a chunk size so the timeout is
   per-chunk, delete it afterwards.
7. Send `thumb_offset`, or your grid is black — and you only get one chance per post.
8. Poll `status`, not just `status_code`. Retry transport failures only, never the whole publish.
9. It cannot attach a trending sound. Automation is a safety net, not the hero path.
