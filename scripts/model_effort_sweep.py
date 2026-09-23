"""Sweep `DRAFT_MODEL` x `DRAFT_EFFORT` over REAL generation, and cost it.

    .venv/bin/python scripts/model_effort_sweep.py plan
    .venv/bin/python scripts/model_effort_sweep.py submit --yes
    .venv/bin/python scripts/model_effort_sweep.py collect
    .venv/bin/python scripts/model_effort_sweep.py table

--------------------------------------------------------------------------
WHY A SEPARATE DRIVER, AND WHY IT OWNS NO MEASUREMENT LOGIC
--------------------------------------------------------------------------
`verifier_live_eval` already does one configuration end to end — plan, price,
prewarm, batch, collect, verify, adjudicate — and it is the file that has been
reviewed for spend control. This driver calls THAT code once per cell and adds
nothing to it but a loop and a table. Nothing here re-implements pricing,
scoring, batching or verification; if a number differs between the two files,
this one is wrong.

--------------------------------------------------------------------------
WHAT A SWEEP CAN AND CANNOT ANSWER
--------------------------------------------------------------------------
It answers, per cell: measured dollars per draft, the verifier's hold rate on
that model's paraphrase, whether prompt caching engaged (`cache_read_input_
tokens > 0`), batch wall clock, and the drafts themselves.

It does NOT answer false-positive rate per cell. Splitting a hold into "the
verifier was wrong" and "the model fabricated" needs a human reading the claim
against the evidence, and no labelled live output exists for any of these
models. Each cell writes its adjudication file; the hold rate is reported as
the UPPER BOUND on false positives and labelled as such, exactly as the harness
docstring requires.

It also emits no quality score. A model grading its own copy is not a
measurement. `table --drafts` prints the bodies for a human, and the only
quality facts recorded are objectively checkable ones: which fact ids were
cited, the body word count against the 90-130 band, whether the model returned
`no_angle`, whether it refused.

--------------------------------------------------------------------------
THE CACHE TRAP THIS EXISTS TO MEASURE
--------------------------------------------------------------------------
`MIN_CACHEABLE_PREFIX_TOKENS` is not monotonic with price: haiku-4-5's floor is
4096 tokens against opus-5's 512. The frozen prefix is ~1441 tokens, so the
CHEAPEST model is the one that cannot cache it and pays it in full on every
request, while its output is 5x cheaper. Which way that nets out is an
empirical question about this workload's input:output ratio, so the cell is
submitted and measured rather than argued about. `assert_cacheable_prefix`
raises for such a model; `verifier_live_eval._prefix_tokens` catches it,
records it, and prices the cell without caching.

--------------------------------------------------------------------------
SPEND CONTROL
--------------------------------------------------------------------------
* `plan` is free (`count_tokens` is not billed) and prints a per-cell and
  total estimate.
* `submit` re-estimates, refuses without `--yes`, and refuses outright if the
  total exceeds `--budget` (default $5).
* `--cap` bounds leads per cell; `no_angle` leads never enter a batch.
* Every cell goes through the Batch API at 50% off.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import select

from mazz.config import reload_settings
from mazz.db import get_sessionmaker
from mazz.models import DraftBatch, Lead
from mazz.pipeline.draft import split_body

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verifier_live_eval as ev  # noqa: E402  (after the path insert above)

#: The grid. Effort is swept inside each model because it is a within-model
#: quality/cost trade; model is swept because the price table spans 5x.
MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")
EFFORTS = ("low", "medium", "high")

#: Pseudo-effort meaning "send no `output_config.effort` at all".
#:
#: Not a fourth point on the effort scale — an escape hatch for a model that
#: has no such scale. claude-haiku-4-5 rejects the parameter outright
#: (`adaptive thinking is not supported on this model`, 400, every request),
#: so all three of its (model, effort) cells are dead on arrival. Dropping the
#: model there would leave the cache-trap question — a model that cannot cache
#: the prefix but bills output at a fifth of opus — answered by arithmetic
#: instead of by `usage`. This cell exists to answer it with a measurement, and
#: is reported separately BECAUSE it is not comparable on the effort axis.
NO_EFFORT = "none"

#: Hard stop on the whole sweep, in dollars. Checked against the summed
#: estimate before a single request is submitted.
DEFAULT_BUDGET_USD = 5.00

DEFAULT_OUT_DIR = Path("var/sweep")

#: The band `SYSTEM_PROMPT` asks the model for. Reported as an objectively
#: checkable property of the output, NOT as a quality score.
WORD_BAND = (90, 130)


def cell_name(model: str, effort: str) -> str:
    return f"{model}__{effort}"


def cell_dir(out_dir: Path, model: str, effort: str) -> Path:
    return out_dir / cell_name(model, effort)


@dataclass(slots=True)
class Cell:
    model: str
    effort: str
    error: str | None = None
    estimate: ev.Estimate | None = None


def _pin(model: str, effort: str) -> None:
    """Pin this process's drafting config to one cell.

    `get_settings()` is `lru_cache`d, so setting the environment is not enough
    — `reload_settings()` is what makes `_model()` / `_effort()` see it. Doing
    this in-process (rather than per-cell subprocesses) keeps one database
    connection and one client for the whole sweep.
    """

    os.environ["DRAFT_MODEL"] = model
    os.environ["DRAFT_EFFORT"] = effort
    reload_settings()


def _strip_effort(plan: ev.EvalPlan) -> None:
    """Remove adaptive thinking from every built request.

    Both halves of it: `output_config.effort` AND the `thinking` block that
    `build_params` always sends. claude-haiku-4-5 rejects the feature outright
    — `adaptive thinking is not supported on this model`, 400 on every request,
    with the effort field alone removed — so measuring that model at all means
    sending a DIFFERENT request shape from the one the pipeline sends.

    That is the finding, not a workaround: running haiku in production would be
    a change to `build_params`, i.e. to the composer contract, not a change to
    `DRAFT_MODEL`. Done here on the built params so nothing outside this sweep
    sees it.
    """

    for item in plan.requests:
        item["params"].get("output_config", {}).pop("effort", None)
        item["params"].pop("thinking", None)


def plan_cell(session, cell: Cell, *, cap: int, client: Any | None) -> ev.EvalPlan:
    _pin(cell.model, cell.effort)
    leads = ev.select_leads(session, cap=cap)
    plan = ev.build_plan(session, leads)
    if plan.requests:
        got = str(plan.requests[0]["params"]["model"])
        if got != cell.model:
            raise SystemExit(
                f"config did not take: asked for {cell.model}, built {got}. "
                "Refusing to submit a mislabelled cell."
            )
        if cell.effort == NO_EFFORT:
            _strip_effort(plan)
        else:
            effort = plan.requests[0]["params"]["output_config"].get("effort")
            if effort != cell.effort:
                raise SystemExit(
                    f"config did not take: asked for effort {cell.effort}, built {effort}."
                )
    cell.estimate = ev.estimate_cost(plan, model=cell.model, client=client)
    return plan


def cmd_plan(args) -> int:
    factory = get_sessionmaker()
    client = ev.get_client() if args.count_tokens else None
    cells: list[Cell] = []
    with factory() as session:
        for model in args.models:
            for effort in args.efforts:
                cell = Cell(model=model, effort=effort)
                try:
                    plan = plan_cell(session, cell, cap=args.cap, client=client)
                except Exception as exc:  # a cell that cannot even be planned
                    cell.error = f"{type(exc).__name__}: {exc}"
                    print(f"  !! {cell_name(model, effort)}: {cell.error}")
                cells.append(cell)
    print_estimates(cells, plan)
    return 0


def print_estimates(cells: Sequence[Cell], plan: ev.EvalPlan) -> float:
    total = 0.0
    print(f"\n  requests per cell        : {len(plan.requests)}")
    print(f"  settled no_angle, free   : {len(plan.no_angle)} of {plan.considered} leads")
    print(f"\n  {'cell':<34} {'prefix':>7} {'cacheable':>10} {'est $':>9} {'worst $':>9}")
    print("  " + "-" * 72)
    for cell in cells:
        name = cell_name(cell.model, cell.effort)
        if cell.error or cell.estimate is None:
            print(f"  {name:<34} {'—':>7} {'—':>10} {'ERROR':>9}")
            continue
        est = cell.estimate
        total += est.expected_usd
        print(
            f"  {name:<34} {est.prefix_tokens:>7} "
            f"{('NO' if est.uncacheable else 'yes'):>10} "
            f"${est.expected_usd:>8.4f} ${est.worst_case_usd:>8.4f}"
        )
    print("  " + "-" * 72)
    print(f"  {'TOTAL ESTIMATE':<34} {'':>7} {'':>10} ${total:>8.4f}")
    uncacheable = {c.model for c in cells if c.estimate and c.estimate.uncacheable}
    for model in sorted(uncacheable):
        note = next(c.estimate.uncacheable for c in cells if c.model == model and c.estimate)
        print(f"\n  !! {model}: {note}")
    return total


def cmd_submit(args) -> int:
    factory = get_sessionmaker()
    client = ev.get_client()
    cells: list[Cell] = []
    plans: dict[str, ev.EvalPlan] = {}

    with factory() as session:
        for model in args.models:
            for effort in args.efforts:
                cell = Cell(model=model, effort=effort)
                try:
                    plans[cell_name(model, effort)] = plan_cell(
                        session, cell, cap=args.cap, client=client if args.count_tokens else None
                    )
                except Exception as exc:
                    cell.error = f"{type(exc).__name__}: {exc}"
                    print(f"  !! {cell_name(model, effort)}: {cell.error}")
                cells.append(cell)
        total = print_estimates(cells, next(iter(plans.values())))

    if total > args.budget:
        print(
            f"\n  REFUSING TO SUBMIT: estimate ${total:.4f} exceeds the "
            f"${args.budget:.2f} budget. Lower --cap or trim the grid."
        )
        return 2
    if not args.yes:
        print("\n  refusing to submit without --yes. Nothing spent (count_tokens is free).")
        return 2

    index: list[dict[str, Any]] = []
    for cell in cells:
        name = cell_name(cell.model, cell.effort)
        if cell.error:
            index.append({"cell": name, "model": cell.model, "effort": cell.effort,
                          "error": cell.error})
            continue
        _pin(cell.model, cell.effort)
        plan = plans[name]
        with factory() as session:
            # Re-plan against a live session: the plan above was built on a
            # session that has since closed, and its Lead objects with it.
            plan = ev.build_plan(session, ev.select_leads(session, cap=args.cap))
            if cell.effort == NO_EFFORT:
                _strip_effort(plan)
            # The prewarm is done HERE rather than inside `ev.submit` for two
            # reasons, both about honesty of the measured number:
            #   * a model that cannot cache this prefix has nothing to prewarm,
            #     and a prewarm call it cannot use is a billed no-op that
            #     production would never make;
            #   * `collect` prices batch results only, so the prewarm's dollars
            #     would otherwise vanish from the cell's cost. Captured here and
            #     carried in the manifest so `table` can add it back.
            prewarm_usd = 0.0
            prewarm_usage: dict[str, int] | None = None
            try:
                if not (cell.estimate and cell.estimate.uncacheable):
                    usage = ev.prewarm_cache(client)
                    prewarm_usd = ev.price_usd(usage, model=cell.model, batch=False)
                    prewarm_usage = {
                        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                        "cache_read_input_tokens": usage.cache_read_input_tokens,
                        "input_tokens": usage.input_tokens,
                    }
                row = ev.submit(session, plan, client=client, prewarm=False)
            except Exception as exc:
                session.rollback()
                cell.error = f"{type(exc).__name__}: {exc}"
                print(f"  !! {name}: submit failed: {cell.error}")
                index.append({"cell": name, "model": cell.model, "effort": cell.effort,
                              "error": cell.error})
                continue
            manifest = {
                "batch_id": row.batch_id,
                "model": cell.model,
                "effort": cell.effort,
                "cap": args.cap,
                "considered": plan.considered,
                "lead_ids": plan.lead_ids,
                "no_angle_lead_ids": plan.no_angle,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "estimated_usd": cell.estimate.expected_usd if cell.estimate else None,
                "uncacheable": cell.estimate.uncacheable if cell.estimate else None,
                "prewarm_usd": prewarm_usd,
                "prewarm_usage": prewarm_usage,
            }
            session.commit()
        ev._save_manifest(cell_dir(args.out_dir, cell.model, cell.effort), manifest)
        index.append({"cell": name, **{k: manifest[k] for k in
                                       ("batch_id", "model", "effort", "uncacheable")}})
        print(f"  submitted {name:<34} {manifest['batch_id']}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "sweep.json").write_text(
        json.dumps(index, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"\n  index: {args.out_dir / 'sweep.json'}")
    return 0


def _cells_from_index(out_dir: Path) -> list[dict[str, Any]]:
    path = out_dir / "sweep.json"
    if not path.exists():
        raise SystemExit(f"no sweep index at {path}; run `submit` first")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_status(args) -> int:
    client = ev.get_client()
    for entry in _cells_from_index(args.out_dir):
        if entry.get("error"):
            print(f"  {entry['cell']:<34} ERROR {entry['error']}")
            continue
        batch = client.messages.batches.retrieve(entry["batch_id"])
        counts = batch.request_counts
        print(
            f"  {entry['cell']:<34} {batch.processing_status:<12} "
            f"succeeded={counts.succeeded} processing={counts.processing} "
            f"errored={counts.errored} expired={counts.expired}"
        )
    return 0


def cmd_collect(args) -> int:
    """Land every ended cell. A still-running cell is left for the next run."""

    factory = get_sessionmaker()
    client = ev.get_client()
    pending = 0

    for entry in _cells_from_index(args.out_dir):
        name = entry["cell"]
        if entry.get("error"):
            print(f"  {name:<34} skipped: {entry['error']}")
            continue
        out = cell_dir(args.out_dir, entry["model"], entry["effort"])
        manifest = json.loads((out / "run.json").read_text(encoding="utf-8"))
        # Effort changes nothing about rendering or verification, but pin it
        # anyway so anything that reads settings during collect sees the cell
        # it is collecting.
        _pin(entry["model"], entry["effort"])
        with factory() as session:
            row = session.execute(
                select(DraftBatch).where(DraftBatch.batch_id == entry["batch_id"])
            ).scalar_one_or_none()
            if row is None:
                print(f"  {name:<34} no draft_batches row for {entry['batch_id']}")
                continue
            report = ev.collect(
                session, row, client=client,
                expected_uncacheable=manifest.get("uncacheable"),
            )
            if report is None:
                session.commit()
                print(f"  {name:<34} still {row.status}")
                pending += 1
                continue
            report.no_angle += len(manifest.get("no_angle_lead_ids", []))
            session.commit()

        path = ev.write_adjudication(report, out / f"adjudication-{entry['batch_id']}.jsonl")
        _save_cell_result(out, entry, report, prewarm_usd=manifest.get("prewarm_usd") or 0.0)
        print(ev.render_report(report, cap=manifest["cap"], considered=manifest["considered"]))
        print(f"  adjudication file: {path}")

    if pending:
        print(f"\n  {pending} cell(s) still processing; re-run `collect` later")
        return 3
    return 0


def _save_cell_result(
    out: Path, entry: dict, report: ev.EvalReport, *, prewarm_usd: float = 0.0
) -> Path:
    """Everything `table` needs, so summarising never re-hits the API."""

    data = {
        "cell": entry["cell"],
        "model": report.model,
        "effort": entry["effort"],
        "batch_id": report.batch_id,
        "drafts_returned": len(report.drafted),
        "passed": report.usable,
        "held": len(report.held),
        "hold_rate": report.hold_rate,
        "refused": len(report.refused),
        "errored": len(report.errored),
        "model_said_no_angle": len([c for c in report.cases if c.status == "no_angle"]),
        "cost_usd": report.cost_usd,
        "prewarm_usd": prewarm_usd,
        "cost_usd_with_prewarm": report.cost_usd + prewarm_usd,
        # Per draft INCLUDING the one-off prewarm, which at N=4 is a much
        # bigger share of the bill than it would be at production batch sizes.
        # `cost_per_draft_batch_only` is the figure that generalises.
        "cost_per_draft": (report.cost_usd + prewarm_usd) / max(len(report.drafted), 1),
        "cost_per_draft_batch_only": report.cost_usd / max(len(report.drafted), 1),
        "cost_per_usable_lead": report.cost_per_usable_lead,
        "usage": {
            "input_tokens": report.usage.input_tokens,
            "output_tokens": report.usage.output_tokens,
            "cache_read_input_tokens": report.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": report.usage.cache_creation_input_tokens,
        },
        "cache_hit": report.usage.cache_read_input_tokens > 0,
        "cache_failure": report.cache_failure,
        "wall_clock_seconds": report.wall_clock_seconds,
        "cases": [c.as_json() for c in report.cases],
    }
    path = out / "result.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _word_count(body: str) -> int:
    """Words in the body ABOVE the compliance footer.

    `render_body` appends the footer outside the composer, so counting it would
    charge the model for text it never wrote.
    """

    message, _footer = split_body(body)
    return len(message.split())


def cmd_table(args) -> int:
    rows = []
    for entry in _cells_from_index(args.out_dir):
        path = cell_dir(args.out_dir, entry.get("model", ""), entry.get("effort", "")) / "result.json"
        if not path.exists():
            print(f"  {entry['cell']:<34} not collected")
            continue
        rows.append(json.loads(path.read_text(encoding="utf-8")))

    if not rows:
        return 1

    print(f"\n  {'cell':<30} {'$/draft':>9} {'batch-only':>11} {'pass':>5} {'held':>5} "
          f"{'hold%':>6} {'cache':>6} {'cache_rd':>9} {'out tok':>8} {'wall':>8}")
    print("  " + "-" * 104)
    for r in sorted(rows, key=lambda r: r["cost_per_draft"]):
        print(
            f"  {r['cell'].replace('claude-', ''):<30} "
            f"${r['cost_per_draft']:>8.5f} "
            f"${r['cost_per_draft_batch_only']:>10.5f} "
            f"{r['passed']:>5} {r['held']:>5} {r['hold_rate'] * 100:>5.0f}% "
            f"{('yes' if r['cache_hit'] else 'NO'):>6} "
            f"{r['usage']['cache_read_input_tokens']:>9,} "
            f"{r['usage']['output_tokens']:>8,} "
            f"{(('%.0fm' % (r['wall_clock_seconds'] / 60)) if r['wall_clock_seconds'] else '?'):>8}"
        )
    print("  " + "-" * 104)
    print("  $/draft includes each cell's one-off prompt-cache prewarm; batch-only")
    print("  excludes it. At N=4 the prewarm is a large share of the bill and at")
    print("  production batch sizes it is amortised to nothing, so batch-only is the")
    print("  figure that generalises.")
    print("\n  hold% is the UPPER BOUND on the verifier's false-positive rate for that")
    print("  cell, not the rate: a hold is either a false positive or a real")
    print("  fabrication caught, and telling them apart needs a human on the")
    print("  adjudication file. No quality score is printed; use --drafts.")

    print("\n  --- cost per USABLE lead (drafts that passed verification) ----------")
    for r in sorted(rows, key=lambda r: r["cost_per_draft"]):
        usable = r["cost_per_usable_lead"]
        # None means nothing passed: undefined, not free. Spending money and
        # getting no approvable draft is infinite cost per usable lead.
        shown = f"${usable:.5f}" if usable is not None else (
            f"undefined — 0 of {r['drafts_returned']} passed"
        )
        print(f"  {r['cell'].replace('claude-', ''):<30} {shown}")

    print("\n  --- objectively checkable output properties ------------------------")
    print(f"  {'cell':<30} {'words/draft':>28} {'cited fact ids':>16} {'no_angle':>9} {'refused':>8}")
    for r in sorted(rows, key=lambda r: r["cost_per_draft"]):
        bodies = [c for c in r["cases"] if c["status"] in ("drafted", "held")]
        words = [_word_count(c["body"]) for c in bodies]
        in_band = sum(1 for w in words if WORD_BAND[0] <= w <= WORD_BAND[1])
        cites = sum(len(c["signals_used"]) for c in bodies)
        print(
            f"  {r['cell'].replace('claude-', ''):<30} "
            f"{str(words) + f' ({in_band}/{len(words)} in {WORD_BAND[0]}-{WORD_BAND[1]})':>28} "
            f"{cites:>16} {r['model_said_no_angle']:>9} {r['refused']:>8}"
        )

    if args.drafts:
        for r in sorted(rows, key=lambda r: r["cost_per_draft"]):
            print("\n" + "=" * 74)
            print(f"  DRAFTS — {r['cell']}")
            print("=" * 74)
            for case in r["cases"][: args.drafts]:
                print(f"\n  lead {case['lead_id']} ({case['company_domain']}) "
                      f"[{case['status']}] cites={case['signals_used']}")
                print(f"  subject: {case['subject']}")
                for line in (case["body"] or "").splitlines():
                    print(f"    {line}")
                if case["untraceable"]:
                    print(f"  UNTRACEABLE: {case['untraceable']}")
    return 0


def cmd_adopt(args) -> int:
    """Fold an ALREADY-PAID batch's raw results into the sweep as one cell.

    The opus-5 / default-effort batch was submitted before this sweep existed
    and its `draft_batches` row did not survive a database reset, so `collect`
    cannot land it. Re-submitting it would be paying twice for an answer
    already bought. This reads the raw results saved from the API — one JSON
    object per line, `custom_id` / `text` / `usage` — and puts them through the
    SAME verification and pricing path every other cell uses, so the row it
    produces is comparable rather than merely adjacent.

    The drafts are not re-generated and not re-billed. Only the verifier and
    the price table run here.
    """

    from mazz.pipeline.compose_claude import NO_ANGLE_SENTINEL
    from mazz.pipeline.draft import DraftContent, render_body
    from mazz.pipeline.score import facts_for_lead
    from mazz.pipeline.verify import verify_content

    records = [
        json.loads(line)
        for line in Path(args.results).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = ev.EvalReport(batch_id=args.batch_id or "adopted", model=args.model)
    factory = get_sessionmaker()

    with factory() as session:
        for record in records:
            lead_id = ev.lead_id_from_custom_id(record["custom_id"])
            lead = session.get(Lead, lead_id) if lead_id is not None else None
            if lead is None:
                raise SystemExit(f"no lead for {record['custom_id']!r} in this database")

            raw = record["usage"]
            usage = ev.Usage(
                input_tokens=raw.get("input", 0),
                output_tokens=raw.get("output", 0),
                cache_read_input_tokens=raw.get("cache_read", 0),
                cache_creation_input_tokens=raw.get("cache_write", 0),
            )
            report.usage = report.usage + usage
            report.cost_usd += ev.price_usd(usage, model=args.model, batch=True)

            case = ev.CaseOutcome(
                lead_id=lead.id,
                company_domain=lead.company_domain or "",
                cache_read_input_tokens=usage.cache_read_input_tokens,
            )
            if record.get("stop_reason") == "refusal":
                case.status = "refused"
                case.notes = "stop_reason=refusal"
                report.cases.append(case)
                continue

            content = DraftContent.model_validate_json(record["text"])
            if content.body_text.strip() == NO_ANGLE_SENTINEL:
                case.status = "no_angle"
                report.no_angle += 1
                report.cases.append(case)
                continue

            facts = facts_for_lead(session, lead)
            body = render_body(content.body_text, lead)
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
            case.evidence = ev._evidence_for(facts, content.signals_used)
            case.notes = verdict.notes
            case.untraceable = [
                {"kind": c.kind, "token": c.token, "where": c.where, "context": c.context}
                for c in verdict.untraceable
            ]
            case.problems = list(verdict.problems)
            case.status = "drafted" if verdict.passed else "held"
            report.cases.append(case)

    report.no_angle += args.no_angle
    report.cache_failure = ev.check_cache_engaged(report)

    out = cell_dir(args.out_dir, args.model, args.effort)
    out.mkdir(parents=True, exist_ok=True)
    entry = {"cell": cell_name(args.model, args.effort), "model": args.model,
             "effort": args.effort, "batch_id": report.batch_id, "uncacheable": None}
    _save_cell_result(out, entry, report)
    ev.write_adjudication(report, out / f"adjudication-{report.batch_id}.jsonl")
    print(ev.render_report(report, cap=args.cap, considered=len(records) + args.no_angle))

    index_path = args.out_dir / "sweep.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    index = [e for e in index if e.get("cell") != entry["cell"]] + [entry]
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\n  adopted into {index_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=["plan", "submit", "status", "collect", "table", "adopt"]
    )
    parser.add_argument("--results", default=None, help="raw batch results for `adopt`")
    parser.add_argument("--model", default="claude-opus-5", help="model for `adopt`")
    parser.add_argument("--effort", default="default", help="effort label for `adopt`")
    parser.add_argument("--batch-id", default=None, help="batch id for `adopt`")
    parser.add_argument("--no-angle", type=int, default=0,
                        help="leads `score` settled no_angle, absent from the results file")
    parser.add_argument("--cap", type=int, default=ev.DEFAULT_CAP)
    parser.add_argument("--models", nargs="+", default=list(MODELS))
    parser.add_argument("--efforts", nargs="+", default=list(EFFORTS))
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--drafts", type=int, default=0,
                        help="print this many full drafts per cell in `table`")
    parser.add_argument("--no-count-tokens", dest="count_tokens", action="store_false")
    return parser


def main(argv: Sequence[str]) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(list(argv))
    if args.cap < 1 or args.cap > ev.MAX_CAP:
        raise SystemExit(f"--cap must be between 1 and {ev.MAX_CAP}")
    return {
        "plan": cmd_plan,
        "submit": cmd_submit,
        "status": cmd_status,
        "collect": cmd_collect,
        "table": cmd_table,
        "adopt": cmd_adopt,
    }[args.command](args)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main(sys.argv[1:]))
