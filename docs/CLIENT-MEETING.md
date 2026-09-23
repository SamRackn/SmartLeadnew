# Client meeting: what to ask for, what to set up

**Date:** 2026-09-23 · **For:** Sean Gough / Search Site Marketing

Everything here is blocking in some way. The four items in §1 are the ones
without which **the system cannot send a single email**, so get those first and
let the rest run long if the meeting is short.

---

## 1. The four blockers

### 1.1 The offer sentence — exact wording

Every email is built from a fixed template with five slots. Three come from the
campaign and they are the client's copy decisions, not ours:

> Hey **{first name}**,
>
> Quick question — is **{company}** currently looking to bring in more clients?
>
> We help **{vertical}** **{offer line}**. **{proof point}**
>
> Worth sending over a little more info?

**Ask for:**

| Slot | From the screenshot | Confirm or replace |
|---|---|---|
| Vertical | `law firms` | Is every list this vertical, or do different lists need different campaigns? |
| Offer line | `generate qualified client opportunities through search marketing` | Exact wording he wants to stand behind |

**Critical constraint — the offer line cannot contain a number.** The system
refuses to save one, at the campaign form and again in the composer. That
sentence reads as the sender's own voice, so nothing holds its figures to
anything; a statistic typed there would be the strongest claim in the email with
nothing behind it. Numbers have exactly one legitimate route in — see next.

### 1.2 Proof points — and the client name behind each one

The screenshot's email says:

> One recent client saw **50+ potential clients in a single month**.

That sentence cannot go out as written. A number may only enter the copy through
a `proof_points` record, and **the client name is mandatory** — an unattributed
statistic is exactly the shape of claim the table exists to prevent.

**Ask for, per proof point:**

- [ ] The claim, **verbatim**, as he wants it printed
- [ ] **Which client it was** (required — no name, no claim)
- [ ] Roughly when it happened
- [ ] Where the evidence lives (a dashboard, a report, an email — for our records, never sent)
- [ ] **Is he willing to name this client in outbound email?** If not, the claim cannot be used. Ask whether he has a different client who *can* be named.

Two or three usable proof points is a good outcome. One is enough to launch.

> **Do not soften this.** Cold email making unverifiable claims about results is
> legal exposure, not a quality nit. The system will refuse to send it, and that
> refusal is the product working.

### 1.3 DNS on both sending domains

The four mailboxes are loaded and deliberately **paused**. They cannot send
until someone confirms:

- [ ] **SPF** record on `searchsitemarketinghub.com`
- [ ] **DKIM** signing enabled on `searchsitemarketinghub.com`
- [ ] **DMARC** policy on `searchsitemarketinghub.com`
- [ ] **SPF** record on `getsearchsitemarketing.com`
- [ ] **DKIM** signing enabled on `getsearchsitemarketing.com`
- [ ] **DMARC** policy on `getsearchsitemarketing.com`

Smartlead was handling deliverability. It is now ours, and this is the part that
has no software fix — a domain without these lands in spam regardless of how
good the copy is.

### 1.4 Google Workspace service account

To send as those mailboxes we need a service account with **domain-wide
delegation**, scoped to `gmail.send` and nothing else (not `gmail.modify` — the
system must be able to send mail and must not be able to read his inbox).

- [ ] Who administers the Google Workspace tenant(s)?
- [ ] Are both domains in **one** tenant, or two? (One tenant = one JSON; two = two)
- [ ] Get the service-account JSON — **as a file, not pasted into chat or email**

Setup, once we have it: place the JSON somewhere readable and set
`GMAIL_SA_SEARCHSITEMARKETINGHUB_COM` and `GMAIL_SA_GETSEARCHSITEMARKETING_COM`
to its path. The database never stores a credential — only the *name* of one.

---

## 2. Warmup — decide, do not drift

Two burner domains, four mailboxes, no warmup pool. **We do not build warmup**:
it is a network effect needing thousands of opted-in mailboxes, and four
mailboxes emailing each other is the precise pattern spam filters exist to
catch.

- [ ] Is warmup already running on these domains? Through what?
- [ ] If not: which service does he want to buy? (This is a purchase decision, not a build task.)
- [ ] How old are the two domains? A domain registered last week should not be sending cold mail at all yet.

Current caps are set for a ramp, not steady state: **10/day per mailbox, 15
minutes apart, 40/day across the roster**. His stated volume of ~500/week needs
about 18/day per mailbox. Raising the caps is a one-field change per mailbox —
but it should follow warmup, not precede it.

---

## 3. The lead list

- [ ] **Get a real list file**, in whatever format his source exports (`.xlsx`, `.xls` or `.csv` all work now)
- [ ] Which tool is he pulling leads from?
- [ ] Roughly how many leads per week?

**Only two columns are actually required:** a name and an email. Everything else
improves the research but nothing breaks without it. Header spelling does not
matter — `Email`, `Work Email`, `Email Address` and `Business Email` all map to
the same field, so he does **not** need to re-header his export.

Columns the system understands, beyond name and email: company, website, job
title, employee count, revenue range, location, primary industry, specific
industry, LinkedIn URL, notes. Unknown columns are ignored rather than rejected.

**Worth showing him in the meeting:** drop his real file onto the import screen
and watch the row-by-row result — accepted, suppressed, invalid — before
anything is queued.

---

## 4. Suppression — ask, because it is easy to forget

- [ ] Does he have an existing **do-not-contact / unsubscribe list** to load? From Smartlead, or anywhere else?
- [ ] Any **domains** to block wholesale (current clients, competitors, his own)?

