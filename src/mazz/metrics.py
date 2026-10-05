"""The numbers the client judges this system by.

PRIMARY: cost per qualified conversation = a month's recorded costs divided by
the conversations a human tagged `qualified` IN that month (by the date of the
tag, not the date of the email). Qualified means a genuine response from a
decision-maker in the vertical, showing enough interest to discuss the
services. The system never decides it; a person presses the button.

SECONDARY: cost per contacted lead. Everything else here (sent, reply rate,
bounce rate, booked, showed, won) explains where the primary number comes from.

A ratio with no denominator is None, never zero: "$0 per conversation" reads
like a success, and it means nothing was measured.

Months are UTC calendar months. We deliberately do NOT build open or click
tracking (Smartlead's was off too: 0 opens), so none is reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from mazz.models import (
    Campaign,
    CostEntry,
    Lead,
    Reply,
    ReplyTag,
    SentMessage,
)

COST_CATEGORIES: dict[str, str] = {
    "mailboxes": "Mailboxes / SmartSenders",
    "google_workspace": "Google Workspace seats",
    "domains": "Domains",
    "smartlead": "Smartlead subscription",
    "zoominfo": "ZoomInfo allocation",
    "hosting": "Hosting",
    "other": "Other",
}

#: The human funnel, in order. `interested` is a precondition, not a goal.
STAGES = ("interested", "qualified", "meeting_booked", "meeting_showed", "client_won")

#: The client's own Smartlead numbers before this system, from his screenshot.
#: Shown beside ours so "better or worse" is a comparison, not a feeling.
BASELINE = {
    "label": "Smartlead (client's screenshot, before cutover)",
    "contacted": 133,
    "unique_replies": 6,
    "reply_rate": 6 / 133,
    "positive": 0,
    "bounces": 3,
    "bounce_rate": 0.009,
    "deals": 0,
    "note": "Open tracking was off (0 opens), so there is nothing to compare there.",
}


def month_start(value: date | datetime) -> date:
    return date(value.year, value.month, 1)


def parse_month(raw: str | None, *, default: date | None = None) -> date:
    """'2026-10' -> date(2026, 10, 1). Anything else -> ``default`` / this month."""

    try:
        year, month = (raw or "").split("-")
        return date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return month_start(default or datetime.now(timezone.utc))


def bounds(month: date) -> tuple[datetime, datetime]:
    start = datetime(month.year, month.month, 1, tzinfo=timezone.utc)
    nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return start, datetime(nxt.year, nxt.month, 1, tzinfo=timezone.utc)


def ratio(numerator, denominator):
    return None if not denominator else numerator / denominator


@dataclass(slots=True)
class MonthMetrics:
    month: date
    workspace_id: int | None
    cost_total: Decimal = Decimal("0")
    cost_by_category: dict[str, Decimal] = field(default_factory=dict)
    sent: int = 0
    contacted: int = 0
    unique_replies: int = 0
    bounces: int = 0
    stages: dict[str, int] = field(default_factory=dict)
    skipped_unconfirmed: int = 0

    @property
    def has_costs(self) -> bool:
        return bool(self.cost_by_category)

    @property
    def qualified(self) -> int:
        return self.stages.get("qualified", 0)

    @property
    def cost_per_qualified(self) -> Decimal | None:
        return ratio(self.cost_total, self.qualified) if self.has_costs else None

    @property
    def cost_per_contacted(self) -> Decimal | None:
        return ratio(self.cost_total, self.contacted) if self.has_costs else None

    @property
    def reply_rate(self) -> float | None:
        return ratio(self.unique_replies, self.contacted)

    @property
    def bounce_rate(self) -> float | None:
        return ratio(self.bounces, self.sent)


def _scoped_leads(workspace_id: int | None):
    query = select(Lead.id)
    if workspace_id is not None:
        query = query.join(Campaign, Campaign.id == Lead.campaign_id).where(
            Campaign.workspace_id == workspace_id
        )
    return query


def month_metrics(
    session: Session, month: date, *, workspace_id: int | None = None
) -> MonthMetrics:
    start, end = bounds(month)
    leads = _scoped_leads(workspace_id).scalar_subquery()
    m = MonthMetrics(month=month, workspace_id=workspace_id)

    costs = select(CostEntry.category, func.sum(CostEntry.amount)).where(CostEntry.month == month)
    if workspace_id is not None:
        costs = costs.where(CostEntry.workspace_id == workspace_id)
    for category, total in session.execute(costs.group_by(CostEntry.category)).all():
        m.cost_by_category[category] = Decimal(total)
    m.cost_total = sum(m.cost_by_category.values(), Decimal("0"))

    # Smartlead's own sends are its baseline, not ours.
    in_month = (SentMessage.sent_at >= start, SentMessage.sent_at < end,
                SentMessage.lead_id.in_(leads), SentMessage.legacy.is_(False))
    m.sent = session.execute(select(func.count(SentMessage.id)).where(*in_month)).scalar_one()
    m.contacted = session.execute(
        select(func.count(distinct(SentMessage.lead_id))).where(*in_month)
    ).scalar_one()
    m.bounces = session.execute(
        select(func.count(SentMessage.id)).where(
            SentMessage.bounced_at >= start, SentMessage.bounced_at < end,
            SentMessage.lead_id.in_(leads), SentMessage.legacy.is_(False),
        )
    ).scalar_one()
    # Genuine replies only: an out-of-office is not a response. And only from
    # someone we emailed, so a reply to Smartlead's mail has no denominator here.
    m.unique_replies = session.execute(
        select(func.count(distinct(Reply.lead_id))).where(
            Reply.received_at >= start, Reply.received_at < end,
            Reply.classification != "ooo", Reply.lead_id.in_(leads),
            Reply.lead_id.in_(select(SentMessage.lead_id).where(SentMessage.legacy.is_(False))),
        )
    ).scalar_one()

    tagged = dict(
        session.execute(
            select(ReplyTag.tag, func.count(distinct(Reply.lead_id)))
            .join(Reply, Reply.id == ReplyTag.reply_id)
            .where(ReplyTag.tagged_at >= start, ReplyTag.tagged_at < end,
                   Reply.lead_id.in_(leads))
            .group_by(ReplyTag.tag)
        ).all()
    )
    m.stages = {stage: tagged.get(stage, 0) for stage in STAGES}

    skipped = select(func.count(Lead.id)).where(Lead.skip_reason == "vertical_unconfirmed")
    if workspace_id is not None:
        skipped = skipped.where(Lead.id.in_(leads))
    m.skipped_unconfirmed = session.execute(skipped).scalar_one()
    return m
