# Scar tissue

Every default in this library that looks paranoid is here because the
straightforward version failed in production. This file records the incident
behind each one, so a future contributor can tell a deliberate choice from an
accident — and so nobody "simplifies" a guard back into the bug it prevents.

If you change any behaviour described here, please say in the PR which incident
you believe no longer applies.

---

## Refresh tokens rotate on use

**What happened.** Two scheduled jobs published for the same account minutes
apart. Both noticed the access token was near expiry, both called refresh. The
provider issued a new refresh token to the first caller and *invalidated the
one the second caller was holding*. The second job's write then clobbered the
good token with a dead one. Recovery was a manual browser re-authorization —
there is no programmatic way back.

**Why the obvious fix is not enough.** A lock around the write does not help:
the loser already read the old token before the winner rotated it, so it wakes
up and writes a token that was invalidated while it slept.

**What this library does.** The lock spans the read *and* the write, the token
is re-read after the lock is acquired so the loser adopts the winner's result
instead of spending its own, and the new token is written to local disk before
any network call that can time out. A refreshed token that is not persisted is
unrecoverable, so persistence happens first and remote sync second.

## Deduplication has to happen inside the lock

**What happened.** A publish path checked "have I already posted this?", then
took a lock, then posted. Two callers both passed the check, then queued
politely on the lock, and the second one published the same video again.

**What this library does.** The dedupe check is re-run after the lock is held,
never before it. A lock serializes callers; it does not make a stale read fresh.

## A filename is not an identity

**What happened.** A scheduler rendered the same content type twice in one day.
The second render overwrote the first file, so two genuinely different videos
shared one name — and the deduplicator, keyed on the filename, treated the
second as a duplicate of the first and refused to post it.

**What this library does.** Identity is `(account, date, key, slot, content
hash)`, where the hash comes from the media bytes. When it cannot resolve an
identity it says so loudly rather than silently disabling deduplication, on the
grounds that a visible failure beats an invisible one.

## Empty allow-list means post nothing

**What happened.** A shared publish path was reused by a new caller that had no
allow-list configured. An empty list was read as "no restrictions", so the new
caller inherited posting rights to every account the path could reach.

**What this library does.** Allow-lists deny by default. An empty X account
allow-list means *post nothing*, and you must name accounts to enable them.

## Cover frames are worth a warning, never a failure

**What happened.** Instagram covers were generated from frame 0. Many videos
open on a fade from black, so the grid filled with black thumbnails. The
selection code had been failing and returning the default frame without saying
anything.

**What this library does.** Cover selection scans for a non-black frame, and
every deliberate `except` that continues anyway logs its reason. A missing
cover is not worth failing a post over; a cover that vanished without a word is
how a profile grid becomes a wall of black squares.

## Nothing is created on import

**What happened.** Importing the publishing layer on a fresh machine created
directories under `$HOME`, shelled out to a cloud CLI, and opened a socket —
during `import`. Tests could not run offline, and a machine that had merely
*read* the code had state written to it.

**What this library does.** Loading a config touches no socket, no subprocess
and no `$HOME`. Directories appear on first write.

## A shared temp filename is a race

**What happened.** Two writers serialized their updates through
`os.replace(path + ".tmp", path)`. The rename is atomic; the *name* was not.
Both wrote to the same temp path and one silently lost every row it had added.

**What this library does.** Temp names include the process id, and the
read-modify-write is performed under a lock rather than assuming the atomic
rename makes it safe.

## `ok: true` is not "published"

**What happened.** A Tier-1 TikTok integration reported success for weeks. The
API had accepted every upload — into a drafts inbox that nobody ever emptied.
Uploads eventually began failing with `spam_risk_too_many_pending_share`, at
which point it became clear that none of the "successful" posts had ever been
public.

**What this library does.** Upload and publish are distinct operations with
distinct return values, and the inbox path documents that it produces a draft
requiring manual action — not a live post.
