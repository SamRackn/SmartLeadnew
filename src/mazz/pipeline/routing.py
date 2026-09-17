"""Campaign assignment: the other end of the per-campaign scoring lever.

CLAUDE.md's routing seam, and the client's stated workflow behind it — *one
queue, the system determines campaign, segment and mailbox*::

    lead -> RoutingPolicy.route(lead) -> (campaign, segment, mailbox)

Why this module exists at all: `docs/DECISIONS.md` settled that the scoring
config is per campaign, because ClientRise runs outbound on behalf of each
agency-client at that client's own target industry, and one ICP vocabulary
cannot serve two engagements. `campaigns.scoring_config` stores it and
`score.config_for_lead` reads it — but until something writes
``leads.campaign_id`` every lead in production is NULL and scores against the
shipped B2B-SaaS defaults. This is the write side.

Three decisions worth stating, because they are the ones a reader will want to
argue with:

**1. Routing is not a compliance gate, and never rejects a lead.**
An unroutable lead is admitted with ``campaign_id`` left NULL, which means "the
shipped defaults" — the exact, already-tested behaviour every existing lead has
today. So the failure mode of this module is *the status quo*, not a dropped
prospect. Contrast with the suppression gate, which fails CLOSED and refuses the
lead: an unreadable never-contact list may mean "do not contact", while an
unmatched campaign only ever means "we do not know whose engagement this is".
Rejecting there would invent a compliance control the brief does not have and
would cost the client leads. Routing therefore sits strictly *after* the
suppression gate and cannot reorder, weaken or short-circuit it.

**2. It never guesses.** If two campaigns claim the same segment the lead is
left unrouted, not handed to whichever row sorted first. A campaign is another
agency-client's engagement; putting a prospect in the wrong one means emailing
him about the wrong thing on the wrong client's behalf. An unrouted lead stays
routable later. A misrouted one looks routed and is wrong.

**3. It only ever fills a NULL.** Re-uploading a spreadsheet refreshes a lead's
details; it must not silently move a person from one client's campaign into
another's. Moving a lead between campaigns is an operator action with its own
audit trail, not a side effect of a re-import.

Mailboxes: ``RoutingDecision`` carries the third slot of CLAUDE.md's triple and
it is ALWAYS ``None`` today, because the ``mailboxes`` table is work-order item
4 and does not exist. Nothing here selects, persists or pretends to know a
mailbox; the field exists so the mailbox arm lands as a filled-in value rather
than as a signature change on every caller. One-campaign-per-mailbox stays
reachable without a migration because that relation belongs on the mailbox row.
"""

from __future__ import annotations

import abc
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from sqlalchemy import select

from mazz.models import Campaign, Lead, Workspace

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ROUTING_POLICY",
    "PinnedRoutingPolicy",
    "RoutingDecision",
    "RoutingPolicy",
    "SystemRoutingPolicy",
    "assign_campaign",
    "candidate_segments",
    "segment_slug",
]


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """``(campaign, segment, mailbox)`` plus the reason, for the import report.

    ``reason`` is not decoration. "no campaign serves segment
    'third-party-logistics'" tells the client he needs a campaign; "two
    campaigns claim it" tells him he has a configuration collision. Both are
    things he can fix; "campaign_id is NULL" is not.
    """

    campaign_id: int | None = None
    segment: str | None = None
    #: Always None. `mailboxes` does not exist yet — see the module docstring.
    mailbox_id: int | None = None
    reason: str = ""

    @property
    def routed(self) -> bool:
        return self.campaign_id is not None

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        if not self.routed:
            return f"unrouted ({self.reason})"
        return f"campaign {self.campaign_id} segment={self.segment!r} ({self.reason})"


def _unrouted(reason: str, segment: str | None = None) -> RoutingDecision:
    return RoutingDecision(campaign_id=None, segment=segment, reason=reason)


# --------------------------------------------------------------------------
# Segments
# --------------------------------------------------------------------------

_SLUG_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def segment_slug(value: str | None) -> str | None:
    """Normalise a segment name so both sides of the match agree.

    ``"Third-Party Logistics"`` -> ``"third-party-logistics"``. Applied to the
    lead's attributes AND to ``campaigns.segment``, so a client who types
    "Ecommerce / DTC" into the campaign row still matches a lead whose CRM
    exported "ecommerce dtc". `campaigns.segment` is deliberately free text
    (a segmentation is the client's, not ours), and free text with no agreed
    normalisation is free text that silently never matches.
    """

    slug = _SLUG_PUNCT_RE.sub("-", (value or "").strip().lower()).strip("-")
    return slug or None


