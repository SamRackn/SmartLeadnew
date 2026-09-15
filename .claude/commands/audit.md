---
description: Run the compliance-auditor across the tree and report findings ranked by severity.
---

Invoke the `compliance-auditor` agent over the current state of the repo.

It checks the six controls from `CLAUDE.md`'s guardrails section: suppression
before the queue, verification failing closed, no auto-send, complete
delete-by-person, footer address + unsubscribe, and no fabricated or filler copy.

Relay its findings in full, including severity and the owning agent for each fix.
Do not soften a finding or fix it silently — the point of a separate gate is that
its output is visible. If it reports all controls passing, say that plainly.
