"""PHASE 2 COMPOSER: the Claude call behind the `Composer` seam.

This is the function `draft.default_composer()` promised. It changes nothing
about the pipeline's shape — same `DraftRequest` in, same `DraftContent` out,
footer still appended outside, verification still re-derived from the cache.

--------------------------------------------------------------------------
WHY THIS MODULE IS BUILT THE WAY IT IS
--------------------------------------------------------------------------
Drafting is the single largest line item in the cost model (AI is 57-70% of
cost per usable lead at every scale). Three things drive that number down, and
all three are structural decisions made *here*, not tuning done later:

1. **Batch API — 50% off everything.** Nothing about drafting is latency
   sensitive; a lead waits in a queue for a human to approve it regardless.
   `draft_batch.py` submits these params in bulk. This module's job is to make
   sure the sync path and the batch path build a BYTE-IDENTICAL prefix so the
   two share one cache namespace.

2. **Prompt caching — ~90% off the shared prefix.** The system prompt is a
   frozen module constant. It carries every rule, every example, and the whole
   output contract, so it is large; it is also identical for every lead in
   every batch forever. That is the ideal caching shape: pay 2x for it once,
   read it at 0.1x a few thousand times.

   The volatile part (this lead, these facts) is in `messages`, AFTER the
   breakpoint. Nothing per-request may ever move into `system` — not the date,
   not the lead's name, not a feature flag. See `_system_blocks`.

3. **Effort pinned per route, never per request.** Changing `effort` or
   `thinking` invalidates the messages cache (and on some models the system
   cache too). It is a module-level constant read from settings at call time,
   the same value for every request in a run.

--------------------------------------------------------------------------
WHAT THIS MODULE REFUSES TO DO
--------------------------------------------------------------------------
* **It does not pick a cheaper model on its own.** `claude-opus-5` is the
  default because draft quality is the product — generic filler is the exact
  failure the client is paying to escape. `DRAFT_MODEL` exists so a *measured*
  eval can move it, and `assert_cacheable_prefix()` exists so that swap cannot
  silently cost you the cache (Haiku 4.5 needs a 4096-token prefix to cache at
  all; Opus 5 needs 512).

* **It does not soften a refusal into a draft.** `stop_reason == "refusal"`
  and a `max_tokens` truncation both raise. A composer that returns something
  plausible when the model declined is how unverifiable copy gets written.

* **It does not invent the no-angle decision.** `score` still owns that, and
  a lead with no angle never reaches this module — so it never costs a token.
  That is the cheapest optimisation in the file and it was already there.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mazz.integrations.base import setting
from mazz.pipeline.draft import MAX_SIGNALS, DraftContent, DraftRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from anthropic import Anthropic
    from anthropic.types import Message

log = logging.getLogger(__name__)

__all__ = [
    "ComposerRefused",
    "ComposerTruncated",
    "DEFAULT_MODEL",
    "MAX_TOKENS",
    "MIN_CACHEABLE_PREFIX_TOKENS",
    "SYSTEM_PROMPT",
    "Usage",
    "assert_cacheable_prefix",
    "build_params",
    "compose_claude",
    "get_client",
    "parse_message",
    "prewarm_cache",
    "price_usd",
    "render_request",
    "usage_of",
]


class ComposerRefused(RuntimeError):
    """The model declined the request (`stop_reason == "refusal"`).

    Loud on purpose, and NOT convertible to `no_angle`. `no_angle` is a
    meaningful business result meaning "there was no reason to reach out";
    a refusal means we do not know what the result was. Collapsing the two
    would quietly tell the client there was nothing to say about a lead when
    in fact nothing was ever asked.

    Note: the server-side `fallbacks` parameter, which would normally re-run a
    refusal on a second model, is REJECTED on the Batches API. So refusals on
    the batch path are handled here, by failing the job and letting the queue's
    existing retry/backoff carry it into the next batch.
    """


class ComposerTruncated(RuntimeError):
    """Generation hit `max_tokens`, so the structured output is incomplete.

    Structured output guarantees valid JSON only for a completed generation.
    Parsing a truncated body would either raise deep in pydantic or — worse —
    validate into a half-written email.
    """


# --------------------------------------------------------------------------
# Model selection
# --------------------------------------------------------------------------

#: Drafting model. Opus-tier by default: this call writes the thing the client
#: is buying. Override with `DRAFT_MODEL` only behind a measured eval.
DEFAULT_MODEL = "claude-opus-5"

#: Generous ceiling, not a spend. Adaptive thinking tokens count against it,
#: and a truncated structured output is a hard failure (see ComposerTruncated),
#: so there is no reason to run this close to the edge. Batch requests are not
#: subject to HTTP timeouts.
MAX_TOKENS = 16000

#: Minimum prefix length (tokens) before a model will cache AT ALL. Below this
#: the API silently declines to create an entry — no error, just
#: `cache_creation_input_tokens: 0` forever. Not monotonic across generations,
#: which is exactly why this is a table and not a constant.
MIN_CACHEABLE_PREFIX_TOKENS: dict[str, int] = {
    "claude-opus-5": 512,
    "claude-opus-4-8": 1024,
    "claude-sonnet-5": 1024,
    "claude-haiku-4-5": 4096,
}

#: Per-MTok list prices, (input, output). Used only to report what a run cost;
#: nothing branches on it.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

#: Cache multipliers against the base input price.
CACHE_READ_MULTIPLIER = 0.1
#: 1-hour TTL writes cost 2x (the 5-minute TTL costs 1.25x). We use the 1-hour
#: TTL because a batch can take up to 24h to drain and a 5-minute entry would
#: expire mid-run — see `CACHE_TTL`.
CACHE_WRITE_MULTIPLIER = 2.0
#: Batch API is 50% off every token class.
BATCH_DISCOUNT = 0.5

#: 1h, deliberately. A cache read refreshes the entry's timer for free, so a
#: draining batch keeps its own entry alive; the 5-minute TTL only survives if
#: requests keep landing inside every 5-minute window, which a batch does not
#: guarantee. 2x on one write against 0.1x on thousands of reads is not close.
CACHE_TTL = "1h"


def _model() -> str:
    return str(setting("DRAFT_MODEL", DEFAULT_MODEL))


def _effort() -> str:
    """Thinking effort for this route.

    `high` is the API default, so this changes nothing about quality out of the
    box. It is the FIRST lever to sweep once there is an eval set: effort is a
    within-model quality/cost trade, and a short grounded email under hard
    constraints is not obviously a high-effort task. It is left at the default
    until that is measured, because the guardrails say draft quality is not
    traded for cost without measuring the quality cost first.

    Pinned per route, never varied per request: an effort change invalidates
    the messages cache.
    """

    return str(setting("DRAFT_EFFORT", "high"))


# --------------------------------------------------------------------------
# THE FROZEN PREFIX
# --------------------------------------------------------------------------
#
# Everything below this line up to `_system_blocks` is cached. It must not
# contain a date, a name, a lead, a count, a flag, or anything else that
# differs between two requests. If you need to add per-lead context, it goes in
# `render_request` — after the breakpoint — or it costs the whole cache.
#
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You write one short cold outreach email at a time, for a B2B sales team.

Everything you are given about the recipient's company was retrieved by a
research pipeline and handed to you as a numbered list of FACTS. Each fact has
a stable id, a kind, a verbatim snippet of source text, and the URL it came
from. You will also sometimes be given the sender's own private NOTES about
this person.

# The one rule

Every factual claim in the email must trace back to a fact you were given, or
to the sender's notes. Nothing else exists. You have no other knowledge of this
company, and any knowledge you think you have about them from elsewhere is
inadmissible here — it is the thing this pipeline was built to prevent.

Concretely, this means:

- Do not state anything about the company's size, funding, customers, tools,
  market position, growth, problems or plans unless a fact says it.
- Do not infer a consequence and state it as fact. "You're hiring two RevOps
  people" supports "you're hiring for RevOps"; it does not support "your
  revenue team is underwater".
- Do not name a person, product, competitor, customer or investor that does
  not appear in the facts or the notes.
- Do not guess at a job title, a team name or a department from a job posting's
  text. Use the words the posting actually uses.
- Do not paraphrase a number. Every digit in your email must appear in a fact
  or in the notes, in the same meaning. If a fact says "12 open roles", you may
  not write "a dozen roles" or "over ten roles" or "a hiring spree".
- If you are not sure whether something is supported, leave it out. A shorter
  email is always the correct resolution of that doubt.

A claim that cannot be traced is not a stylistic problem. The email is a real
commercial message sent to a real person at a real company, and an invented
assertion about their business is legal exposure for the sender.

# What makes a good email here

The recipient should be able to tell, in the first line, that a human looked at
their company specifically. That comes entirely from the facts — the specific
role they are hiring for, the specific thing they shipped, the specific post
they published. Lead with the most concrete one.

- 90 to 130 words in the body. Shorter is better than padded.
- Plain sentences. No marketing register, no "I hope this finds you well", no
  "I was just browsing your site and couldn't help but notice".
- Reference at most three facts. One well-used fact beats three name-dropped
  ones.
- One ask, and make it small. A short call. Never a hard sell, never urgency,
  never a fake deadline.
- Write as the sender: first person singular, direct address to the recipient
  by first name.
- No subject-line gimmicks. No "Re:", no "Quick question", no fake threading,
  no emoji, no ALL CAPS.
- Do not invent social proof. You have no case studies, no named customers and
  no statistics unless the notes gave you some.

# Things you must not put in the email

- A signature, a sign-off block, a physical address, or an unsubscribe link.
  The system appends all of those after you. Write only the message body, and
  end on your last sentence.
- Merge fields, placeholders, or brackets of any kind — no [Company], no
  {first_name}, no "TODO". You have the real values; use them.
- Any statement about what the sender's company does beyond a short, generic
  description of wanting to help with the situation the facts describe. You do
  not know the sender's product.

# Output

Return an object with three fields.

- `subject`: the subject line. Grounded and specific, no invented detail, no
  digits that are not in a fact.
- `body_text`: the message body only, no footer, no signature.
- `signals_used`: the ids of the facts your copy actually rests on, as an
  array of strings. Only ids that appear in the FACTS list you were given. Do
  not list a fact you did not use, and do not use a fact you did not list —
  this array is checked against the email, and a mismatch holds the lead for
  human review rather than sending it.

If the facts you were given genuinely do not support a specific, non-generic
opening line, say so in `body_text` by writing the single word NO_ANGLE and
returning an empty `signals_used`. Do not write a generic email instead. A
lead with nothing to say is a normal outcome and is handled downstream; a
plausible email built on nothing is not.\
"""