def candidate_segments(lead: Lead) -> tuple[str, ...]:
    """The segments this lead could belong to, MOST SPECIFIC FIRST.

    Segment comes from lead attributes, per CLAUDE.md. The client's records
    carry two industry fields and they are exactly the pair this needs: an
    engagement is usually sold at one of those two grains ("Third-Party
    Logistics" or the broader "Logistics & Supply Chain"), and which grain a
    given client agreed on is his decision, not ours.

    Ordering is the whole design: a campaign written at the specific grain wins
    over one written at the broad grain, so an agency running a narrow
    engagement is not out-competed by another client's catch-all.

    Headcount and revenue are deliberately NOT folded in. A composite like
    ``ecommerce-smb`` is one plausible segmentation among many, and baking a
    band-splitting rule in here would make the segment vocabulary ours instead
    of the client's. A campaign that wants a size split states it as its own
    segment string and the client populates the industry field to match, or a
    different `RoutingPolicy` gets written — which is why this is a policy.
    """

    out: list[str] = []
    for value in (lead.industry_specific, lead.industry_primary):
        slug = segment_slug(value)
        if slug and slug not in out:
            out.append(slug)
    return tuple(out)


# --------------------------------------------------------------------------
# Policies
# --------------------------------------------------------------------------


class RoutingPolicy(abc.ABC):
    """How a lead is matched to a campaign. Swappable by design.

    CLAUDE.md is explicit that this must be a policy rather than a hardwired
    topology, so that one-campaign-per-mailbox — or per-industry, or per-source
    — stays reachable without a migration.

    ``session`` is a keyword argument rather than constructor state so a policy
    is a stateless, reusable, configuration-shaped object while still being able
    to read ``campaigns``. The return type carries the mailbox slot already, so
    the work-order-item-4 arm lands inside an implementation and touches no
    caller.
    """

    @abc.abstractmethod
    def route(self, lead: Lead, *, session: "Session") -> RoutingDecision:
        """Decide the campaign for one lead. Must not write anything."""

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.__class__.__name__


class SystemRoutingPolicy(RoutingPolicy):
    """The default: the system determines the campaign from lead attributes.

    Derive the lead's candidate segments (specific industry, then primary) and
    match them against ``campaigns.segment``, normalised on both sides. First
    grain that matches EXACTLY ONE campaign wins.

    Ambiguity is refused rather than broken by a tie-break: two campaigns
    claiming one segment is a configuration mistake, and a mistake that produces
    a visible "unrouted, two campaigns claim it" is cheap next to one that
    quietly emails an agency's prospect on behalf of its competitor.

    ``workspace_slug`` narrows the candidate set to one agency-client — the
    natural shape for "this spreadsheet is Acme Digital's", and the thing that
    makes cross-client ambiguity impossible in the first place.

    No caching, for the same reason `SuppressionChecker` does not cache: at ~25
    leads/day a per-lead SELECT against a table with a handful of rows costs
    nothing, while a cache would happily keep routing to a campaign the client
    just retired mid-upload.
    """

    def __init__(self, *, workspace_slug: str | None = None) -> None:
        self.workspace_slug = workspace_slug

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        if self.workspace_slug:
            return f"SystemRoutingPolicy(workspace_slug={self.workspace_slug!r})"
        return "SystemRoutingPolicy()"

    def _candidate_campaigns(self, session: "Session") -> Sequence[Campaign]:
        stmt = select(Campaign).where(Campaign.segment.isnot(None))
        if self.workspace_slug is not None:
            stmt = stmt.join(Campaign.workspace).where(
                Workspace.slug == self.workspace_slug
            )
        return session.execute(stmt.order_by(Campaign.id)).scalars().all()

    def route(self, lead: Lead, *, session: "Session") -> RoutingDecision:
        segments = candidate_segments(lead)
        if not segments:
            return _unrouted(
                "lead has no industry to segment on; left on the shipped "
                "scoring defaults"
            )

        campaigns = self._candidate_campaigns(session)
        if not campaigns:
            return _unrouted(
                "no campaign declares a segment; left on the shipped scoring "
                "defaults",
                segment=segments[0],
            )

        for segment in segments:
            matches = [c for c in campaigns if segment_slug(c.segment) == segment]
            if len(matches) == 1:
                campaign = matches[0]
                return RoutingDecision(
                    campaign_id=campaign.id,
                    segment=segment,
                    reason=(
                        f"segment {segment!r} -> campaign {campaign.id} "
                        f"({campaign.name!r})"
                    ),
                )
            if len(matches) > 1:
                ids = ", ".join(str(c.id) for c in matches)
                return _unrouted(
                    f"segment {segment!r} is claimed by {len(matches)} campaigns "
                    f"({ids}); refusing to guess which client this prospect "
                    "belongs to",
                    segment=segment,
                )

        return _unrouted(
            f"no campaign serves segment {segments[0]!r}; left on the shipped "
            "scoring defaults",
            segment=segments[0],
        )


