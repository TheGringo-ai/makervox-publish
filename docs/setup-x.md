# Setting up an X (Twitter) developer app for makervox_publish

*Self-hosting guide. You register your own X app; makervox_publish never ships one.*

---

## Read this before you spend an hour on it

Three facts that determine whether this platform is worth your time at all:

1. **There is no free tier.** X is pay-per-use on prepaid credits. Posting costs money from the
   first post. (Verified on `docs.x.com/x-api/getting-started/pricing`, 2026-08-28. Any guide —
   including older notes in this repo's own history — that says "~500 writes/month free" is
   describing the retired `developer.x.com` portal and is wrong.)
2. **A post containing a URL is billed at roughly 13× a plain post.** That is not a style
   preference or a reach heuristic. It is a line item. See [Pricing](#pricing-the-thing-that-actually-caps-you).
3. **There is no app review.** Unlike TikTok and Meta, nobody audits your integration. You can be
   posting programmatically within thirty minutes. The gate here is money and one specific
   click-order trap, not a submission queue.

If you are here because you read that "LLM assistants ground their answers on X posts" and you
want to be citable: that premise was tested directly and did not hold. Asked a factual question
with a request for sources, the assistant cited ordinary web pages, not X posts. Asked about the
account doing the posting, it described it accurately as "an automated or semi-automated content
feed" — a reason to discount a source, not to cite it. Your own indexed site is what earns those
citations. Post to X for the archive and the distribution, not for that.

---

## Part 1 — Register the app, click by click

### 1.0 Prerequisites that stop people at the door

- **An X account with an actual password.** If you sign in with Google or Apple, you have no
  password, and several developer-portal and account-settings screens demand one. Run "Forgot
  password?" and set one *before* you start. (This bit us: the username-change screen refuses to
  proceed without it, and there is no API for that field.)
- **A verified phone number on the account.** The developer signup requires it.
- **A payment method you are willing to attach later** — but see §1.7, do not attach it yet.

### 1.1 Create the developer account

1. Go to **`https://developer.x.com`**. It will bounce you to the current portal, which is
   **`https://console.x.com`**. Both names are live and both appear in official docs; they are the
   same product mid-rename. Bookmark whichever one loads.
2. Sign in with the X account **that will do the posting**, not a personal one you happen to be
   logged into. The app's default access token is minted for the signed-in account (see §1.5), and
   getting this wrong means redoing the token step.
3. Fill in the use-case free text. This is read by a human in some cases and can be rejected. Be
   boring and specific: *"Scheduled publishing of my own original content to my own account, one
   post per day, via POST /2/tweets."* Do not mention scraping, aggregation, or anything about
   other users' data.

### 1.2 Create a Project, then an App inside it

The hierarchy is **Project → App → Environment**, and it matters:

- A **Project** is the billing/entitlement container. Endpoints under `/2/` require the app to
  belong to a Project. A "standalone app" (a legacy shape you may still see) cannot call v2 at all.
- An **App** holds the keys. Environments are **Development** / **Staging** / **Production**.
  Development is fine for a self-hosted poster and is what the working setup here uses.

Click **Projects & Apps → + Create Project**, name it, pick a use case, then create an App inside
it. The app name is globally unique across all of X — expect your first three choices to be taken.
It is also cosmetic; it is not shown on your posts (the *account* name is).

> **One app per platform, not one per brand.** If you publish for several brands, they share this
> one X app. The per-brand thing is the *account* that authorizes it. Getting this backwards means
> multiplying every setup step below by your brand count for no benefit.
>
> On X specifically, be aware that one set of OAuth 1.0a credentials = **one account**. Multi-brand
> posting through one credential set is not multi-account posting; it is several brands writing to
> the same timeline. makervox_publish's `enabled_accounts` allow-list exists for exactly this reason.

### 1.3 User authentication settings — the screen that gates everything

In the App, open **Settings → User authentication settings → Set up**.

You must complete this form even if you only intend to use OAuth 1.0a. The permission radio you
need lives here, and the form will not save without the URL fields below.

