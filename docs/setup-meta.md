# Setting up the Meta developer app (Facebook Pages + Instagram)

**Scope of this document.** Everything you have to do in Meta's consoles *before* `postvox` can
post a single reel — app creation, the use-case/permission model, minting a non-expiring Page
token, and linking Instagram. Then every failure mode this integration has actually produced in
production, stated as **symptom → cause → fix**, because on Meta the symptom almost never
resembles the cause.

**The good news, up front:** posting to *your own* Pages needs **no App Review and no Business
Verification**. Standard Access covers it. If you only ever publish to Pages you administer,
you can be live in an afternoon. The review wall only appears when you want to post to *other
people's* Pages — see [Part 6](#part-6--when-you-actually-need-app-review).

**The bad news, up front:** Meta silently gives you tokens that look right and stop working
later, and the two most expensive bugs in this integration (black thumbnails, missing first
comments) both **succeeded** as far as the API was concerned.

---

## Part 0 — The shape of the thing

Three facts that determine every decision below.

1. **One Meta app serves every brand you publish.** The per-brand thing is the *Page*, which
   connects to the single app. Do not create an app per client — you would redo every review.
2. **The Graph API can only post to a Page.** There is no API for posting to a personal profile.
   If your content is going to a profile today, you need a Page first.
3. **Instagram publishes through the Facebook Page.** A Professional IG account linked to a Page
   is published via `graph.facebook.com/{ig-user-id}` using the **Page token**. You do not need a
   separate Instagram token, an Instagram OAuth flow, or the Instagram-login API. (Taking the
   Instagram-login route instead is a documented 90-minute dead end — [P4.1](#41-instagram-login-oauth-insufficient-developer-role).)

So: **one app → N Pages → each Page optionally carries one IG account.**

---

## Part 1 — Create the app, click by click

Meta renames these screens roughly every other quarter. The *names* below may drift; the
*structure* — app type, use cases, permissions, token — has been stable.

### 1.1 Create it

1. <https://developers.facebook.com/apps> → **Create app**.
2. "What do you want your app to do?" → choose **Other** (the specific use cases on that first
   screen lock you into a narrower permission set).
3. App type → **Business**. This is the type that owns Pages and Instagram publishing.
   > ⚠️ The Business type is also what triggers [P3.1](#31-graph-api-explorer-wont-offer-pages_manage_posts) —
   > permissions are not selectable until they are added to a **use case**. Pick Business anyway;
   > the alternative types cannot do what you want.
4. Name it, give a contact email, and attach a **Business portfolio** if you have one.
   You can attach one later, but see [P6.2](#62-your-pages-are-spread-across-three-business-portfolios).
5. **Settings → Basic** → copy **App ID** and **App Secret**. These are the only two app-level
   secrets you will ever need.

### 1.2 Leave the app in Development mode

Development mode is **sufficient** for everything in this guide. It restricts the app to users
who hold a Role on it (you, as admin) — and you administer the Pages you are posting to, so
that restriction never bites. Reels published this way are fully public on the Page.

Switch to Live mode only when you begin App Review. Live mode requires a privacy policy URL and
an app icon; Development mode does not.

> The one thing Development mode *does* break is the Instagram-login OAuth flow
> ([P4.1](#41-instagram-login-oauth-insufficient-developer-role)). Since you are not using that
> flow, this is a feature, not a limitation.

### 1.3 Add the permissions via **Use cases** (this is the step people miss)

App dashboard → **Use cases** → **Add use case** → **Manage a Page** (labelled "Manage everything
on your Page" on some accounts) → **Customize**.

You now get a **Permissions** list with an **Add** button beside each one. Click **Add** on:

| Permission | What breaks without it |
|---|---|
| `pages_show_list` | `/me/accounts` returns an empty `data` array — you cannot discover any Page |
| `pages_read_engagement` | Page reads fail; required alongside the manage scopes |
| `pages_manage_posts` | **Posting itself.** Text, photo and reel publishing all fail |
| `pages_manage_metadata` | Cannot set the Page's about text or cover photo |
| `pages_manage_engagement` | The **post succeeds** and the first comment returns `(#200)` — [P3.3](#33-200-insufficient-permissions-on-the-comment-only) |
| `pages_read_user_content` | Reading comments/replies (needed only for an engagement loop) |
| `read_insights` | Reel **play counts** come back empty — [P5.4](#54-reels-report-no-reach) |

For Instagram, add the Instagram publishing use case (or the **Instagram** product where your
console still shows products) and **Add**:

| Permission | What breaks without it |
|---|---|
| `instagram_basic` | Cannot resolve or read the linked IG account |
| `instagram_content_publish` | Cannot create or publish IG media containers |
| `instagram_manage_insights` | Optional — IG reach/impressions reporting |

> **If a reel's `start` phase 403s** with the permission set above, add **`publish_video`**.
> Meta's Video Reels reference lists it; the production token this package was extracted from
> worked with the `pages_*` set alone. Both statements are true of different consoles at
> different times — grant it if you hit the wall, don't chase it if you don't.

**Everything in both tables is Standard Access.** "Ready for testing" beside a permission means
it works *right now*, for the app admin, on Pages the admin administers. No review. No
verification. That is the whole reason self-hosting on your own Pages is viable.

### 1.4 Redirect URIs — and why you may not need one

`postvox`'s Meta setup mints its user token in the **Graph API Explorer**, so for the flow in
Part 2 **you never register a redirect URI at all**. That is worth knowing before you spend an
afternoon on OAuth plumbing you don't need.

You need a redirect URI only if you build a browser OAuth flow (e.g. onboarding other people).
If you do, the rules are unforgiving:

- **Settings → Facebook Login for Business → Valid OAuth Redirect URIs.**
- The match is **exact string equality after normalisation** — scheme, host, port, path *and
  trailing slash*. `https://app.example.com/callback` and `https://app.example.com/callback/`
  are **two different URIs**. Registering one and sending the other yields
  *"URL Blocked: This redirect failed because the redirect URI is not white-listed in the app's
  client OAuth settings"* — an error that names redirects and OAuth and says nothing about the
  slash.
- Register **both spellings** of every URI. It costs nothing and removes an entire class of
  hour-long debugging session. This class of exact-string trap has cost real hours on the sibling
  platform in this package (TikTok's URL-prefix verification, where a verified prefix of
  `https://example.com/` refused a submitted `https://example.com`) — the machinery differs,
  the failure is identical.
- **HTTPS is required.** Strict mode is permanently on; wildcards, query strings and fragments
  in the registered URI are rejected. Plain `http://localhost` support has come and gone across
  console generations — do not plan around it. Use the Explorer path (Part 2) for a self-host,
  or terminate TLS on a real hostname if you truly need the browser flow.

---

## Part 2 — Mint a Page token that does not expire

This is a **chain**, and every link matters. Getting a working token from the wrong link is how
you ship something that dies in 60 days.

```
short-lived USER token (~1–2 h, from the Explorer)
   └── fb_exchange_token ──> long-lived USER token (~60 days)
          └── GET /me/accounts ──> PAGE token  (no expiry)
```

### 2.1 The short-lived user token

<https://developers.facebook.com/tools/explorer/>

1. **Meta App** → your app.
2. **User or Page** → **User Token**.
3. **Add a Permission** → tick every scope from §1.3 that you want on this token.
4. **Generate Access Token** → grant in the popup.
5. In the Page-selection step, choose **"Opt in to all current and future Pages"**.
   > **Symptom:** you add a fourth brand's Page months later and it never shows up in
   > `/me/accounts`, no error anywhere. **Cause:** the grant was scoped to the Pages that existed
   > on the day you authorised. **Fix:** re-run this whole section; a re-auth is cheap.

### 2.2 Exchange and harvest

`postvox` does both calls for you (the `connect` step: exchange, then pull every Page token and
store them). Doing it by hand:

```sh
APP_ID=1234567890123456
APP_SECRET=aaaaaaaabbbbbbbbccccccccdddddddd
USER_TOKEN=EAAxxxxxxxxxSHORT_LIVED_FROM_EXPLORERxxxxxxxxx

LONG=$(curl -s "https://graph.facebook.com/v21.0/oauth/access_token\
?grant_type=fb_exchange_token&client_id=$APP_ID&client_secret=$APP_SECRET\
&fb_exchange_token=$USER_TOKEN" | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

curl -s "https://graph.facebook.com/v21.0/me/accounts?fields=name,id,access_token&access_token=$LONG"
```

### 2.3 Prove the Page token is actually non-expiring

**Do not skip this.** It is two seconds and it is the difference between "works" and "works until
a Tuesday in November".

```sh
curl -s "https://graph.facebook.com/v21.0/debug_token\
?input_token=$PAGE_TOKEN&access_token=$APP_ID|$APP_SECRET"
```

You are looking for **three** things in `data`:

- `"type": "PAGE"` — if it says `USER`, you harvested the wrong link in the chain.
- `"expires_at": 0` — zero means never. **Any other number means you called `/me/accounts` with
  the short-lived user token**, which hands back Page tokens that inherit its ~1 hour lifetime.
  Everything will work perfectly for an hour. Go back to §2.2.
- `"scopes": [...]` — the scopes are frozen into this token. See §2.4.

And confirm what the Page token can see:

```sh
curl -s "https://graph.facebook.com/v21.0/me/permissions?access_token=$USER_LONG_TOKEN"
```

### 2.4 🔑 A token is a snapshot. Adding a permission does not upgrade it.

**Symptom.** You hit `(#200)` or "insufficient permission", you add the missing permission in the
app console, you retry, and you get the *identical* error. You conclude the console change didn't
take, and start looking for a second cause.

**Cause.** Permissions are baked into a token at the moment it is minted. Changing the app
changes what *future* tokens may carry. It does nothing to tokens already in your token store.

**Fix.** After **every** permission change: re-run §2.1 → §2.2 in full. The Pages stay linked;
only the tokens change. Then re-check `scopes` in `debug_token` before you retry the failing call.

### 2.5 What silently kills a "non-expiring" token

Non-expiring means "has no expiry timestamp", not "is immortal". These invalidate it with no
warning, no email, and no distinguishing error — the next call just returns `(#190)`:

- The admin **changes their Facebook password**.
- The admin **removes the app** in Facebook → Settings → Apps and Websites.
- You **rotate the App Secret**.
- The admin **loses their admin role** on the Page, or the Page changes ownership/portfolio.
- Meta drops a **security checkpoint** on the account (posting velocity is a trigger).
- **Data-access expiration** on the app (App dashboard → check the current setting) can expire
  data access after a period of inactivity, which takes the token with it.

**Fix:** treat re-auth as routine maintenance, not an incident, and monitor for it. A daily
`debug_token` check on every stored Page token — assert `is_valid: true` and log the scope list —
turns a silent multi-day outage into a one-line alert. See also [P5.1](#51-the-post-failed-and-your-logs-said-ok):
if your pipeline only prints publish results, an expired token is structurally invisible.

---

## Part 3 — Facebook Page publishing: what actually goes wrong

### 3.1 Graph API Explorer won't offer `pages_manage_posts`

**Symptom.** On a **Business**-type app, the permission simply is not in the Explorer's
"Add a Permission" dropdown. You cannot select what you cannot see, so you assume it needs review.

**Cause.** On the Business app type, the Explorer only offers permissions that are attached to a
**use case**. It is not a review gate; it is a visibility gate.

**Fix.** §1.3 — Use cases → Manage a Page → Customize → **Add** beside `pages_manage_posts`.
Then re-mint the token (§2.4). **Cost when undiagnosed: one hour.**

### 3.2 `no Pages returned` from `/me/accounts`

**Symptom.** `{"data": []}` with a 200 status. No error.

**Cause**, in order of likelihood: (a) the token lacks `pages_show_list`; (b) the Facebook user
who granted the app is not an **admin** of the Page (Editor is not enough for `pages_manage_posts`);
(c) you were signed into a *different* Facebook account in the Explorer than the one that
administers the Page; (d) you didn't opt into all Pages during the grant (§2.1).

**Fix.** Check (c) first — it is the one nobody suspects, and it produces exactly the same empty
array as a permission problem.

### 3.3 `(#200) insufficient permissions` on the comment only

**Symptom.** Reels publish perfectly. The first-comment CTA never appears. Nothing in your
success path noticed, because the *post* succeeded.

**Cause.** Creating a comment **as the Page** requires `pages_manage_engagement`, which is not
implied by `pages_manage_posts`. Most permission-set guides omit it.

**Fix.** Add `pages_manage_engagement` (§1.3), re-mint (§2.4). Then verify the comment path
separately from the post path — a publisher that treats the comment as best-effort will never
tell you it is broken. In `postvox` the comment failure is logged at WARNING with its reason;
a swallowed exception here is what hid this for months.

### 3.4 🔑 `(#100) Unsupported post request … Object … does not exist` when commenting on a reel

This one has **two independent causes** and they present identically. This is the single most
expensive failure in this integration. Cost when misdiagnosed: weeks of ~2.4% CTA delivery
(**7 of 288 reels**) while every check read green.

**Cause A — the object id is wrong.** The reel `finish` phase returns a `post_id`. That is **not**
the commentable object. Neither is the bare `video_id`, nor `{page_id}_{video_id}`. The
commentable object is `{page_id}_{story_fbid}`, where `story_fbid` is the **`post_id` field on
the video node**:

```sh
curl -s "https://graph.facebook.com/v21.0/$VIDEO_ID?fields=post_id&access_token=$PAGE_TOKEN"
# -> {"post_id": "122100000000000000", "id": "..."}      # comment on {PAGE_ID}_122100000000000000
```

**Cause B — the object exists before it accepts comments.** Even with the correct id, POSTing to
`{page_id}_{story}/comments` immediately after publish returns the *same* `(#100)`. The `post_id`
field appears on the video node almost at once, so resolving the id is fast — and useless.

⛔ **This is why B looks like A.** The error names the object, so it reads as "wrong object" —
the exact symptom A produces. Do not re-diagnose it as scope or object format. The tell that it
is a race: **successes and failures interleave by date** rather than starting at a deploy. Every
"success" was a run where the id lookup happened to be slow, so the resolver's own sleeps let the
object settle. The code only worked when it was slow.

**Fix.** Retry **the POST**, not just the id resolution. Backoff `5, 10, 15, 20`s — a 50-second
ceiling covers the observed lag, and a first-try success sleeps zero.
Config: `[platforms.facebook.reels.first_comment] post_attempts` / `post_backoff_s`.

**How to confirm it is fixed:** count `first comment added` vs `first comment skipped` in your
logs. Before the fix that ratio was 7:281.

### 3.5 🔑 42 of 50 Page reels rendered as black tiles

**Symptom.** The Page looks abandoned. Every reel thumbnail is solid black. The videos play fine.

**Cause.** Facebook auto-generates ~11 candidate thumbnails per reel and marks one **preferred**.
Measured on a live Page, it had marked the **darkest** (mean brightness 1.6) as preferred while a
16.2 sat unused in the same set. If your video fades in from black, frame 0 is literally
brightness 0.0.

**Fix, and it is cheap:** Facebook — unlike Instagram — accepts a new preferred thumbnail on an
**already-published** video:

```sh
curl -s -F "is_preferred=true" -F "source=@cover.jpg" \
  "https://graph.facebook.com/v21.0/$VIDEO_ID/thumbnails?access_token=$PAGE_TOKEN"
```

So a whole back catalogue is repairable **in place** — no deletes, no reposts, no lost engagement.
A two-pass repair (upload a frame from the local master; where no master exists, download
Facebook's own *brightest* auto-thumbnail and re-upload it as preferred) took one Page from
8 good / 42 black to **49 good / 1 black**.

Going forward, set the cover after every publish:
`[platforms.facebook.reels] set_cover_after_publish = true`, with the frame chosen by
`[media.cover]` (scan for luminance instead of trusting frame 0).

**⚠️ Sub-trap that turned a `--limit 1` test into 14 processed reels:**
`POST /{video-id}/thumbnails` answers `{"success": true}` and **returns no `id`**. Code that
checks for an id counts every success as a failure — and then retries, and reports zeroes.

**Reusable diagnostic** for "why is my thumbnail black":

```sh
ffmpeg -ss 0.5 -i reel.mp4 -frames:v 1 -vf "scale=64:64,format=gray" -f rawvideo - \
  | python3 -c "import sys;d=sys.stdin.buffer.read();print(sum(d)/len(d))"
```

### 3.6 Duplicate reels, and why they cost more than an upload

**Symptom.** The same reel appears on the Page 2–3 times, in a tight cluster under ten minutes
apart, exactly as many times as your retry count.

**Cause — three of them, all independent:**

1. **The retry re-minted the `video_id`.** Facebook's reel upload is three phases:
   `start` (mints a `video_id`) → upload bytes to `rupload.facebook.com` → `finish`.
   Retrying the *outer* function re-runs `start`, so a network drop **after** Facebook already
   committed the publish produces a second reel. Re-uploading and re-finishing the **same**
   `video_id` is idempotent; re-starting is not.
2. **A lock is not a dedupe.** A per-account publish lock only serialises callers. Caller #2
   waits for #1 to finish and then publishes the same reel anyway. Any delivery check made
   *before* acquiring the lock is a TOCTOU — the snapshot goes stale while blocked.
3. **The ledger key was a temp filename.** If one path uploads a short cut (`mkstemp` output,
   no date in the name) and logs *that* name, a recovery path later cannot match it as delivered
   and re-posts hours later.

**Fix.** Retry *inside* the phased upload against one `video_id`
(`[platforms.facebook.reels] upload_retries`); tolerate an "already published" response on a
re-finish; run the delivery re-check **inside** the lock
(`[platforms.facebook.dedupe] check_inside_lock = true`); and key the ledger on the **source**
media, never the wire bytes.

**Why it matters more than a wasted upload:** Facebook reach-**suppresses** near-identical
uploads, so a duplicate doesn't just cost bandwidth — it burns the slot's reach and throttles the
original too.

### 3.7 A link in the post body throttles reach

**Symptom.** Reels with a CTA link in the description reach far fewer people than the same reels
without one.

**Cause.** Facebook suppresses distribution of posts carrying outbound links in the body.

**Fix.** Publish the description **link-free** and put the CTA in the **first comment**
(`[links.strip]` + `[platforms.facebook.reels.first_comment]`). A first comment carries the
clickable, UTM-tagged link without the penalty — and it is what restores `utm_source=facebook`
attribution that otherwise reads as a flat zero.

**⚠️ The sharp edge on that fix:** a link stripper drops *any line containing a link*. A caption
that carries its own deep link (`example.com/match`) loses that line entirely and gets the
generic homepage CTA instead. If per-post deep links matter, strip only the lines you intend to.

---

## Part 4 — Instagram

### 4.1 Instagram-login OAuth: "Insufficient Developer Role"

**Symptom.** The Instagram business-login OAuth flow refuses the account. The error blames your
developer role, so you go hunting through App Roles and Test Users.

**Cause.** The `instagram_business_*` permission family belongs to the **Instagram-login API**
(`graph.instagram.com`), which does not work while the app is in Development mode — and those
permissions are not selectable in the Graph API Explorer at all, because the Explorer speaks
Facebook login.

**Fix — don't go that way.** Publish through the **Facebook Page** instead. Dead ends that cost
~90 minutes and taught nothing: App Roles → Test Users, the Explorer's `instagram_business_*`
search, and the Facebook Login for Business settings page.

### 4.2 The winning path (zero extra console work)

If your IG account is **Professional** and **linked to the Facebook Page**, you publish to it via
`graph.facebook.com/{ig-user-id}` with the **Page token** — the same token that already posts to
the Page. If you selected `instagram_basic` + `instagram_content_publish` when you minted it,
there is nothing else to configure.

Resolve the IG user id from the Page rather than hard-coding it:

```sh
curl -s "https://graph.facebook.com/v21.0/$PAGE_ID\
?fields=name,instagram_business_account&access_token=$PAGE_TOKEN"
# -> {"name":"…","instagram_business_account":{"id":"17000000000000000"}}
```

Publishing is three calls: create a container → poll `status_code` until `FINISHED` → `media_publish`.

### 4.3 🔑 The link between IG and the Page has no API. It is a UI click.

**Symptom.** Everything is wired, the code runs, and IG auto-post silently no-ops. Zero errors —
because "no linked IG account" is indistinguishable from "this brand isn't configured for IG".

**Cause.** `instagram_business_account` is empty on the Page. The link is made in **Meta Business
Suite → Settings → Linked accounts** (or the Page's Linked accounts panel). **There is no Graph
endpoint that creates it.**

**Fix.** Do it in the UI, then verify with the `curl` in §4.2 — an absent
`instagram_business_account` field is the entire diagnosis. On one brand here, this single UI
click was the *only* missing piece: the token already carried the scopes and the code was already
calling the publisher.

### 4.4 🔑 Instagram will not ingest a Facebook-hosted URL

**Symptom.** You try to shortcut FB → IG by handing IG the `source` URL of the video you just
posted to the Page. The container fails in about 8 seconds with a generic *"reel processing
error"* — no mention of the URL, the host, or permissions.

**Cause.** Instagram **pulls media by URL** (you cannot upload bytes), and it will not fetch from
Facebook's CDN.

**Fix.** Stage the local file to storage you control, publish from that public URL, then delete
the object. `[platforms.instagram.staging]` does this; `delete_after_publish = true`, and a staged
object that fails to delete is logged at ERROR **with its URI**, because a forgotten staged object
stays publicly readable.

### 4.5 Staging upload times out on larger files

**Symptom.** `RetryError: Timeout of 120.0s exceeded` while staging a 30–40 MB reel. Small reels
are fine. Cost: three silently lost posts.

**Cause.** Without an explicit chunk size, the storage client does a **single-shot** upload, and
the default 120 s timeout covers the **whole transfer**. On a slow uplink the file simply cannot
finish in time.

**Fix.** Set a chunk size to force a **resumable** upload, where the timeout applies **per chunk**
— a slow link then stretches the upload instead of failing it:
`[platforms.instagram.staging.options] chunk_bytes = 8388608` (8 MiB, must be a multiple of
256 KiB) and `upload_timeout_s = 300`.

### 4.6 Making the staged object public raises 403

**Symptom.** The stager raises on `make_public()`, not on upload.

**Cause.** Per-object public ACLs require **uniform bucket-level access to be DISABLED**. Modern
buckets have it **enabled by default**.

**Fix.** Either use a bucket with uniform access off, or switch the stager to
`public_mode = "signed_url"`. `postvox` detects this and raises a named
`StagingPreconditionError` rather than surfacing a raw 403 you would have to reverse-engineer.

### 4.7 Black covers on Instagram — and the 45 you cannot fix

**Symptom.** The IG profile grid is a wall of black tiles; the account looks broken or empty.

**Cause.** Instagram defaults a reel's cover to **frame 0**. Measured on a real fade-in reel:
`t=0 → 0.0`, `t=0.5 → 63.2`. If `thumb_offset` is not sent, every cover is the black fade-in frame.

**Fix for new posts.** Scan the first seconds for luminance and send `thumb_offset` (milliseconds)
on the REELS container: `[media.cover]` + `[platforms.instagram.publish] send_thumb_offset = true`.
Tune `fallback_floor` if your art is legitimately dark — some clips never clear a strict floor,
and returning "no offset" hands the platform back frame 0, which is the exact bug you were fixing.

**⚠️ There is no fix for already-published IG reels.** The Graph API cannot change the cover of a
published reel. It is Edit → Cover by hand in the app, or delete and repost. (Facebook is the
opposite — see [P3.5](#35--42-of-50-page-reels-rendered-as-black-tiles). Do not assume symmetry
between the two halves of the same API.)

### 4.8 A product limit you cannot code around

The publishing API **cannot attach a trending sound**. That is in-app only. Treat automated IG
posting as a **safety net** so the account is never dark, and hand-post hero content when reach
matters. Design your expectations around this before you build a cadence on it.

---

## Part 5 — Rate limits, caps, and the things that bite in production

### 5.1 The post failed and your logs said OK

Not a Meta limit — the operational failure that hides all the others. If your publisher only
`print()`s each platform's result and returns, say, the notification status, a run where every
platform failed still reports success. Put those prints in `/tmp` on a machine that wipes it on
reboot and the gaps become structurally invisible.

**Fix.** Persist every platform outcome to a durable ledger and alert on failure. Then audit:

```sh
jq 'select(.results|to_entries|any(.value.ok==false))' ~/.local/state/postvox/delivered.jsonl
```

Related: verify what your success flag actually asserts. "ok: true" that means "accepted for
processing" is not the same claim as "visible on the Page".

### 5.2 Page-level rate limits scale with engagement — so a new Page has almost none

Meta's Page rate limit is proportional to the Page's **engaged users** over the last 24 hours.
A brand-new Page with near-zero engagement gets a correspondingly small budget, which is exactly
when you are most likely to be running backfills and repair scripts against it.

**Fix.** Log the `X-Business-Use-Case-Usage` and `X-App-Usage` response headers — they carry
`call_count`, `total_cputime` and `total_time` as percentages of your budget, plus an
`estimated_time_to_regain_access` when you are throttled. Alert at 75%. Do not retry a
throttling rejection; `[http.retry] retry_on = ["transport"]` exists because retrying an API
*rejection* makes it worse.

### 5.3 Instagram's publishing cap is hard and per-account

Roughly **50 published posts per IG account per rolling 24 hours**. Query your remaining budget
rather than counting locally:

```sh
curl -s "https://graph.facebook.com/v21.0/$IG_USER_ID/content_publishing_limit\
?fields=config,quota_usage&access_token=$PAGE_TOKEN"
```

Also: media containers expire after ~24 h, IG captions cap at 2200 characters, and 30 hashtags is
the ceiling. Verify current numbers against the docs before you tune a cadence — these move.

### 5.4 Reels report no reach

**Symptom.** The `views` field is missing or zero on reel video nodes; there is no
`post_impressions` metric.

**Cause.** Reels are not measured like Page posts in this API version. Plays are the reach proxy,
and they live on the **insights** edge, not the video node.

**Fix.** `GET /{video_id}/video_insights?metric=blue_reels_play_count,fb_reels_total_plays`,
with `read_insights` on the token. `[platforms.facebook.reels] insight_metrics` holds the list.

### 5.5 Reach penalties are not rate limits, but they behave like them

Two things throttle you with no error at all: **near-duplicate uploads** ([P3.6](#36-duplicate-reels-and-why-they-cost-more-than-an-upload))
and **in-body links** ([P3.7](#37-a-link-in-the-post-body-throttles-reach)). Both return clean
200s. Your only signal is a reach number that never recovers.

---

## Part 6 — When you actually need App Review

Everything above is **Standard Access**: your app, your admin account, your Pages. No review.

You cross into **Advanced Access** — which means **App Review plus Business Verification** — when
you want to post to Pages **you do not administer**: a client's Page, a customer's Page, any
multi-tenant product.

### 6.1 What that costs

- **Business Verification requires a legal entity** — registered business name, address, and
  documents Meta can verify against a public registry. Plan for it as a lead time, not a form.
- **App Review** wants a screencast demo of the exact permission in use, written step-by-step
  reproduction instructions, a working test account, an app icon, a privacy policy URL, and the
  app in **Live** mode.
- You may additionally be pushed through **Tech Provider verification** depending on how your
  product is classified.

This is the reason `postvox` is bring-your-own-app: self-hosting keeps every one of these on the
operator's own Pages, where none of it is required.

### 6.2 Your Pages are spread across three Business portfolios

**Symptom.** Verification, permissions and asset assignment behave inconsistently across your own
Pages, and Business Suite shows different asset lists depending on which portfolio you are in.

**Cause.** Pages created at different times land in different Business portfolios by default.
Nothing warns you.

**Fix.** Consolidate the Pages you publish under **one** portfolio, then verify that portfolio.
Both consolidation and verification are Meta-UI flows with no API. Verify each step from outside
the UI:

```sh
curl -s "https://graph.facebook.com/v21.0/$PAGE_ID\
?fields=business,instagram_business_account,verification_status&access_token=$PAGE_TOKEN"
```

> If any Page is subject to a legal hold, litigation, or a separate ownership claim, **leave it
> out of the consolidation**. Moving a Page between portfolios changes its ownership record.

---

## Part 7 — Config

Fake placeholders throughout. **No secret values live in this file** — every `*_credential` key
is the *name* of a secret that the credential chain resolves (an environment variable by default),
which is what makes the config safe to commit.

```toml
# ---------------------------------------------------------------- accounts
[accounts.moonlit]
display_name  = "Moonlit Example"
first_comment = "Full write-up, free -> https://moonlit.example"
link_url      = "https://moonlit.example"
platforms     = ["facebook", "instagram"]

[accounts.moonlit.facebook]
page_id = "100000000000001"          # fake — from GET /me/accounts

[accounts.moonlit.instagram]
# Omit to resolve the IG business account from the linked Page (recommended —
# it survives a re-link). Pin it only if you have a reason.
# ig_user_id = "17000000000000000"

# ---------------------------------------------------------------- facebook
[platforms.facebook]
enabled                  = true
app_id_credential        = "META_APP_ID"        # NAME of the secret, not the value
app_secret_credential    = "META_APP_SECRET"
api_version              = "v21.0"              # drives both hosts; keep in lockstep
graph_base               = "https://graph.facebook.com"
rupload_base             = "https://rupload.facebook.com/video-upload"

[platforms.facebook.tokens]
store = "meta_tokens"                # SHARED with instagram: one file, one lock

[platforms.facebook.reels]
upload_retries              = 3      # retries reuse ONE video_id  (P3.6)
upload_backoff_s            = 3
tolerate_already_published  = true
set_cover_after_publish     = true   # P3.5 — FB accepts a cover on a published video
strip_links_from_description = true  # P3.7
insight_metrics             = ["blue_reels_play_count", "fb_reels_total_plays"]  # P5.4

[platforms.facebook.reels.first_comment]
enabled                   = true
include_body              = true
utm_medium                = "reel"
target_resolve_attempts   = 5        # P3.4 cause A — resolve {page_id}_{story_fbid}
target_resolve_backoff_s  = 3
post_attempts             = 5        # P3.4 cause B — retry the POST itself
post_backoff_s            = 5        # 5+10+15+20 = 50s, covers the observed lag

[platforms.facebook.dedupe]
check_inside_lock      = true        # P3.6 — a lock is not a dedupe
duplicate_result_is_ok = true

# --------------------------------------------------------------- instagram
[platforms.instagram]
enabled     = true
auth_path   = "facebook_page"        # P4.1/P4.2 — NOT instagram_login
api_version = "v21.0"

[platforms.instagram.tokens]
store = "meta_tokens"                # same store as facebook

[platforms.instagram.staging]        # P4.4 — IG pulls by URL; it cannot fetch from FB's CDN
impl = "postvox.platforms.meta.staging.gcs:GcsStager"

[platforms.instagram.staging.options]
bucket               = "example-media-staging"   # REQUIRED, no default
prefix               = "postvox-temp"
public_mode          = "object_acl"  # P4.6 — needs uniform bucket-level access OFF
chunk_bytes          = 8388608       # P4.5 — forces resumable; timeout is PER CHUNK
upload_timeout_s     = 300
delete_after_publish = true

[platforms.instagram.publish]
poll_attempts     = 30               # container must reach FINISHED before publish
poll_interval_s   = 6                # 30 x 6s = 3 minutes
send_thumb_offset = true             # P4.7 — or the grid is black tiles

# ------------------------------------------------------------ token store
[token_stores.meta_tokens]
impl = "postvox.state.token_store:FileTokenStore"

[token_stores.meta_tokens.options]
path         = "~/.local/state/postvox/meta_tokens.json"   # chmod 0600
atomic_write = true
lock         = "meta-tokens"
```

The two secrets themselves, in the environment (or any provider in the credential chain):

```sh
export META_APP_ID='1234567890123456'
export META_APP_SECRET='aaaaaaaabbbbbbbbccccccccdddddddd'
```

Page tokens are **not** environment variables. They are minted by `connect` (§2.2) and written to
the token store, owner-readable only.

---

## Checklist

Console, once:

- [ ] Business-type app created; App ID + App Secret copied from Settings → Basic
- [ ] Use case **Manage a Page** added, and every permission in §1.3 **Add**ed inside it
- [ ] Instagram permissions added (`instagram_basic`, `instagram_content_publish`)
- [ ] App left in **Development** mode (Live is only for App Review)
- [ ] IG account is **Professional** and **linked to the Page** in Business Suite — no API for this

Token, once per permission change:

- [ ] User token generated in the Explorer with **all** scopes ticked
- [ ] **"Opt in to all current and future Pages"** selected during the grant
- [ ] Exchanged to a long-lived user token, *then* `/me/accounts` for Page tokens
- [ ] `debug_token` shows `type: PAGE`, `expires_at: 0`, and the scopes you expect

Before you trust it in production:

- [ ] `GET /{page_id}?fields=instagram_business_account` returns an id
- [ ] A test reel publishes **and** its first comment lands (count added vs skipped in the logs)
- [ ] Its thumbnail is not black — on both platforms
- [ ] `content_publishing_limit` is being read, not guessed
- [ ] Rate-limit headers are logged; publish outcomes are persisted, not printed
- [ ] A daily `debug_token` check alerts on `is_valid: false`