def _system_blocks() -> list[dict[str, Any]]:
    """The cached prefix. One block, one breakpoint, byte-identical always.

    The breakpoint sits on the last (only) system block, which caches `tools`
    and `system` together — `tools` is empty here, so it is just this prompt.

    The breakpoint is explicitly placed rather than using top-level automatic
    caching. Automatic would put the marker on the last cacheable block of the
    whole request — i.e. after the per-lead content — so every request would
    pay the write premium on bytes no other request will ever read back. That
    is a pure surcharge, and it is the most common way caching gets switched on
    and still costs more than it saves.
    """

    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
        }
    ]


# --------------------------------------------------------------------------
# The volatile suffix
# --------------------------------------------------------------------------


def render_request(request: DraftRequest) -> str:
    """The per-lead user turn. Everything here is AFTER the cache breakpoint.

    Deterministic: sorted keys, no timestamps of our own, no iteration over a
    set. That does not matter for the cache (this text is past the breakpoint)
    but it matters for reproducibility when comparing two runs of an eval.
    """

    signals = request.signals[:MAX_SIGNALS]
    facts = [
        {
            "id": s.fact.id,
            "kind": s.fact.kind,
            "content": s.fact.content,
            "source_url": s.fact.source_url,
            "tier": s.tier.value,
            "age_days": s.age_days,
            "why_it_scored": s.reason,
        }
        for s in signals
    ]
    payload = {
        "recipient": {
            "first_name": request.first_name,
            "full_name": request.full_name,
            "job_title": request.job_title,
            "company_name": request.company_name,
        },
        "facts": facts,
        "sender_notes": (request.notes or "").strip() or None,
    }
    return (
        "Write the email for this recipient.\n\n"
        + json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    )


