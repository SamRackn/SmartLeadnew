<!-- Reference copy of the rules embedded in every agent definition. Not an agent. -->
## House rules (all MazzSmartLead agents)

1. **Report honestly.** If a test fails, paste the real output. Never claim green
   without having run it. A partial result reported accurately beats a confident
   summary that is wrong.
2. **Stay in your lane.** Each agent owns a file set. Do not edit files another
   agent owns — finish your part and hand back with a note saying what the other
   agent needs to change.
3. **Run tests on your own database.** Every test runs against a real
   Postgres and truncates between tests, so two agents running `pytest`
   concurrently corrupt each other's runs. `createdb mazz_<yourname>`,
   migrate it, and pass `DATABASE_URL=...` on every test command. Never
   run bare `pytest` — that hits the shared `mazz_dev`.
4. **Tests never touch the network or spend an API credit.**
   `tests/conftest.py` has a session-wide autouse fixture pinning every provider
   to stub and blanking credentials, so a plain `pytest` run cannot bill the
   client's account. Do not weaken it. Live adapters exist in production code;
   turning one on is an explicit settings change, and a key in the environment
   is not consent to spend.
5. **The brief is the spec.** `CLAUDE.md` at the repo root governs. If your task
   conflicts with it, say so rather than silently picking one.
6. **Compliance controls are hard blocks, never warnings.** Suppression checks,
   draft verification, and delete-by-person must fail closed.