| Field | What to put | Why |
|---|---|---|
| **App permissions** | **Read and Write** | "Read" is the default and cannot post. Do not pick "Read and write and Direct message" unless you actually need DMs — the wider scope is a bigger blast radius on a leaked key for zero benefit. |
| **Request email from users** | Off | Adds a review-ish friction and you do not need it. |
| **Type of App** | **Web App, Automated App or Bot** (confidential client) | This is the only option that gives you a client secret and OAuth 2.0 refresh tokens. Choose it even if you plan to use 1.0a — it costs nothing and leaves the door open. |
| **Callback URI / Redirect URL** | See §2 | Mandatory. Cannot be blank even for 1.0a-only use. |
| **Website URL** | A real `https://` URL you control | Mandatory. Cannot be `localhost`. Cannot be blank. |
| Terms of service / Privacy policy | Optional for a personal app | Required only if you put the app in front of other people. |

Hit **Save**. If it refuses without telling you why, it is almost always the Website URL (must be
`https://`, must not be an IP, must not be `localhost`).

### 1.4 Enable OAuth 1.0a

On the same screen there are two independent toggles: **OAuth 2.0** and **OAuth 1.0a**. Turn on
**OAuth 1.0a**. makervox_publish's X client uses OAuth 1.0a user context, signed per request — see §4 for
why that is the right choice for an unattended poster.

You can leave OAuth 2.0 on as well. It does not interfere.

### 1.5 Keys and tokens — and the one step everybody misses

Open the **Keys and tokens** tab. There are three pairs and they are not interchangeable:

| Credential | Also called | Used by makervox_publish? | Notes |
|---|---|---|---|
| **API Key / API Key Secret** | Consumer Key / Secret | ✅ `X_API_KEY`, `X_API_SECRET` | Identifies the *app*. ~25 and ~50 chars. |
| **Bearer Token** | App-only auth | ❌ **No** | ~116 chars. **Cannot post.** App-only auth has no user to post as. Generating one and wondering why `POST /2/tweets` 403s is a common dead end. |
| **Access Token / Access Token Secret** | User context tokens | ✅ `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | Identifies the *account*. Minted for whoever owns the app. |

🔑 **THE TRAP.** The Access Token and Secret carry the permission level that was in force **at the
moment they were generated**. If the app was on the default "Read" when you first visited this tab
— and it was, because §1.3 comes after app creation — then the token you are looking at is
**read-only**, and it will stay read-only forever no matter what the app settings now say.

**You must click Regenerate on the Access Token & Secret AFTER saving Read and Write in §1.3.**

- **Symptom:** `403` from `POST /2/tweets` with a body along the lines of
  `"Your credentials do not allow access to this resource"`, or on the v1.1 endpoints the older
  `"Read-only application cannot POST"`. The keys are correct, the signature is correct, the account
  is fine, and nothing you change in the app settings fixes it.
- **Cause:** permission level is baked into the token at generation time.
- **Fix:** regenerate the access token pair. Then verify — do not assume:

```sh
curl -s -D- -o /dev/null --oauth1 \
  --user "$X_API_KEY:$X_API_SECRET" \
  --oauth1-token "$X_ACCESS_TOKEN" --oauth1-token-secret "$X_ACCESS_SECRET" \
  "https://api.twitter.com/1.1/account/verify_credentials.json" | grep -i x-access-level
