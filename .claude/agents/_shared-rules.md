<!-- Reference copy of the rules embedded in every agent definition. Not an agent. -->
## House rules (all MazzSmartLead agents)

1. **Report honestly.** If a test fails, paste the real output. Never claim green
   without having run it. A partial result reported accurately beats a confident
   summary that is wrong.
2. **Stay in your lane.** Each agent owns a file set. Do not edit files another
   agent owns — finish your part and hand back with a note saying what the other
   agent needs to change.
3. **No live network calls in Phase 1.** Every external provider is stubbed and
   reads canned data from `tests/fixtures/`. No API keys are required to run
   anything in Phase 1.
4. **The brief is the spec.** `CLAUDE.md` at the repo root governs. If your task
   conflicts with it, say so rather than silently picking one.
5. **Compliance controls are hard blocks, never warnings.** Suppression checks,
   draft verification, and delete-by-person must fail closed.
