from __future__ import annotations

from dataclasses import dataclass

from event_wiki.models import EventFamily, EventProposal


@dataclass(frozen=True)
class GoldEvent:
    event_id: str
    event_family: EventFamily
    evidence_ids: frozenset[str]


@dataclass(frozen=True)
class DiscoveryMetrics:
    gold_events: int
    predicted_events: int
    matched_gold_events: int
    false_merges: int

    @property
    def recall(self) -> float:
        return self.matched_gold_events / self.gold_events if self.gold_events else 1.0

    @property
    def false_merge_rate(self) -> float:
        return self.false_merges / self.predicted_events if self.predicted_events else 0.0


def evaluate_discovery(
    proposals: list[EventProposal], gold_events: list[GoldEvent]
) -> DiscoveryMetrics:
    matched: set[str] = set()
    false_merges = 0
    for proposal in proposals:
        evidence = set(proposal.evidence_ids)
        overlaps = [
            gold
            for gold in gold_events
            if gold.event_family == proposal.event_family and evidence & gold.evidence_ids
        ]
        matched.update(gold.event_id for gold in overlaps)
        if len(overlaps) > 1:
            false_merges += 1
    return DiscoveryMetrics(
        gold_events=len(gold_events),
        predicted_events=len(proposals),
        matched_gold_events=len(matched),
        false_merges=false_merges,
    )