```

You want `x-access-level: read-write`. If it says `read`, you regenerated nothing. This one header
is the fastest ground truth on the whole setup and is worth wiring into a health check.

*(Older `curl` builds lack `--oauth1`. If yours does, do the same check from Python with
`requests_oauthlib` — the point is the response header, not the tool.)*

### 1.6 Copy the four values out — carefully

The Access Token & Secret are shown **once**. If you navigate away, you regenerate.

Two mechanical failures that have cost real time here:

- **A trailing newline** from a copy-paste or a `echo` into a file breaks the OAuth signature and
  produces a `401` that looks exactly like a wrong key. Store with `printf %s`, and verify length:
  API key ≈ 25 chars, API secret ≈ 50, access token ≈ 50 (with a `-` after the numeric user id),
  access secret ≈ 45.
- **The access token is prefixed with the numeric user id** (`1234567890123456789-AbCd…`). If yours
  has no leading digits-then-dash, you copied the wrong field.

### 1.7 Buy credits — and do NOT enable auto-recharge

Billing lives under the console's usage/billing section. X is **prepaid credits**, not a monthly
subscription: you buy a balance and requests draw it down.

- ⛔ **Do not enable auto-recharge.** That is the surprise-bill mechanism. A publisher stuck in a
  retry loop can spend real money quickly.
- ✅ **Set a spending limit** instead.
- ✅ A balance of `$0.00` with no payment method is a genuinely safe default while you are wiring
  things up: posts **fail loudly rather than silently bill**. Test your error handling in that state
  first, then top up.

A working single-account poster at one post per day costs about **$5.50 a year**. Adding a link
reply takes it to about **$78 a year**. Budget accordingly and read §5 before you decide.

### 1.8 The xAI trap

⛔ **A Grok / xAI API key does not work for posting.**

`console.x.ai` (the xAI model API) and `console.x.com` (the X API) are separate services with
separate auth and separate billing. They are related companies — X API spend can earn xAI credits
back — but that is a rebate, not single sign-on. Keys are not interchangeable in either direction.
If you already have an xAI key and assumed you were done, you are at step zero.

---

## Part 2 — Redirect URI requirements and the exact-match traps

### The rule

X compares your supplied callback against the registered list **byte for byte**. Everything is
part of the comparison:

- scheme (`http` vs `https`)
- host (`localhost` vs `127.0.0.1` are **different strings**)
- port (`:8722` is part of the match)
- path case (`/Callback` ≠ `/callback`)
- **trailing slash** (`/callback` ≠ `/callback/`)
- query string, if you put one there

### What it looks like when you get it wrong

| Symptom | Cause |
|---|---|
| OAuth 1.0a `POST oauth/request_token` returns **`403 Forbidden`** with `"Callback URL not approved for this client application"` | The `oauth_callback` parameter is not byte-identical to a registered URI. |
| OAuth 2.0 authorize page renders **"Something went wrong. You weren't able to give access to the App."** with no further detail | Same cause. X deliberately gives the user no diagnostic here, which is why this burns an hour. |
| Everything works on your laptop and fails on the server | The server picked a different ephemeral port, or resolved `localhost` to `::1`. |

### The specific traps

1. **The trailing slash.** This is the single most expensive character in social API setup. A
   sibling platform in this same package charges an hour for the identical mistake — a verified URL
   *prefix* of `https://example.com/` refusing to match a submitted `https://example.com`, with the
   unhelpful error "This URL is not verified." X's redirect matching is the same class of bug with
   less helpful text. **Register both variants.** X allows multiple callback URIs; there is no cost
   to listing `https://example.com/oauth/x/callback` and
   `https://example.com/oauth/x/callback/` side by side, and it removes a category of failure
   permanently.

2. **`localhost` vs `127.0.0.1`.** Register `http://127.0.0.1:8722/callback`. X is more reliable
   about the loopback IP than the hostname, and Python's `http.server` binds the IP anyway. If you
   want to type `localhost` in a browser, register that too — as a *second* entry, not a
   replacement.

3. **Pin the port.** A loopback listener that grabs a random free port cannot have a registered
   callback. makervox_publish's config pins it:

   ```toml
   [cli.auth_listener]
   host = "127.0.0.1"
   port = 8722
   ```

   Change the port and you must change the registered URI too. That is a two-place edit; treat the
   config value as the source of truth and copy from it.

4. **`http://` on loopback is allowed; `http://` on a real domain is not.** Do not waste time
   trying to register `http://example.com/callback`.

5. **The Website URL field is not the callback field.** They are separate, both mandatory, and both
   validated differently. The Website URL cannot be loopback.

6. **If you only ever use OAuth 1.0a with pre-minted tokens from §1.5, you still need a callback on
   file** — but you will never exercise it. Put a valid loopback URI there and move on. Do not skip
   the field hoping it is optional; the permissions radio will not save without it.

---

## Part 3 — Scopes: what needs review, what works immediately

**Good news, stated plainly: nothing about posting is review-gated on X.** There is no audit, no
demo video, no submission form, no waiting. That is the one respect in which X is dramatically
easier than TikTok or Meta.

### Works the moment you have Read+Write and credits

- `POST /2/tweets` — create a post.
- `POST /2/tweets` with `reply.in_reply_to_tweet_id` — thread a reply.
- `POST /1.1/media/upload.json` — attach a still image (single multipart POST).
- `DELETE /2/tweets/:id`
- `GET /1.1/account/verify_credentials.json` — the access-level probe from §1.5.

OAuth 2.0 equivalents, if you go that route, are the scopes `tweet.read`, `tweet.write`,
`users.read`, `media.write`, plus `offline.access` for a refresh token. All self-serve. None
reviewed.