Anyone who unsubscribed through Smartlead must be carried over. Sending to
someone who already opted out is the one compliance failure that is both certain
to happen and entirely preventable.

---

## 5. Decisions only he can make

### 5.1 What counts as success?

The system currently reports **cost per usable lead**. Ask whether the number he
actually cares about is **cost per qualified conversation** — a reply worth
having. These produce different dashboards and different optimisation targets,
and it is much cheaper to pick now than to re-derive later.

### 5.2 Leads whose vertical we cannot confirm

If the system cannot confirm from a prospect's own website that they are a law
firm, it **refuses to write the email** rather than guessing. That is deliberate:
"We help law firms…" sent to a dentist is the failure he is paying to escape.

There is one opt-in exception: "We help **businesses like yours**…", which
asserts nothing and so cannot be false. It is off by default.

- [ ] Show him a `no_angle` lead and ask: skip these, or send the generic version?

### 5.3 Approve and send are two separate clicks

Worth walking through, because it differs from Smartlead's "Launch Campaign":

1. **Review** — read the email, edit if needed, approve. Nothing is queued.
2. **Outbox** — press Send on a specific message. That is the only thing in the system that puts mail on the wire.

There is no "send all" and there is not going to be one. If he asks for one,
that is a conversation worth having properly rather than in passing — it is the
control that makes every other guarantee mean something.

---

## 6. Demo script

### Before he arrives

```bash
.venv/bin/alembic upgrade head          # already done, safe to repeat
.venv/bin/python scripts/seed_client_demo.py
.venv/bin/uvicorn mazz.web.app:app --reload
```

The seed is **additive** — it does not reset the database, so the mailbox
roster survives. It loads the SearchSiteMarketing campaign, eight law-firm
leads, and runs them through the pipeline.

**Two things to fix or explain before showing the dashboard:**

1. **A leftover demo mailbox, `dana@outreach-northwind.com`, is the only
   `active` one.** It is fake seed data, and it is currently supplying all the
   capacity the dashboard reports ("Mailboxes active: 1 of 5", "30 of 30
   available") while his four real ones sit paused. Either pause it on
   `/mailboxes` before the meeting, or be ready to explain it.
2. **"Cost per usable lead" shows nothing, and that is the good news.** The
   panel is hidden because there is no measured AI spend to divide — email 1
   is a fixed template costing zero tokens. Worth saying out loud: the metric
   he was quoted has no numerator on the cold path any more.

### What the seeded data deliberately shows

Not eight happy leads — a demo where everything succeeds shows none of the
controls he is paying for:

| Leads | Outcome | What it demonstrates |
|---|---|---|
| 5 law firms | drafted, verification **passed** | the normal path |
| Krieger Kim | `no_angle`, **no draft** | site never says what the business is — the vertical gate |
| Konta Georges | `no_angle`, **no draft** | site says it is not taking new clients |
| Jason Goldman Law | **refused at ingest** | already on the do-not-contact list |

### The run-through

**Sending is off** (`EMAIL_SENDER=stub`) — real MIME is written to a file and
nothing touches the network, so nothing can leave by accident.

1. **Import** — drop his spreadsheet on `/leads/import`, show accepted vs rejected per row
2. **Review** — `/review`, two panes: list left, email right. Search, click through a few
3. **Show a refusal** — open Krieger Kim, `no_angle` with no draft, and explain why that is the product working
4. **Show a hold** — edit a body to add "we got them 200 new cases", save, watch it refuse and say why
5. **Approve** — the same lead, unedited. Note that nothing is queued yet
6. **Outbox** — `/outbox`, roster capacity, press Send on one message
7. **Dashboard** — `/dashboard`, hold rate, sending, cache effectiveness
8. **Mailboxes** — `/mailboxes`, his four, paused, and why

### The email it produces

> Hey Louis,
>
> Quick question — is Fasulo Braverman currently looking to bring in more clients?
>
> We help law firms generate qualified client opportunities through search marketing.
>
> Worth sending over a little more info?

Compare to his Smartlead screenshot: same sentence, same shape. The missing
piece is the proof-point line ("One recent client saw 50+…"), which is absent
**because the proof point is seeded inactive with placeholder text**. That is
§1.2 — get the real claim and the real client name, add it on the campaign
screen, and the sentence appears.

---

## 7. Things to tell him plainly

- **Deliverability is now his risk and ours, not Smartlead's.** The upside is control — rotation, pacing, his own domains. The cost is that a burned domain is a real, recoverable-but-painful event, and warmup and DNS are not optional.
- **Removing Smartlead saved roughly $39/month at his volume.** That was never the point; one burned domain costs more than a year of that saving. The point is control and owning the rail.
- **Follow-ups do not exist yet.** Email 1 only. No sequences, no reply inbox, no reply classification. If his process depends on a 3-step sequence, say so now — it changes what gets built next.
- **The unified inbox does not exist yet either.** Replies land in his actual mailboxes and he reads them there for now.

---

## 8. Post-meeting checklist

- [ ] Vertical + offer line, digit-free, in writing
- [ ] At least one proof point **with a named client**
- [ ] SPF/DKIM/DMARC confirmed on both domains
- [ ] Service-account JSON in hand, `gmail.send` only
- [ ] Warmup decision made
- [ ] Real lead file
- [ ] Suppression list exported from Smartlead
- [ ] Metric chosen: cost per lead, or cost per conversation
- [ ] Generic-fallback decision: skip `no_angle` leads, or send the vacuous version
- [ ] Confirm whether a fifth inbox exists — four were supplied, five were mentioned earlier