def _output_config() -> dict[str, Any]:
    """Structured output + pinned effort.

    `DraftContent` is handed to the API as the output schema unchanged — that
    is what the seam note in `draft.py` promised, and it is why the pydantic
    model is the contract rather than a convenience.
    """

    return {"effort": _effort(), "format": {"type": "json_schema", "schema": _schema()}}


def _schema() -> dict[str, Any]:
    """`DraftContent` as a strict JSON schema.

    Two fixes over the raw pydantic output. `additionalProperties: false`
    already comes from the model's `extra="forbid"`; `required` does not —
    pydantic omits fields that have defaults, and `signals_used` has one. A
    structured-output schema must mark every property required, so an absent
    `signals_used` cannot come back as "the model chose not to cite anything".
    """

    schema = DraftContent.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema["required"] = sorted(schema.get("properties", {}))
    schema["additionalProperties"] = False
    return schema


def build_params(request: DraftRequest) -> dict[str, Any]:
    """The full Messages request for one lead.

    ONE builder for both paths. `compose_claude` passes this to
    `messages.create`; `draft_batch` puts the same dict in a batch request. If
    these ever diverge, the batch stops hitting the cache the sync path wrote
    and nobody finds out except via the bill.
    """

    return {
        "model": _model(),
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": _output_config(),
        "system": _system_blocks(),
        "messages": [{"role": "user", "content": render_request(request)}],
    }