### Gated — but by price tier, not by review

These are the things people assume they will get and then cannot afford:

- **Reading your own post metrics at any useful volume.** Reads are billed per resource. Trending
  impressions for a daily poster is cheap; anything analytics-shaped at scale is not.
- **Full-archive search, filtered stream at volume, follower enumeration.** Higher access tiers.
- **Academic / research access** in its old generous form no longer exists. Do not design around
  documentation that predates the pricing change.

### Not available at all through this path

- **Changing the account's handle.** There is no API. It is
  `x.com/settings/account` → Account information → Username, in a browser, and it demands the
  account password. (`x.com/settings/username` 404s — do not follow that link from an old blog
  post.)
- **Video posting**, unless you build it. See §5.

---

## Part 4 — Token lifetime and refresh behaviour

### OAuth 1.0a (what makervox_publish uses, and why)

**The access token and secret do not expire.** There is no refresh flow, no expiry timestamp, and
nothing to schedule. For an unattended daily poster this is a feature, not a legacy wart: the whole
class of "the scheduler silently stopped six weeks ago because a token lapsed" cannot happen here.

For contrast — this is not hypothetical — the LinkedIn publisher in this same family of integrations
has ~60-day tokens, and when one expired *every post failed with no bounce, no error mail, and no
visible symptom other than posts stopping*. It now needs a dedicated monitor with a 14-day warning.
X's OAuth 1.0a needs none of that.

**What *does* invalidate an OAuth 1.0a token:**

| Event | Effect |
|---|---|
| You regenerate the API Key/Secret | All access tokens die immediately. |
| You **change the app's permission level** | Existing access tokens are invalidated / stay at the old level. This is the §1.5 trap seen from the other side. |
| The user revokes the app | Settings → Security and account access → Apps and sessions. Silent from your side; you just start getting 401s. |
| The app is suspended | 403s with an app-suspension message. |
| The account is locked or under review | May block writes; see §5. |

**What does *not* invalidate it — verified:**

🔑 **OAuth binds to the numeric user id, not the handle.** An account here was renamed in place
(handle *and* display name changed) and the existing tokens kept working with **zero
reconfiguration** — still `read-write` on the very next `verify_credentials` call. If you are
rebranding, you do not need to re-auth.

⚠️ But note what the rename *did* cost: the account's post count dropped from 37 to 26. Old posts
under the previous branding were cleared. Rename an account you care about with that in mind.

### OAuth 2.0 (if you choose it instead)

- Access token: **~2 hours**.
- Refresh token: requires the `offline.access` scope, and **rotates on every use** — the old one is
  dead the instant the new one is issued.
- 🔑 **Rotation-on-use is a concurrency bug waiting to happen.** Two processes refreshing the same
  account destroys one of them, and recovery is a manual browser re-authorization. If you go this
  route, serialize the refresh across the read *and* the write, re-read inside the lock so the loser
  adopts the winner's token, and write to local disk *before* anything that can time out. makervox_publish's
  token stores do exactly this; it is documented in `docs/scar-tissue.md`.

For a single-account headless poster, **use OAuth 1.0a and avoid the entire problem.**

### The Bearer token

App-only auth. ~116 chars. It authenticates the *app*, not a user, so it **cannot post**. Keep it if
you want it for app-only reads; do not wire it into the posting path and do not treat its presence
as evidence that setup is complete.

---

## Part 5 — Failure modes: symptom, cause, fix

Everything in this section actually happened. The symptom rarely resembles the cause; that gap is
the reason this section exists.

### 5.1 The platform posts nothing and never says why

- **Symptom:** every scheduled run completes cleanly. No error. Nothing appears on X.
- **Cause:** `requests_oauthlib` was declared in `requirements.txt` but **not installed in the venv
  the scheduler actually runs**. The publisher raised `ImportError`, the per-platform dispatcher
  swallowed it as "this platform is unavailable," and the log line read as routine.
- **Fix:** make the missing-dependency path *loud* and distinguishable from "not configured yet".
  makervox_publish returns a specific reason string (`requests_oauthlib not installed`) rather than a generic
  false. Also: check the interpreter your scheduler uses, not the one in your shell — they are
  frequently different, and on a Mac with a broken Homebrew Python they can be very different.

