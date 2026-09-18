"""Measure the verifier against REAL model output. Bounded, batched, costed.

    .venv/bin/python scripts/verifier_live_eval.py plan
    .venv/bin/python scripts/verifier_live_eval.py submit --yes
    .venv/bin/python scripts/verifier_live_eval.py collect
    .venv/bin/python scripts/verifier_live_eval.py report --adjudicated <file>

--------------------------------------------------------------------------
WHY THIS EXISTS
--------------------------------------------------------------------------
`tests/fixtures/verification_eval.json` is 63 labelled drafts and the verifier
scores 0% false positives and 0% false negatives on it. That number is a
REGRESSION BASELINE and nothing more: every case in it was hand-written, by the
same person who wrote the verifier, in the shape that person expected a model to
write. It is not an unbiased sample of the paraphrase `compose_claude` actually
produces, and the false-positive rate on that paraphrase is the one number
standing between here and `DRAFT_COMPOSER=claude`.

A false positive here is not a cosmetic defect. Verification is a compliance
control that fails closed: a held draft is never auto-approvable. If real model
copy is held at, say, 30%, then switching the composer on quietly turns a third
of the client's pipeline into a queue of drafts a reviewer has to overrule — and
a control a reviewer routinely overrules has stopped being a control.

--------------------------------------------------------------------------
WHAT IT MEASURES, AND WHAT IT HONESTLY CANNOT
--------------------------------------------------------------------------
It measures, exactly:

  * the HOLD RATE on real output — of N drafts the model wrote from real cached
    facts, how many did the verifier refuse — plus, for each hold, the specific
    claim that could not be traced;
  * measured cost: tokens and dollars, recorded on `draft_batches` the same way
    a production run records them, per draft and per usable lead;
  * whether prompt caching actually engaged.

It CANNOT, by itself, split the hold rate into false positives and true catches.
A held draft is one of two things: legitimate copy the verifier failed to trace
(a false positive), or the model genuinely fabricating (a true catch, and the
most valuable result this harness can produce). Telling those apart is a human
reading the claim against the evidence. So the hold rate is reported as the
UPPER BOUND on the false-positive rate, and `collect` writes an adjudication
file with one line per draft — the copy, the cited evidence, the untraceable
token and its context — for a human to label. `report --adjudicated` then turns
those labels into the false-positive rate and compares it to the labelled set's.

Anything else would be inventing the number this whole exercise exists to stop
inventing.

--------------------------------------------------------------------------
SPEND CONTROL
--------------------------------------------------------------------------
* `--cap` bounds the number of leads (default 25, hard ceiling `MAX_CAP`).
* `plan` makes no billable call at all and prints the estimate.
* `submit` prints the estimate and refuses to send without `--yes`.
* `no_angle` leads are settled inline by `score` and never enter the batch, so
  they cost nothing — the same rule `draft_batch.build_batch` follows.
* Everything goes through the Batch API (50% off) and the existing prewarmed
  prompt-cache path, so the dollars this prints are production's dollars and
  not a sync-call approximation of them.

--------------------------------------------------------------------------
WHAT IT DOES NOT TOUCH
--------------------------------------------------------------------------
No `drafts` row, no lead status, no queue job, no approval, no send. It reads
leads and `research_cache`, and it writes one `draft_batches` row — the table
that exists to record what AI costs. A measurement that mutated the pipeline
would be a second drafting run racing the real one.

`DRAFT_COMPOSER` is not read and not changed. This harness calls the model
directly; the flag stays `template` until this measurement says otherwise.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import select

from mazz.models import DraftBatch, Lead, ResearchCache
from mazz.db import get_sessionmaker
from mazz.pipeline.compose_claude import (
    MAX_TOKENS,
    NO_ANGLE_SENTINEL,
    SYSTEM_PROMPT,
    ComposerRefused,
    Usage,
    assert_cacheable_prefix,
    build_params,
    get_client,
    parse_message,
    prewarm_cache,
    price_usd,
    usage_of,
)
from mazz.pipeline.draft import build_request, render_body
from mazz.pipeline.score import facts_for_lead, score_lead
from mazz.pipeline.verify import verify_content

log = logging.getLogger("verifier_live_eval")

#: Hard ceiling on `--cap`. A typo'd digit is the only realistic way this run
#: costs real money, so the ceiling is enforced in code and not in a docstring.
MAX_CAP = 200

#: Default lead cap. Sized for a first, cheap, informative run — see
#: `estimate_cost`, which prints what it will actually cost before anything is
#: submitted.
DEFAULT_CAP = 25

#: Output tokens assumed per draft when ESTIMATING, before anything real is
#: known. A 90-130 word body is ~200 tokens; the rest is adaptive thinking.
#: Deliberately generous. The estimate also prints the true worst case, which is
#: every request running to `MAX_TOKENS`.
ASSUMED_OUTPUT_TOKENS = 1200

#: Where run state and adjudication files land. Not in the repo tree by default.
DEFAULT_OUT_DIR = Path("var/verifier-live-eval")


# --------------------------------------------------------------------------
# Selecting the leads
# --------------------------------------------------------------------------


def select_leads(session, *, cap: int) -> list[Lead]:
    """Up to `cap` real leads that have cached research, colleagues first.

    Ordered so that domains with more than one lead come first. That is not a
    convenience: the cache assertion in `collect` needs a SECOND lead at a
    shared domain, because two colleagues share a research_cache row (the
    dedup that makes 50 prospects at one employer cost one research operation)
    and so produce two requests whose only difference is the recipient block.
    If the prompt cache is going to work anywhere, it works there.

    Deterministic: two `plan` runs over an unchanged database select the same
    leads, so an estimate is about the run that is actually submitted.
    """

    rows = (
        session.execute(
            select(Lead)
            .join(ResearchCache, ResearchCache.company_domain == Lead.company_domain)
            .order_by(Lead.company_domain, Lead.id)
        )
        .scalars()
        .all()
    )

    by_domain: dict[str, list[Lead]] = defaultdict(list)
    for lead in rows:
        by_domain[lead.company_domain].append(lead)

    groups = sorted(by_domain.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    selected: list[Lead] = []
    for _domain, leads in groups:
        for lead in leads:
            if len(selected) >= cap:
                return selected
            selected.append(lead)
    return selected


def shared_domain_pairs(leads: Sequence[Lead]) -> dict[str, list[int]]:
    """domain -> lead ids, for every domain contributing two or more leads."""

    by_domain: dict[str, list[int]] = defaultdict(list)
    for lead in leads:
        by_domain[lead.company_domain].append(lead.id)
    return {d: ids for d, ids in by_domain.items() if len(ids) > 1}


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def custom_id_for(lead: Lead) -> str:
    """`custom_id` keyed by LEAD, not by job.

    Batch results come back in arbitrary order and must be keyed by custom_id.
    This harness claims no queue jobs — it is a measurement, not a second
    drafting run — so the lead id is the unit of work.
    """

    return f"evallead-{lead.id}"


def lead_id_from_custom_id(custom_id: str) -> int | None:
    prefix, _, raw = custom_id.partition("-")
    if prefix != "evallead" or not raw.isdigit():
        return None
    return int(raw)


@dataclass(slots=True)
class EvalPlan:
    """What the run intends to send, and what `score` already settled for free."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    lead_ids: list[int] = field(default_factory=list)
    no_angle: list[int] = field(default_factory=list)
    considered: int = 0

    def __bool__(self) -> bool:
        return bool(self.requests)