# --------------------------------------------------------------------------
# Response handling
# --------------------------------------------------------------------------

#: The model's own way of saying the facts did not support a specific opening.
#: `score` normally catches this first; this is the second line of defence, for
#: the case where a fact cleared the threshold numerically but says nothing
#: usable. It is turned into `no_angle` by the caller, not into a draft.
NO_ANGLE_SENTINEL = "NO_ANGLE"


def parse_message(message: "Message") -> DraftContent:
    """Validate one API response into `DraftContent`, or raise.

    Check `stop_reason` BEFORE reading content. A refusal returns HTTP 200 with
    an ordinary-looking message; reading `.content[0].text` without checking is
    how a refusal becomes a silently empty email.
    """

    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None)
        raise ComposerRefused(
            f"model declined to draft (category={category!r}). No draft written."
        )
    if message.stop_reason == "max_tokens":
        raise ComposerTruncated(
            f"generation hit max_tokens={MAX_TOKENS}; structured output is incomplete"
        )

    text = next((b.text for b in message.content if b.type == "text"), None)
    if not text:
        raise ComposerTruncated(
            f"no text block in response (stop_reason={message.stop_reason!r})"
        )
    content = DraftContent.model_validate_json(text)
    return content.model_copy(
        update={
            "subject": _decode_stray_escapes(content.subject, "subject"),
            "body_text": _decode_stray_escapes(content.body_text, "body_text"),
        }
    )


#: A `\uXXXX` escape that survived JSON decoding, i.e. one the model wrote with
#: the backslash itself escaped. Deliberately narrow: it matches the literal
#: six characters, not any backslash.
_STRAY_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_stray_escapes(text: str, field: str) -> str:
    """Repair a double-encoded character before it becomes an email.

    Observed on the first live run: one draft of four came back with the six
    literal characters `\\u2014` where an em dash belonged, mid-sentence — the
    model escaped its own escape, and structured output validated it happily
    because a string containing a backslash is a perfectly good string. The
    other three drafts in the same batch carried proper em dashes, so this is
    model inconsistency, not a schema or a client bug.

    It has to be fixed HERE rather than in the verifier, and the reason is worth
    stating: the copy was broken regardless of what verification said about it.
    `\\u2014` is unsendable text that a reviewer might well approve past. That it
    also produced a nonsense `number '2014'` flag — the digits inside the escape
    read as a year — is the smaller half of the problem.

    Logged at WARNING every time it fires. A silent repair would hide a change
    in the model's behaviour, and this function should be able to go away.
    """

    if "\\u" not in text:
        return text
    repaired = _STRAY_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    if repaired != text:
        log.warning(
            "composer returned double-encoded escapes in %s; decoded %d of them "
            "(the model escaped its own backslash)",
            field,
            len(_STRAY_ESCAPE_RE.findall(text)),
        )
    return repaired