### 5.2 403 with correct-looking credentials

- **Symptom:** `403` on `POST /2/tweets`. Keys verified, signature verified, account healthy.
- **Cause:** access token minted before Read+Write. See §1.5.
- **Fix:** regenerate the token pair; confirm `x-access-level: read-write`.

### 5.3 `api.x.com` 404s where `api.twitter.com` works

- **Symptom:** you "modernize" your host constant and half your calls start 404ing.
- **Cause:** ⚠️ **`api.x.com` is not an alias for `api.twitter.com`.** Verified: profile update works
  at `api.twitter.com/1.1/account/update_profile.json` and 404s on `api.x.com`. Posting is the same.
- **Fix:** keep the hosts exactly as the working code has them. Note the deliberate mixed-generation
  split, which looks like a typo and is not:

  ```
  posting : https://api.twitter.com/2/tweets
  media   : https://upload.twitter.com/1.1/media/upload.json
  ```

### 5.4 The account goes "under review" right after setup

- **Symptom:** an interstitial on the account; possible write blocking.
- **Cause:** a combination that reads exactly like an account takeover — a brand-new write-capable
  app, profile fields changed **via the API**, and a password reset, all on a dormant low-activity
  account within an hour.
- **Fix (and prevention):** make profile changes **in the web UI**, not through the API, especially
  on an account with little history. Space the setup steps out. In the incident here, writes were
  never actually blocked (`verify_credentials` stayed `read-write` throughout) — but that was luck.
  Buying credits is a different system and does not trigger this.

### 5.5 One slot posts, the later slots silently never do

- **Symptom:** the 09:20 job posts every day; the 18:40 job has *never once* posted. Its log line
  says `daily cap reached` — which looks like the governor working correctly.
- **Cause:** a date-rollover bug. The stored date was read **after** it had already been overwritten
  with today's date, so the "is this a new day?" comparison was always true, the midnight reset
  branch was unreachable, and the counter climbed forever. The cap then let exactly one post through
  per day: the first run passed the guard on the stale date, incremented past the cap, and blocked
  every later slot.
- **Fix:** read the stored date **before** writing the new one.

  ```python
  same_day  = state.get("date") == today   # BEFORE
  state["date"]  = today
  state["count"] = (state.get("count", 0) + 1) if same_day else 1
  ```

  And test the rollover explicitly with a frozen clock — this bug is invisible to any test that runs
  inside a single day.

### 5.6 Two brands quietly double your cadence and your bill

- **Symptom:** the account posts twice a day. Every per-brand check reads green: brand A posted once,
  brand B posted once, both within their caps.
- **Cause:** **per-brand state files.** One set of OAuth 1.0a credentials is **one account**. A
  per-brand counter lets each brand spend its own "one per day" onto the same timeline.
- **Fix:** **one state file per account, not per brand.** The cap belongs to the account, because
  that is what X rate-limits and bills.

  ```toml
  [platforms.x.governor.state]
  options = { path = "~/.local/state/makervox_publish/x_governor.json" }   # ONE file
  ```

### 5.7 An unexpected brand starts posting

- **Symptom:** content from a completely different product lands on the wrong account.
- **Cause:** a shared `send_post()` path and a **deny-list** instead of an allow-list. Anything not
  explicitly denied inherited posting rights the moment it passed some other gate.
- **Fix:** allow-list, and **empty means deny all**. makervox_publish ships `enabled_accounts = []` semantics
  as deny-all deliberately; a generic package that defaulted to allow-all would invert the safety
  property the setting exists for.

### 5.8 A post ends in a preposition

- **Symptom:** posts read `Today's card is The Tower. Full reading at` — a sentence with no object.
- **Cause:** the shaper strips the URL but leaves the connector phrase that pointed at it.
- **Cause of the *harder* version:** the fix is order-dependent and the wrong order **still passes a
  naive test**. Hashtags must be removed **before** the dangling-connector sweep. Captions put tags
  after the link, so with tags still present the leftover `Full reading at` sits mid-string and the
  anchored end-of-string pattern never fires. A test that asserts on the *final* output passes either
  way, because the final output ends in hashtags regardless.
- **Fix:** strip hashtags → strip URLs → sweep trailing connectors → re-append at most two hashtags.
  **Assert on the body, not on the whole output.**

### 5.9 A dry run that lies about the expensive branch

