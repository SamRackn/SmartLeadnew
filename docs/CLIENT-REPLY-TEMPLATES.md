# Client reply-handling templates (from Smartlead)

Source: `Master PR Template.pdf`, supplied by the client. What he used in
Smartlead before this project. Transcribed here so the content is greppable
and diffable; the PDF is the artifact of record.

**These are not cold first-touch emails.** Every template is downstream of a
prospect who has already replied or already booked. The lead template opens
"I really appreciate you getting back to me." Our pipeline
(`ingest → enrich → score → draft → verify → review → send`) produces the
message that comes *before* all of this.

## Why this document matters

1. **It is the client's own reply taxonomy.** `CLAUDE.md` lists reply
   classification as Missing with no detail. The categories below are the
   operator's own, with the action attached to each. That labelling is the
   expensive half of building a classifier.
2. **It shows the operating model is per-client.** Page 1: "Change the
   highlighted segments for clients target industry." The booking email says
   "a Google Meet with our Founder, CLIENT NAME." ClientRise runs outbound on
   behalf of its clients, swapping industry per engagement — so multi-client
   workspaces (work-order item 5) describes the current business, not a
   future one.
3. **It is the generic-filler baseline we are replacing.** Every template is
   merge fields and nothing else: NAME, DAY, TIME, TIMEZONE, COMPANY NAME. No
   research signal anywhere. Guardrail 2 exists to prevent exactly this at
   first touch.

## Two cautions

**Generic is correct here.** Guardrail 2 ("weak signals mean no email, no
generic fallback") is about first touch — personalization earns the reply.
Once someone has replied, "here are two times" is the right email. Do not let
the no-fallback rule leak into reply handling.

**No footer in any of the eight.** None carries a physical mailing address or
an unsubscribe. When these become templated sends out of this platform,
guardrail 6 applies, and the footer is appended outside the template for the
same reason it is appended outside the composer: so nothing upstream can omit
it.

## Categories

| Trigger | Template | Action encoded |
|---|---|---|
| Replied, wants the pitch | Straight Offer | Offer two slots, Google Meet |
| Replied, asks to be phoned | Prospect Asks You To Call | Redirect to Google Meet, offer a slot |
| Replied, asks price | Prospect Asks About Price | Deflect to a call, offer a slot |
| Agreed a time | Booking Email | Confirm, ask them to accept the invite |
| No reply (1st chase) | Follow Up #1 | "lost in the shuffle", offer a slot |
| No reply (2nd chase) | Follow Up #2 | Restate offer, ask for 15 min |
| No reply (3rd chase) | Follow Up #3 | Break-up: "Where do we go from here?" |
| Booked then no-showed | No Show #1 | Offer two reschedule slots |
| Still no-showed | No Show #2 | Last attempt, two slots |

Note the client's sequence has no "not interested" or "unsubscribe me" branch.
Suppression (guardrail 1) has to catch that path; the templates do not.

The standing offer across all of them: *marketing agencies, 1–3 more clients
every month.* That is the ICP and the promise any first-touch draft is
setting up.

## Transcription

Header on page 1: *Change the highlighted segments for clients target industry*

### Straight Offer

> Hey NAME,
>
> I really appreciate you getting back to me.
>
> As for the quick rundown, ClientRise works exclusively with marketing
> agencies helping them get 1 - 3 more clients every month via clients
> service.
>
> Think of us as an extension of your company that funnels your future clients
> to you who are currently searching for marketing services.
>
> We build the pipeline. You close the deals.
>
> We should schedule a Google Meet call to discuss this further. I can get you
> in on DAY at TIME am/pm TIMEZONE or DAY at Time am/pm TIMEZONE time.
>
> Let me know which time works for you and I can send over a calendar invite.
>
> Cheers!
>
> Maaz N

### Prospect Asks You To Call Them Directly

> Hey FIRSTNAME, we actually do all of our calls on Google Meet. Are you free
> on DAY at TIME am/pm TIMEZONE to discuss how we can help you gain 1 - 3 new
> clients every month?
>
> Thanks,
> Maaz Nadeem
> COMPANY NAME

### Prospect Asks You About Price

> Hey NAME, pricing really depends on the level of service that you need from
> us, but we have different packages for everyone.
>
> Are you free on DAY at TIME am/pm TIMEZONE to discuss pricing and how we can
> help you get 1 - 3 more clients every month?
>
> Thanks,
> Maaz Nadeem

### Booking Email

> Great NAME — I just booked you in for a Google Meet on DAY at TIME AM/PM
> with our Founder, CLIENT NAME.
>
> Could you please accept the calendar invite that was just sent to your
> email?
>
> Thanks,
> NAME
> COMPANY NAME

### Follow Up #1

> Hey NAME, just wanted to make sure my last email didn't get lost in the
> shuffle. How does a quick 15-min Google Meet on DAY at Time am/pm TIMEZONE
> time work to discuss how we can help you get 1 - 3 new clients every month?
>
> Thanks,
> Maaz Nadeem

### Follow Up #2

> Hey FIRSTNAME, we have been helping marketing agencies gain 1 - 3 more
> clients every month through project research and lead generation.
>
> If we have the blueprint to help you do the same, are you open for a quick
> 15-min call on DAY at TIME am/pm TIMEZONE time to discuss this?
>
> Best,
> Maaz Nadeem
> ClientRise

### Follow Up #3

> Hey,
>
> I tried to reach you a few times but did not hear back. Where do we go from
> here?
>
> Regards,
> Maaz Nadeem
> ClientRise

### No Show Email #1

> Hey NAME,
>
> I saw you missed our Google Meet call. I can get you back on the calendar on
> DAY at TIME am/pm TIMEZONE or DAY at TIME am/pm TIMEZONE.
>
> Do either of these work for you to reschedule?
>
> Best,
> Maaz Nadeem
> ClientRise

### No Show Email #2

> Hey NAME, I'm reaching out one last time to reschedule our call about
> helping your marketing agency get 1 - 3 new clients a month.
>
> I can do DAY at TIME am/pm TIMEZONE or DAY at TIME am/pm TIMEZONE.
>
> Let me know what works best, and I can send over a calendar invite.
>
> Regards,
> Maaz Nadeem
> ClientRise
