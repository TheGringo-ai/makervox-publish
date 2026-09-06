## What this changes

<!-- One or two sentences. -->

## Why

<!-- If this touches a default described in docs/scar-tissue.md, say which
     incident you believe no longer applies. Those defaults look paranoid
     because the straightforward version failed in production. -->

## Checklist

- [ ] `pytest -q` passes
- [ ] No new required dependency (or it is behind an optional extra)
- [ ] Nothing is created at import time — no socket, subprocess or `$HOME` write
- [ ] No account name, domain, cloud project id or personal path added as a default