- **Symptom:** dry-run output reads `would post + link reply` on a code path where the link reply
  never happens.
- **Cause:** the message was hardcoded rather than derived from the actual flag.
- **Fix:** compute the cost from the same variable the real path branches on. A dry run that
  misstates the one action costing 13× is worse than no dry run at all.

  ```python
  cost = price_per_post + (price_per_post_with_url if link_reply else 0.0)
  ```

### 5.10 A stray URL is a silent 13× billing error

- **Symptom:** none. The post succeeds. The log looks perfect. The bill is 13× for that post.
- **Cause:** a caption template gained a link and nothing checks for one.
- **Fix:** a test that pins "the default post body contains no URL." This is the only thing that
  catches it, because every other signal says success.

### 5.11 …but an attached photo's `t.co` link is a false alarm

- **Symptom:** you attach an image, the post text now visibly contains a `t.co` URL, and you panic
  about the 13× rate.
- **Cause:** that is X's **media reference**, not an external URL.
- **Verified:** it does **not** trigger the higher rate. Still billed at the plain-post price. Do not
  disable images over this.

### 5.12 The duplicate filter silently eats your daily post

- **Symptom:** some days simply have no post. The log says `near-duplicate of a recent post`.
- **Cause:** topic selection by `random.choice()` repeats within a fortnight, and the near-duplicate
  fingerprint (correctly) refuses it.
- **Fix:** a **no-replacement bag** — shuffle the full topic list, draw without replacement, reshuffle
  when empty. Persist the bag alongside the counter.

### 5.13 The content-kind allow-list drifts and the day is skipped

- **Symptom:** a post is generated, refused, and the day skipped — with a log line that looks routine.
- **Cause:** a content generator gained a new category name that was never added to the
  `allowed_slugs` allow-list.
- **Fix:** a test that asserts every generator category appears in the allow-list. Also note the
  deliberate asymmetry makervox_publish ships, which is not an oversight:

  ```
  listed slug        -> allowed
  unlisted slug      -> DENIED
  missing/empty slug -> ALLOWED
  ```

  Refusing on *missing* metadata silently stops posting altogether, which is the hardest failure of
  all to notice. Some callers legitimately never pass a slug.

### 5.14 One exception freezes the whole scheduler for days

- **Symptom:** every platform stops publishing. No `failed:` line anywhere in the log — which is
  itself the signature.
- **Cause:** a `SystemExit` (or `KeyboardInterrupt`) escaping one publisher. **`except Exception`
  cannot catch these** — they derive from `BaseException`.
- **Fix:** the *caller* that iterates publishers catches `BaseException`, and each publisher returns
  `(ok, detail)` rather than raising. Distinguish routine outcomes (not configured / cap reached /
  duplicate) from real failures so they do not alert on every run.

### 5.15 Video does not work

- **Symptom:** you pass an `.mp4` and get a text-only post.
- **Cause:** video needs the chunked `INIT` / `APPEND` / `FINALIZE` flow on the v1.1 media endpoint
  plus `STATUS` polling until processing completes. makervox_publish implements **stills only** — a single
  multipart POST.
- **Fix:** either build the chunked path, or post the text and share the video by hand. Note that
  vertical 9:16 clips of 60–75 seconds are not native to X anyway; measure whether X sends you
  anything before spending a day on this.

### 5.16 An image failure costs you the day's post

- **Symptom:** the media upload 4xx'd and the post never went out.
- **Cause:** treating an optional attachment as a required step.
- **Fix:** **an image is a bonus, never a blocker.** Log the upload failure, attach nothing, post the
  text.

  ```toml
  [platforms.x.media]
  image_failure_blocks_post = false
  ```

---

## Part 6 — Rate limits and the caps that actually bite

### Pricing (the thing that actually caps you)

Point-in-time figures, verified 2026-08-28 on `docs.x.com/x-api/getting-started/pricing`.
**Re-verify before you trust them** — X has changed this page repeatedly.

| Operation | Cost |
|---|---|
| `Post: Create` | **$0.015** / request |
| `Post: Create (with URL)` | **$0.200** / request |
| `Post: Create (summoned)` | $0.010 |
| Reads (Posts) | $0.005 / resource |
| Owned Reads | $0.001 / resource |