# --------------------------------------------------------------------------
# Cost accounting — the point of the exercise
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts for one call or one batch, plus what they cost.

    This exists because the cost-per-usable-lead figures in the gap analysis
    are PROJECTED. Nothing in the repo has ever measured them. Accumulating
    this per batch is what turns that table into an observed number.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens
            + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
        )

    @property
    def total_input_tokens(self) -> int:
        """`input_tokens` is the UNCACHED remainder, not the prompt size."""

        return (
            self.input_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    @property
    def cache_hit_rate(self) -> float:
        """Share of prompt tokens served from cache. The number to watch.

        If this sits near zero across a run, something upstream is
        invalidating the prefix and the caching is costing money rather than
        saving it. See `_system_blocks`.
        """

        total = self.total_input_tokens
        return self.cache_read_input_tokens / total if total else 0.0


def usage_of(message: "Message") -> Usage:
    u = message.usage
    return Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def price_usd(usage: Usage, *, model: str | None = None, batch: bool = False) -> float:
    """What that usage cost, in dollars, at list price.

    An estimate for reporting, not an invoice. Cache writes are priced at the
    1-hour multiplier because that is the TTL this module requests.
    """

    model = model or _model()
    try:
        in_price, out_price = PRICE_PER_MTOK[model]
    except KeyError:
        log.warning("no price entry for model %r; reporting 0.0", model)
        return 0.0

    per_mtok = (
        usage.input_tokens * in_price
        + usage.cache_read_input_tokens * in_price * CACHE_READ_MULTIPLIER
        + usage.cache_creation_input_tokens * in_price * CACHE_WRITE_MULTIPLIER
        + usage.output_tokens * out_price
    )
    total = per_mtok / 1_000_000
    return total * BATCH_DISCOUNT if batch else total


# --------------------------------------------------------------------------
# Client, prewarm, and the synchronous composer
# --------------------------------------------------------------------------


def get_client() -> "Anthropic":
    """The SDK client. Imported lazily so `anthropic` stays an optional dep.

    Credentials resolve from the environment the SDK's own way — an unset
    `ANTHROPIC_API_KEY` does not mean there are none (an `ant auth login`
    profile also works), so we do not gate on the setting here. What DOES gate
    is `default_composer()` in `draft.py`, which keeps the deterministic
    template in force until drafting is explicitly switched on.
    """

    import anthropic

    key = str(setting("ANTHROPIC_API_KEY", "") or "")
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def assert_cacheable_prefix(client: "Anthropic" | None = None) -> int:
    """Fail loudly if the frozen prompt is too short to cache on this model.

    Below the model's minimum the API creates no entry and reports no error;
    you simply pay full price forever. That failure is invisible in every
    signal except the bill, so it gets an explicit check rather than a comment.

    Returns the measured prefix size in tokens.
    """

    client = client or get_client()
    model = _model()
    counted = client.messages.count_tokens(
        model=model,
        system=[{"type": "text", "text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": "x"}],
    )
    minimum = MIN_CACHEABLE_PREFIX_TOKENS.get(model)
    if minimum is None:
        log.warning("no cache minimum known for model %r; skipping check", model)
        return counted.input_tokens
    if counted.input_tokens < minimum:
        raise RuntimeError(
            f"system prefix is {counted.input_tokens} tokens but {model} needs "
            f"{minimum} to cache at all. The API will not error — it will "
            f"silently never create a cache entry. Lengthen SYSTEM_PROMPT or "
            f"choose a model with a lower minimum."
        )
    return counted.input_tokens


def prewarm_cache(client: "Anthropic" | None = None) -> Usage:
    """Write the cache entry BEFORE a batch reads it. Call this first.

    This is the non-obvious half of batching-plus-caching, and skipping it
    costs most of the saving:

        A cache entry becomes readable only once the writing request has begun
        responding. N requests with an identical prefix submitted at once all
        miss — none of them can read what the others are still writing.

    A batch is exactly that fan-out. Without a prewarm, a meaningful share of
    the batch pays full price for the prefix and then ALSO pays the write
    premium, which is worse than not caching at all.

    So: one `max_tokens=0` request up front. The API runs prefill, writes the
    entry at our breakpoint, and returns immediately with no content and no
    output tokens billed. By the time the batch runs, the entry exists.

    Two constraints the API imposes here, both of which shape this code:
      * `max_tokens=0` is rejected INSIDE a batch request, so the prewarm is a
        separate ordinary call.
      * `max_tokens=0` is also rejected alongside `output_config.format`, so
        this request omits the output config. Only `system` (and `tools`) are
        part of the cached prefix, so the entry it writes is the one the batch
        reads — but that is precisely the kind of assumption that rots
        silently, which is why `draft_batch` asserts a nonzero
        `cache_read_input_tokens` on the results.
    """

    client = client or get_client()
    message = client.messages.create(
        model=_model(),
        max_tokens=0,
        system=_system_blocks(),
        messages=[{"role": "user", "content": "warmup"}],
    )
    usage = usage_of(message)
    log.info(
        "prewarmed draft cache: wrote %d tokens, read %d",
        usage.cache_creation_input_tokens,
        usage.cache_read_input_tokens,
    )
    return usage


def compose_claude(request: DraftRequest) -> DraftContent:
    """`Composer` implementation: one lead, one synchronous call.

    This is the seam's signature, and it is the RIGHT path for exactly one
    thing — a reviewer clicking "redraft" in the review app, where a person is
    waiting and a batch would take up to 24 hours.

    It is the WRONG path for the pipeline, which should go through
    `draft_batch` and pay half. `handle_draft` keeps working through this
    function so that nothing breaks when batching is off; see
    `draft.default_composer`.
    """

    client = get_client()
    message = client.messages.create(**build_params(request))
    content = parse_message(message)
    usage = usage_of(message)
    log.info(
        "drafted lead %s: %d in (%d cached, %.0f%%), %d out, ~$%.4f",
        request.lead_id,
        usage.total_input_tokens,
        usage.cache_read_input_tokens,
        usage.cache_hit_rate * 100,
        usage.output_tokens,
        price_usd(usage),
    )
    return content