class PinnedRoutingPolicy(RoutingPolicy):
    """Everything in this import goes to one named campaign.

    The honest shape of "this spreadsheet is for Acme Digital's Q4 engagement":
    the operator knows, so nothing has to be inferred. It is a `RoutingPolicy`
    rather than a special case bolted onto ingest, which is the point of having
    a policy seam — the caller swaps the policy instead of ingest growing a
    second, looser way in.

    A campaign id that does not exist leaves the lead UNROUTED with a reason,
    rather than raising. Routing is not a gate (module docstring), and dropping
    the client's upload because a campaign was retired between two clicks would
    be a worse answer than admitting the leads on the shipped defaults and
    telling him.
    """

    def __init__(self, campaign_id: int) -> None:
        self.campaign_id = campaign_id

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"PinnedRoutingPolicy(campaign_id={self.campaign_id})"

    def route(self, lead: Lead, *, session: "Session") -> RoutingDecision:
        campaign = session.get(Campaign, self.campaign_id)
        if campaign is None:
            log.warning(
                "pinned campaign %s does not exist; lead %s stays unrouted",
                self.campaign_id,
                getattr(lead, "id", None),
            )
            return _unrouted(
                f"pinned campaign {self.campaign_id} does not exist; left on "
                "the shipped scoring defaults"
            )
        return RoutingDecision(
            campaign_id=campaign.id,
            segment=segment_slug(campaign.segment),
            reason=f"pinned to campaign {campaign.id} ({campaign.name!r})",
        )


#: The default policy, per CLAUDE.md. Stateless, so one instance is fine.
DEFAULT_ROUTING_POLICY: RoutingPolicy = SystemRoutingPolicy()


# --------------------------------------------------------------------------
# Applying a decision
# --------------------------------------------------------------------------


def assign_campaign(
    session: "Session",
    lead: Lead,
    policy: RoutingPolicy | None = None,
) -> RoutingDecision:
    """Route ``lead`` and write ``campaign_id`` if it is not already set.

    The only writer of ``leads.campaign_id`` in the codebase. Three guarantees,
    all of them load-bearing somewhere else:

    * **It never raises.** A broken policy or an unreadable ``campaigns`` table
      leaves the lead unrouted — NULL, the shipped defaults — and never costs
      the client the lead. The read runs in its own SAVEPOINT so a database
      error inside it cannot roll back the ingest that called it.
    * **It never overwrites.** A lead that already belongs to a campaign keeps
      it; re-importing a spreadsheet must not move a person between clients'
      engagements.
    * **It never deletes or clears.** ``campaign_id`` going NULL is
      ``ON DELETE SET NULL``'s job (retiring a campaign keeps its prospects) and
      person deletion is `privacy.delete_person`'s. Neither is routing's.
    """

    active = policy if policy is not None else DEFAULT_ROUTING_POLICY

    if lead.campaign_id is not None:
        return RoutingDecision(
            campaign_id=lead.campaign_id,
            reason="already assigned to a campaign; left as it is",
        )

    try:
        with session.begin_nested():
            decision = active.route(lead, session=session)
    except Exception as exc:  # noqa: BLE001 - routing must never sink a lead
        log.warning(
            "routing failed for lead %s (%s): %s: %s; leaving campaign_id NULL",
            getattr(lead, "id", None),
            active,
            exc.__class__.__name__,
            exc,
        )
        return _unrouted(
            f"routing failed ({exc.__class__.__name__}: {exc}); left on the "
            "shipped scoring defaults"
        )

    if decision.routed:
        lead.campaign_id = decision.campaign_id
        log.info("lead %s routed: %s", getattr(lead, "id", None), decision)
    else:
        log.info("lead %s unrouted: %s", getattr(lead, "id", None), decision.reason)

    return decision