At one post a day: **plain post ≈ $5.48/yr; post + link reply ≈ $78/yr**. A link reply is itself a
post carrying a URL, so threading the link dodges the *reach* penalty but not the *charge*. Whether
$78/yr is worth per-post UTM attribution depends entirely on whether the channel sends you anyone.
Default it off, put the link in your profile bio (the same route Instagram and TikTok force on you
anyway), and flip it on when the traffic justifies it.

```toml
[platforms.x.link_reply]
enabled = false   # cost, not preference
```

### Documented rate limits

`POST /2/tweets` is limited per-user and per-app on a 15-minute window. **The specific numbers have
changed several times and any figure written in a guide goes stale**, so do not hardcode one — read
the response headers and honour them:

```
x-rate-limit-limit      # ceiling for this window
x-rate-limit-remaining  # what's left
x-rate-limit-reset      # unix seconds until the window rolls
```

A `429` means back off until `x-rate-limit-reset`. For a once-daily poster you will never see one;
for a backfill you will see one immediately.

### Hard content limits

- **280 characters.** Truncate at 277 and add an ellipsis, and prefer a sentence boundary past ~180
  characters so you do not cut mid-word.
- **4 images per post**; **5 MB per still** on the simple multipart upload (larger needs chunked).
- **Duplicate content is rejected** by X itself — an identical post returns a 403 about duplicate
  content. Fingerprint on lowercased alphanumerics only, so emoji and punctuation churn cannot
  disguise a repeat as new.

### The cap that actually matters: your own

X suppresses accounts that behave like marketing bots, and **suppression is one-way — a suppressed
account cannot be un-suppressed by posting more.** Every governor default below trades reach per
post for the account staying visible at all:

- **One post per account per day.** This number is measured, not guessed. Across eight consecutive
  posts one account here logged impressions of **0, 0, 0, 2, 0, 0, 0, 0** on 2 followers. Cadence was
  never the constraint; four posts a day bought four times nothing. The cadence history was 2 → 4 → 1.
- **Spacing is the anti-spam signal, not the count.** If you do run more than one, put ~9 hours
  between them and keep them clear of your other platforms' slots.
- **At most two hashtags.** A hashtag wall is a spam signal here, unlike TikTok where five is normal.
- **Near-duplicate suppression over the last 30 posts.**
- ⛔ **Do not raise the cap to fix reach.** If you have 2 followers, posting more is the same
  experiment with a bigger bill. Fix follower count first.

---

## Part 7 — Config

### Environment variables

```sh
# X — OAuth 1.0a user context. All four required; none of them is the Bearer token.
export X_API_KEY="AbCdEfGhIjKlMnOpQrStUvWx1"
export X_API_SECRET="AbCdEfGhIjKlMnOpQrStUvWxYz0123456789aBcDeFgHiJkLmN"
export X_ACCESS_TOKEN="1234567890123456789-AbCdEfGhIjKlMnOpQrStUvWxYz01234"
export X_ACCESS_SECRET="AbCdEfGhIjKlMnOpQrStUvWxYz0123456789aBcDeF"
```

Use `printf %s` rather than `echo` when writing these to a file. A trailing newline breaks the
OAuth signature and produces a `401` indistinguishable from a wrong key.

### `makervox-publish.toml`

The config file holds credential **names**, never credential **values**, so it is safe to commit.