def build_plan(session, leads: Sequence[Lead], *, now: datetime | None = None) -> EvalPlan:
    """Score each lead against ITS OWN campaign config, then batch the rest.

    `score_lead(config=None)` resolves the lead's campaign, exactly as
    `draft_batch.build_batch` does. Scoring a measurement batch against one
    config would score one agency-client's prospects against another's ICP and
    change which leads the model is even asked about.

    A `no_angle` lead is settled here and never batched. It is the largest
    single saving in the cost model and the easiest one to undo by accident.
    """

    plan = EvalPlan(considered=len(leads))
    for lead in leads:
        result = score_lead(session, lead, now=now)
        if not result.has_angle:
            plan.no_angle.append(lead.id)
            continue
        params = build_params(build_request(lead, result))
        plan.requests.append({"custom_id": custom_id_for(lead), "params": params})
        plan.lead_ids.append(lead.id)
    return plan


# --------------------------------------------------------------------------
# Estimating, before spending
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Estimate:
    requests: int
    prefix_tokens: int
    suffix_tokens: int
    expected_usd: float
    worst_case_usd: float
    counted_exactly: bool

    def render(self) -> str:
        how = "counted by the API" if self.counted_exactly else "estimated offline (chars/4)"
        return (
            f"  requests to submit       : {self.requests}\n"
            f"  cached prefix            : {self.prefix_tokens} tokens ({how})\n"
            f"  per-lead content         : {self.suffix_tokens} tokens total\n"
            f"  assumed output           : {ASSUMED_OUTPUT_TOKENS} tokens/draft\n"
            f"  ESTIMATED COST           : ${self.expected_usd:.4f}\n"
            f"  worst case (all hit {MAX_TOKENS}) : ${self.worst_case_usd:.4f}"
        )


