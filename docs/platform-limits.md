# Platform limits

Vendor realities that no amount of code will fix. If you are about to open an
issue asking why makervox_publish cannot do one of these, the answer is that the
platform does not allow it.

Corrections and additions are very welcome — especially for platforms whose
tiers have changed since this was written. Please cite the error string or the
docs page you saw.

---

## TikTok

**Scopes are frozen at authorization.** Adding a scope to your request does
nothing until each account is *re-authorized*. Refreshing the token will not
pick it up, and the error you get back does not say this — it reads like a
permissions bug in your code.

**`user.info.basic` returns identity only.** Ask it for `follower_count` and
you get an error, not a null field. Follower stats need `user.info.stats`.

**Unaudited apps can only post to private accounts.** The error
`unaudited_client_can_only_post_to_private_accounts` means the *account* must
be private. Setting `privacy_level="SELF_ONLY"` does not satisfy it — that is a
property of the post, and both fail identically.

**`creator_info.privacy_level_options` describes the creator, not your app.**
It will happily return `PUBLIC_TO_EVERYONE` while your unaudited app is still
refused. A granted `video.publish` scope proves only that it was *requested*.

**Chunk count uses floor, not ceil.** Single chunk if ≤64MB; otherwise
`total_chunk_count` must equal `floor(video_size / chunk_size)`, and the final
chunk absorbs the remainder. Ceil-based arithmetic produces `invalid chunk
count`.

**Large files fail with a misleading error.** Above roughly 60MB, uploads 403
with `invalid request id`. Transcode before uploading rather than trusting the
documented maximum.

**Drafts do not drain themselves.** Tier-1 uploads land in an inbox with a
pending cap around five. Nothing removes them for you, and once the cap is hit
every upload fails with `spam_risk_too_many_pending_share`.

## Meta — Facebook and Instagram

**A first comment posted too quickly fails.** The story object does not exist
yet. Retry the POST rather than treating the failure as terminal.

**Instagram cannot ingest Facebook-hosted source URLs.** Media must be staged
somewhere else that Instagram can fetch.

**Instagram pulls media by URL.** There is no byte upload — the media must be
reachable at a public URL for the duration of the ingest.

**Trending audio cannot be attached via the API.** It is the single largest
reach lever on both Reels surfaces and it is manual-only.

**Reels report plays, not reach.** They are not the same number and are not
comparable to feed metrics.

**Business Verification requires a legal entity.** It gates the useful
permissions, and no amount of code gets around it.

## X

**There is no free tier.** Posting requires a paid plan.

**A post containing a URL costs roughly 13× one without.** This is why the
common advice is to put links in the profile bio rather than in posts.

## General

**Refresh tokens rotate on use** on several of these platforms. Treat the old
one as dead the moment a refresh succeeds. See
[scar-tissue.md](scar-tissue.md).

**"Accepted" is not "published."** Several of these APIs return success for an
upload that has merely been queued, staged or drafted. Check what your success
flag actually asserts.