```toml
[platforms.x]
enabled = true

# OAuth 1.0a USER CONTEXT — not an OAuth 2.0 bearer. The app must be Read+Write
# and the access token REGENERATED AFTER setting those permissions, or it stays
# read-only forever. Confirm with the `x-access-level` response header.
api_key_credential       = "X_API_KEY"
api_secret_credential    = "X_API_SECRET"
access_token_credential  = "X_ACCESS_TOKEN"
access_secret_credential = "X_ACCESS_SECRET"

# Inert until all four resolve: the client returns (false, reason) and posts
# nothing. Wiring this in before setup is a silent no-op, not a crash.

api_base         = "https://api.twitter.com/2"
# Media upload is still the LEGACY v1.1 host, on a different domain. A real
# mixed-generation quirk, not a typo. api.x.com is NOT an alias for either.
media_upload_url = "https://upload.twitter.com/1.1/media/upload.json"

[platforms.x.governor]
# ONE post per ACCOUNT per day. Not per brand: one credential set is one account.
daily_cap        = 1
timezone         = "UTC"
recent_keep      = 30
enabled_accounts = ["moonlit"]                # ALLOW-list. EMPTY = DENY ALL.
allowed_slugs    = ["howto", "explainer", "reference", "definition"]

[platforms.x.governor.state]
# ONE state file for the account. A per-brand file lets two brands each spend
# their own "1 per day" onto the same timeline — doubling cadence and bill while
# every check still reads green.
impl    = "makervox_publish.state.counters:JsonCounterStore"
options = { path = "~/.local/state/makervox_publish/x_governor.json" }
# JsonCounterStore is an unlocked read-modify-write: correct only when callers are
# serialized on one machine. For threads/workers/containers use SqliteCounterStore.

[platforms.x.shape]
max_chars             = 280
truncate_to           = 277
min_sentence_boundary = 180
min_body_chars        = 20
max_hashtags          = 2
strip_urls            = true
# ORDER IS LOAD-BEARING: hashtags come out BEFORE the dangling-connector sweep.
trailing_connectors   = ["at", "to", "on", "in", "via", "here", "link", "bio",
                         "more", "read", "full"]

[platforms.x.link_reply]
# A post containing a URL is billed ~13x. The link lives in the PROFILE BIO
# instead. Cost of that: bio traffic is one bucket, no per-post UTM.
enabled    = false
template   = "{cta} -> {url}"
cta        = "Full write-up, free"
utm_medium = "social"

[platforms.x.pricing]
# Dry-run/estimate only. Point-in-time — VERIFY on the current pricing page.
price_per_post          = 0.015
price_per_post_with_url = 0.200
currency                = "USD"
verified_on             = "2031-01-01"

[platforms.x.media]
# Stills only; video needs chunked INIT/APPEND/FINALIZE + polling (not built).
image_upload_enabled      = true
image_failure_blocks_post = false   # an image is a bonus, never a blocker

[cli.auth_listener]
# Must match a registered Callback URI byte for byte, port included.
host = "127.0.0.1"
port = 8722
```

Any value can be overridden by environment for a one-off:

```sh
MAKERVOX_PUBLISH_PLATFORMS__X__GOVERNOR__DAILY_CAP=2 makervox_publish publish …
```

---

## Part 8 — Verify, in this order

Each step isolates one failure class. Do not skip ahead; a later step failing tells you nothing if
an earlier one was never confirmed.

1. **Credentials resolve.** Four values present, correct lengths, no trailing newlines.
2. **Access level is `read-write`.** The `verify_credentials` header check from §1.5. If this says
   `read`, nothing after it will work.
3. **Dry run.** Confirms shaping, the governor, and the cost estimate without spending anything.
   Read the output: it should say *no URL* and quote the plain-post price. If it mentions a link
   reply and you did not enable one, fix §5.9 before going further.
4. **Balance is `$0.00`, deliberately.** Attempt a real post and confirm it fails cleanly with a
   billing error rather than a stack trace. This is your only chance to test that path safely.
5. **Top up, post once for real.** Test on a deliberately hostile caption — one containing a URL, a
   dangling call-to-action, and five hashtags. A correct shaper turns that into clean body text, at
   most two tags, and no link.
6. **Post a second time the same day** and confirm the governor *refuses* it. A cap you have never
   seen fire is a cap you do not have.
7. **Cross midnight** (or freeze the clock in a test) and confirm the counter resets. §5.5 is
   invisible to any test that runs inside a single day.

### One-off extra post

Decrement `count` in the governor's state file. **Do not edit `daily_cap`** — a cap edited for a
one-off is a cap that stays edited.

---

## Honest summary

| | X |
|---|---|
| App review required | **No.** None. |
| Time to first programmatic post | ~30 minutes, if you get §1.5 right |
| Free tier | **No.** Prepaid credits from post one. |
| Token expiry (OAuth 1.0a) | **Never.** No refresh to schedule. |
| Video support in makervox_publish | Not implemented (chunked upload) |
| The one step everybody misses | Regenerating the access token **after** setting Read+Write |
| The one thing that will cost you money silently | A URL in the post body |
| Realistic expectation | A new account with few followers is heavily downranked. This opens the channel; it does not produce traffic. |

X is the *easiest* platform in this package to get authorized on and the easiest to overspend on.
Every other guide in `docs/` is about surviving a review queue. This one is about not getting billed
13× for a character you did not mean to include.