def _offline_tokens(text: str) -> int:
    """A crude, deliberately pessimistic token count for when there is no key."""

    return max(1, len(text) // 4)


def estimate_cost(plan: EvalPlan, *, model: str, client: Any | None = None) -> Estimate:
    """What this run will cost, priced the way the run will actually be billed.

    Three token classes, and they are priced differently, so an estimate that
    lumps them together is wrong by more than the thing it is estimating:

      * the prewarm writes the prefix ONCE, at the 1h-TTL write premium (2x),
        as an ordinary call — a prewarm cannot be a batch request;
      * every batched request then READS that prefix at 0.1x, batch-discounted;
      * only the per-lead suffix and the output are paid in full (at 50% off).

    `client=None` estimates offline at ~4 chars/token so `plan` can run with no
    credentials at all. With a client it uses `count_tokens`, which is free.
    """

    prompts = [str(item["params"]["messages"][0]["content"]) for item in plan.requests]

    if client is not None:
        prefix_tokens = assert_cacheable_prefix(client)
        suffix_tokens = sum(
            client.messages.count_tokens(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            ).input_tokens
            for prompt in prompts
        )
        counted = True
    else:
        prefix_tokens = _offline_tokens(SYSTEM_PROMPT)
        suffix_tokens = sum(_offline_tokens(prompt) for prompt in prompts)
        counted = False

    n = len(plan.requests)
    prewarm = price_usd(
        Usage(cache_creation_input_tokens=prefix_tokens), model=model, batch=False
    )
    body = price_usd(
        Usage(
            input_tokens=suffix_tokens,
            cache_read_input_tokens=prefix_tokens * n,
            output_tokens=ASSUMED_OUTPUT_TOKENS * n,
        ),
        model=model,
        batch=True,
    )
    worst = price_usd(
        Usage(
            input_tokens=suffix_tokens,
            cache_read_input_tokens=prefix_tokens * n,
            output_tokens=MAX_TOKENS * n,
        ),
        model=model,
        batch=True,
    )
    return Estimate(
        requests=n,
        prefix_tokens=prefix_tokens,
        suffix_tokens=suffix_tokens,
        expected_usd=prewarm + body,
        worst_case_usd=prewarm + worst,
        counted_exactly=counted,
    )


# --------------------------------------------------------------------------
# Submitting
# --------------------------------------------------------------------------


def _batch_request(item: dict[str, Any]) -> Any:
    """One `{custom_id, params}` entry in the SDK's own types where available.

    Same shape and same fallback as `draft_batch._batch_request`: the dicts are
    TypedDicts, so this is the identical runtime object either way, and the
    fallback keeps the module importable without the `anthropic` package.
    """

    try:
        from anthropic.types.message_create_params import (
            MessageCreateParamsNonStreaming,
        )
        from anthropic.types.messages.batch_create_params import Request
    except ImportError:  # pragma: no cover - exercised when the SDK is absent
        return {"custom_id": item["custom_id"], "params": item["params"]}

    return Request(
        custom_id=item["custom_id"],
        params=MessageCreateParamsNonStreaming(**item["params"]),
    )


def submit(session, plan: EvalPlan, *, client: Any, prewarm: bool = True) -> DraftBatch:
    """Prewarm the cache, send the batch, and record the tracking row.

    The prewarm is load-bearing and is the same one `draft_batch.submit_batch`
    does: a cache entry only becomes readable once the request writing it has
    begun responding, so a fan-out of identical-prefix requests submitted at
    once would otherwise all MISS and all pay the write premium. Skipping it
    here would also make the cost this harness reports unrepresentative of
    production, which is half the point of the run.
    """

    if not plan:
        raise SystemExit("nothing to submit: every selected lead scored no_angle")

    if prewarm:
        prewarm_cache(client)

    batch = client.messages.batches.create(
        requests=[_batch_request(item) for item in plan.requests]
    )
    row = DraftBatch(
        batch_id=batch.id,
        model=str(plan.requests[0]["params"]["model"]),
        status=batch.processing_status,
        # Deliberately empty: this run claims no queue jobs. The reconciliation
        # list lives in the run manifest, keyed by lead.
        job_ids=[],
        request_count=len(plan.requests),
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------
# Collecting, verifying, measuring
# --------------------------------------------------------------------------


@dataclass(slots=True)
class CaseOutcome:
    """One real draft, its verdict, and everything a human needs to adjudicate."""

    lead_id: int
    company_domain: str
    subject: str = ""
    body: str = ""
    signals_used: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    status: str = "drafted"  # drafted | held | no_angle | refused | error
    untraceable: list[dict[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: str = ""
    cache_read_input_tokens: int = 0
    #: Filled in by a human in the adjudication file. "legitimate" means the
    #: verifier was wrong (a false positive); "fabricated" means it was right.
    verdict: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "company_domain": self.company_domain,
            "status": self.status,
            "subject": self.subject,
            "body": self.body,
            "signals_used": self.signals_used,
            "evidence": self.evidence,
            "untraceable": self.untraceable,
            "problems": self.problems,
            "verifier_notes": self.notes,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "verdict": self.verdict,
        }


@dataclass(slots=True)
class EvalReport:
    batch_id: str
    model: str
    cases: list[CaseOutcome] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    no_angle: int = 0
    cache_failure: str | None = None

    @property
    def drafted(self) -> list[CaseOutcome]:
        """Drafts the model actually returned — held ones included."""

        return [c for c in self.cases if c.status in ("drafted", "held")]

    @property
    def held(self) -> list[CaseOutcome]:
        return [c for c in self.cases if c.status == "held"]

    @property
    def refused(self) -> list[CaseOutcome]:
        return [c for c in self.cases if c.status == "refused"]

    @property
    def errored(self) -> list[CaseOutcome]:
        return [c for c in self.cases if c.status == "error"]

    @property
    def hold_rate(self) -> float:
        drafted = self.drafted
        return len(self.held) / len(drafted) if drafted else 0.0

    @property
    def usable(self) -> int:
        """Drafts that passed verification: the only ones a human can approve."""

        return len([c for c in self.drafted if c.status == "drafted"])

    @property
    def cost_per_usable_lead(self) -> float | None:
        """The figure the gap analysis projects at $0.19 / $0.11 / $0.064.

        Denominator is drafts that PASSED verification, because a held draft is
        not a usable lead — it is a lead a human now has to adjudicate.

        ``None`` when nothing passed. That case is UNDEFINED, not free, and it
        returned 0.0 until the first live run printed `PER USABLE LEAD :
        $0.00000` underneath a 100% hold rate — the best-looking number on the
        page attached to the worst possible outcome, in a project whose primary
        objective is cost per usable lead. Spending real money and getting
        nothing usable is infinite cost per usable lead, and the readout says so
        in words rather than rounding it to zero.
        """

        return self.cost_usd / self.usable if self.usable else None

    @property
    def cost_per_usable_lead_display(self) -> str:
        value = self.cost_per_usable_lead
        if value is None:
            return (
                f"undefined — 0 of {len(self.drafted)} drafts passed, and "
                f"${self.cost_usd:.4f} bought no usable lead"
            )
        return f"${value:.5f}"


def _evidence_for(facts: Iterable[Any], cited: Sequence[str]) -> list[dict[str, str]]:
    wanted = set(cited)
    return [
        {"id": f.id, "kind": f.kind, "content": f.content, "source_url": f.source_url or ""}
        for f in facts
        if f.id in wanted
    ]


def collect(session, row: DraftBatch, *, client: Any) -> EvalReport | None:
    """Land a finished batch: verify every draft, price every token.

    Returns None while the batch is still processing. Writes nothing but the
    `draft_batches` row — no drafts, no lead statuses, no jobs.
    """

    report = EvalReport(batch_id=row.batch_id, model=row.model)
    batch = client.messages.batches.retrieve(row.batch_id)
    row.status = batch.processing_status
    if batch.processing_status != "ended":
        return None

    for result in client.messages.batches.results(row.batch_id):
        lead_id = lead_id_from_custom_id(result.custom_id)
        lead = session.get(Lead, lead_id) if lead_id is not None else None
        if lead is None:
            log.warning("batch %s returned unknown custom_id %r", row.batch_id, result.custom_id)
            continue
        report.cases.append(_land(session, lead, result, report))

    row.succeeded_count = len(report.drafted)
    row.errored_count = len(report.refused) + len(report.errored)
    row.usage = {
        "input_tokens": report.usage.input_tokens,
        "output_tokens": report.usage.output_tokens,
        "cache_read_input_tokens": report.usage.cache_read_input_tokens,
        "cache_creation_input_tokens": report.usage.cache_creation_input_tokens,
    }
    row.cost_usd = report.cost_usd
    row.ended_at = datetime.now(timezone.utc)

    report.cache_failure = check_cache_engaged(report)
    if report.cache_failure:
        row.last_error = report.cache_failure
    session.flush()
    return report


def _land(session, lead: Lead, result: Any, report: EvalReport) -> CaseOutcome:
    """One batch result -> one verified case."""

    case = CaseOutcome(lead_id=lead.id, company_domain=lead.company_domain or "")
    kind = result.result.type

    if kind != "succeeded":
        case.status = "error"
        case.notes = f"batch result {kind}: {getattr(result.result, 'error', None)}"
        return case

    message = result.result.message
    usage = usage_of(message)
    case.cache_read_input_tokens = usage.cache_read_input_tokens
    report.usage = report.usage + usage
    report.cost_usd += price_usd(usage, model=report.model, batch=True)

    try:
        content = parse_message(message)
    except ComposerRefused as exc:
        # NEVER no_angle. "There was no reason to reach out" and "we never got
        # an answer" are different facts about this lead, and collapsing them
        # tells the client the first when the truth is the second. In the
        # pipeline this path is `fail_job`; here it fails the run.
        case.status = "refused"
        case.notes = str(exc)
        return case
    except Exception as exc:  # truncated, or invalid against the schema
        case.status = "error"
        case.notes = f"{type(exc).__name__}: {exc}"
        return case

    if content.body_text.strip() == NO_ANGLE_SENTINEL:
        case.status = "no_angle"
        case.notes = "model found no specific angle in the qualifying facts"
        report.no_angle += 1
        return case

    facts = facts_for_lead(session, lead)
    body = render_body(content.body_text, lead)

    # The same set-runner the labelled eval set uses: `verify_content` with the
    # full rendered body (footer included, because a missing footer is also a
    # compliance failure), the ids the composer claims to have used, and the
    # whole cached corpus as available evidence.
    verdict = verify_content(
        lead=lead,
        subject=content.subject,
        body=body,
        signals_used=list(content.signals_used),
        available_facts=facts,
    )

    case.subject = content.subject
    case.body = body
    case.signals_used = list(content.signals_used)
    case.evidence = _evidence_for(facts, content.signals_used)
    case.notes = verdict.notes
    case.untraceable = [
        {"kind": c.kind, "token": c.token, "where": c.where, "context": c.context}
        for c in verdict.untraceable
    ]
    case.problems = list(verdict.problems)
    case.status = "drafted" if verdict.passed else "held"
    return case


def check_cache_engaged(report: EvalReport) -> str | None:
    """Did prompt caching actually do anything? Returns a failure line, or None.

    The specific thing being caught: a `datetime.now()`, an unsorted
    `json.dumps()`, a feature flag — anything per-request that drifts into the
    frozen prefix — silently disables caching. There is no error and no wrong
    output. The only signal is the bill, months later.

    The test is `cache_read_input_tokens > 0` on the SECOND request at a shared
    domain, which is the strongest case there is: two colleagues share a
    research_cache row, so their two requests differ only in the recipient
    block and are the likeliest pair in the whole run to share a prefix. If
    caching does not engage there it is not engaging anywhere.
    """

    cases = [c for c in report.cases if c.status in ("drafted", "held", "no_angle")]
    if len(cases) < 2:
        return None

    by_domain: dict[str, list[CaseOutcome]] = defaultdict(list)
    for case in cases:
        by_domain[case.company_domain].append(case)
    colleagues = [group for group in by_domain.values() if len(group) > 1]

    if colleagues:
        group = sorted(colleagues, key=lambda g: -len(g))[0]
        later = sorted(group, key=lambda c: c.lead_id)[1:]
        if any(c.cache_read_input_tokens > 0 for c in later):
            return None
        scope = (
            f"the {len(later)} later lead(s) at {group[0].company_domain}"
        )
    else:
        if any(c.cache_read_input_tokens > 0 for c in cases):
            return None
        scope = f"all {len(cases)} requests"

    return (
        f"PROMPT CACHING DID NOT ENGAGE: cache_read_input_tokens == 0 across {scope}. "
        "Every lead paid full price for the system prompt, and the prewarm paid the "
        "write premium on top. Check that nothing per-request has drifted into "
        "SYSTEM_PROMPT / _system_blocks (a date, an unsorted json.dumps, a flag), "
        "that the prewarm ran, and that the prefix clears the model's minimum "
        "cacheable length."
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

#: Verdict labels a human writes into the adjudication file.
LEGITIMATE = "legitimate"  # the copy was supported; the verifier was WRONG -> FP
FABRICATED = "fabricated"  # the model really did invent it; the verifier was RIGHT


def render_report(report: EvalReport, *, cap: int, considered: int) -> str:
    lines = [
        "",
        "=" * 74,
        f"  REAL-OUTPUT VERIFIER EVAL — batch {report.batch_id} ({report.model})",
        "=" * 74,
        f"  leads considered         : {considered} (cap {cap})",
        f"  settled no_angle, free   : {report.no_angle}",
        f"  drafts returned          : {len(report.drafted)}",
        f"  passed verification      : {report.usable}",
        f"  HELD by the verifier     : {len(report.held)} = {report.hold_rate:.1%}",
        "",
        "  A hold is a false positive OR a real fabrication caught. This number is",
        "  therefore the UPPER BOUND on the false-positive rate, not the rate.",
        "  Adjudicate the file below and run `report --adjudicated` for the rate.",
        "",
        f"  refusals (run failed)    : {len(report.refused)}",
        f"  other errors             : {len(report.errored)}",
        "",
        "  --- cost, measured -------------------------------------------------",
        f"  input (uncached)         : {report.usage.input_tokens:,} tokens",
        f"  read from cache          : {report.usage.cache_read_input_tokens:,} tokens",
        f"  written to cache         : {report.usage.cache_creation_input_tokens:,} tokens",
        f"  output                   : {report.usage.output_tokens:,} tokens",
        f"  prompt tokens cached     : {report.usage.cache_hit_rate:.0%}",
        f"  TOTAL                    : ${report.cost_usd:.4f}",
        f"  per draft returned       : ${report.cost_usd / max(len(report.drafted), 1):.5f}",
        f"  PER USABLE LEAD          : {report.cost_per_usable_lead_display}",
        "",
        "  Drafting only. The gap analysis' $0.19 / $0.11 / $0.064 per usable lead",
        "  also carries research, mailboxes and compute; this is the AI-drafting",
        "  line of it, measured rather than projected.",
    ]
    if report.cache_failure:
        lines += ["", f"  !! {report.cache_failure}"]
    for case in report.held:
        claims = ", ".join(f"{c['kind']} {c['token']!r}" for c in case.untraceable)
        lines.append(f"  held  lead {case.lead_id} ({case.company_domain}): {claims or case.problems}")
    for case in report.refused:
        lines.append(f"  REFUSED lead {case.lead_id}: {case.notes}")
    lines.append("=" * 74)
    return "\n".join(lines)


def write_adjudication(report: EvalReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for case in report.drafted:
            record = case.as_json()
            record["verdict"] = None
            record["_how_to_label"] = (
                f"Read `body` against `evidence`. Set verdict to {LEGITIMATE!r} if every "
                f"claim really is supported (a verifier false positive), or {FABRICATED!r} "
                "if the model invented something (a true catch)."
            )
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def false_positive_rate(records: Sequence[dict]) -> tuple[int, int, list[dict]]:
    """(false positives, adjudicated total, unlabelled) over an adjudicated file.

    A false positive is a draft the verifier HELD that a human labelled
    legitimate. A draft that passed and was labelled `fabricated` is a false
    NEGATIVE and is reported separately and loudly — that side is the one the
    product exists to prevent, and the labelled set holds it at zero.
    """

    unlabelled = [r for r in records if r.get("verdict") not in (LEGITIMATE, FABRICATED)]
    labelled = [r for r in records if r.get("verdict") in (LEGITIMATE, FABRICATED)]
    fps = [r for r in labelled if r["status"] == "held" and r["verdict"] == LEGITIMATE]
    return len(fps), len(labelled), unlabelled


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _manifest_path(out_dir: Path) -> Path:
    return out_dir / "run.json"


def _save_manifest(out_dir: Path, data: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(out_dir)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _load_manifest(out_dir: Path) -> dict:
    path = _manifest_path(out_dir)
    if not path.exists():
        raise SystemExit(f"no run manifest at {path}; run `submit` first")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_plan(args, factory, client_factory) -> int:
    with factory() as session:
        leads = select_leads(session, cap=args.cap)
        if not leads:
            print("no leads with cached research; nothing to measure")
            return 1
        plan = build_plan(session, leads)
        shared = shared_domain_pairs(leads)
        model = (
            str(plan.requests[0]["params"]["model"]) if plan.requests else "claude-opus-5"
        )
        client = client_factory() if args.count_tokens else None
        estimate = estimate_cost(plan, model=model, client=client)

    print(f"\n  leads selected           : {len(leads)} (cap {args.cap})")
    print(f"  settled no_angle, free   : {len(plan.no_angle)}")
    print(f"  model                    : {model}")
    print(estimate.render())
    if shared:
        print(f"  shared-domain colleagues : {shared} (the cache assertion's subject)")
    else:
        print(
            "  !! no two selected leads share a company domain, so the "
            "cache-read assertion will fall back to the whole run"
        )
    if not plan.requests:
        print("\n  every selected lead scored no_angle — nothing to submit, nothing to spend")
        return 1
    return 0


def cmd_submit(args, factory, client_factory) -> int:
    with factory() as session:
        leads = select_leads(session, cap=args.cap)
        if not leads:
            print("no leads with cached research; nothing to measure")
            return 1
        plan = build_plan(session, leads)
        if not plan:
            print("every selected lead scored no_angle — nothing to submit")
            return 1
        model = str(plan.requests[0]["params"]["model"])
        client = client_factory()
        estimate = estimate_cost(plan, model=model, client=client if args.count_tokens else None)
        print(f"\n  about to submit {len(plan.requests)} requests on {model}")
        print(estimate.render())
        if not args.yes:
            print(
                "\n  refusing to submit without --yes. Nothing has been spent "
                "(count_tokens is free)."
            )
            return 2

        row = submit(session, plan, client=client)
        manifest = {
            "batch_id": row.batch_id,
            "model": row.model,
            "cap": args.cap,
            "considered": plan.considered,
            "lead_ids": plan.lead_ids,
            "no_angle_lead_ids": plan.no_angle,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "estimated_usd": estimate.expected_usd,
        }
        session.commit()

    path = _save_manifest(args.out_dir, manifest)
    print(f"\n  submitted {manifest['batch_id']} ({len(manifest['lead_ids'])} requests)")
    print(f"  manifest: {path}")
    print("  collect with: verifier_live_eval.py collect")
    return 0


def cmd_collect(args, factory, client_factory) -> int:
    manifest = _load_manifest(args.out_dir)
    batch_id = args.batch_id or manifest["batch_id"]
    client = client_factory()

    with factory() as session:
        row = session.execute(
            select(DraftBatch).where(DraftBatch.batch_id == batch_id)
        ).scalar_one_or_none()
        if row is None:
            raise SystemExit(f"no draft_batches row for {batch_id}")
        report = collect(session, row, client=client)
        if report is None:
            session.commit()
            print(f"{batch_id}: still {row.status}")
            return 3
        report.no_angle += len(manifest.get("no_angle_lead_ids", []))
        session.commit()

    out = write_adjudication(report, args.out_dir / f"adjudication-{batch_id}.jsonl")
    print(render_report(report, cap=manifest["cap"], considered=manifest["considered"]))
    print(f"\n  adjudication file: {out}")
    print(f"  label each line, then: verifier_live_eval.py report --adjudicated {out}")

    if report.refused:
        print("\n  FAILED: the model refused at least one request. Not no_angle — unknown.")
        return 1
    if report.cache_failure:
        return 1
    return 0


def cmd_report(args, factory, client_factory) -> int:
    path = Path(args.adjudicated)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    fps, labelled, unlabelled = false_positive_rate(records)
    held = [r for r in records if r["status"] == "held"]
    false_negatives = [
        r for r in records if r["status"] == "drafted" and r.get("verdict") == FABRICATED
    ]

    print(f"\n  drafts in file           : {len(records)}")
    print(f"  adjudicated              : {labelled} ({len(unlabelled)} still unlabelled)")
    print(f"  held by the verifier     : {len(held)}")
    if labelled:
        print(f"  FALSE POSITIVES          : {fps}/{labelled} = {fps / labelled:.1%}")
    print(
        f"  FALSE NEGATIVES          : {len(false_negatives)} "
        "(fabrication that passed — this side must be zero)"
    )
    for record in false_negatives:
        print(f"      ! lead {record['lead_id']}: {record['subject']!r}")
    if unlabelled:
        print("\n  incomplete: label every line before quoting the rate")
        return 2
    return 1 if false_negatives else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["plan", "submit", "collect", "report"])
    parser.add_argument(
        "--cap", type=int, default=DEFAULT_CAP,
        help=f"maximum leads to draft (hard ceiling {MAX_CAP})",
    )
    parser.add_argument("--yes", action="store_true", help="actually submit and spend")
    parser.add_argument("--batch-id", default=None, help="override the manifest's batch id")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--adjudicated", default=None, help="adjudication file for `report`")
    parser.add_argument(
        "--no-count-tokens",
        dest="count_tokens",
        action="store_false",
        help="estimate offline instead of calling the free count_tokens endpoint",
    )
    return parser


def main(argv: Sequence[str], *, factory=None, client_factory=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(list(argv))

    if args.cap < 1 or args.cap > MAX_CAP:
        raise SystemExit(f"--cap must be between 1 and {MAX_CAP}; got {args.cap}")
    if args.command == "report" and not args.adjudicated:
        raise SystemExit("report needs --adjudicated <file>")

    factory = factory or get_sessionmaker()
    client_factory = client_factory or get_client
    handler = {
        "plan": cmd_plan,
        "submit": cmd_submit,
        "collect": cmd_collect,
        "report": cmd_report,
    }[args.command]
    return handler(args, factory, client_factory)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main(sys.argv[1:]))
