from __future__ import annotations

import hashlib
import heapq
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from event_wiki.models import CandidateBundle, EvidenceDocument

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOP = {"a", "an", "the", "of", "for", "to", "in", "on", "and", "or", "says", "said"}


def title_tokens(title: str) -> set[str]:
    return {
        token for token in TOKEN_RE.findall(title.lower()) if token not in STOP and len(token) > 1
    }


def jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def candidate_id_for(evidence_ids: list[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(set(evidence_ids))).encode()).hexdigest()[:20]
    return f"CAND_{digest.upper()}"


@dataclass
class RetrievalStats:
    """Deterministic counters used for scale regression tests and run metrics."""

    documents_seen: int = 0
    index_candidates_scanned: int = 0
    title_comparisons: int = 0
    peak_active_documents: int = 0
    bundles_emitted: int = 0


@dataclass
class _Component:
    pending: list[EvidenceDocument]
    last_seen: datetime
    order: int
    member_ids: set[str]
    generation: int = 0


@dataclass
class _StreamingBuilder:
    window_days: int
    title_threshold: float
    max_documents: int
    max_comparisons: int
    stats: RetrievalStats
    active: dict[str, EvidenceDocument] = field(default_factory=dict)
    active_order: deque[tuple[datetime, str]] = field(default_factory=deque)
    tokens_by_id: dict[str, set[str]] = field(default_factory=dict)
    token_index: dict[str, dict[str, None]] = field(default_factory=lambda: defaultdict(dict))
    symbol_index: dict[str, dict[str, None]] = field(default_factory=lambda: defaultdict(dict))
    entity_index: dict[str, dict[str, None]] = field(default_factory=lambda: defaultdict(dict))
    no_symbol_index: dict[str, None] = field(default_factory=dict)
    parent: dict[str, str] = field(default_factory=dict)
    components: dict[str, _Component] = field(default_factory=dict)
    expiry_heap: list[tuple[datetime, int, str, int]] = field(default_factory=list)
    next_order: int = 0

    @property
    def window(self) -> timedelta:
        return timedelta(days=self.window_days)

    def find(self, evidence_id: str) -> str:
        root = evidence_id
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[evidence_id] != evidence_id:
            parent = self.parent[evidence_id]
            self.parent[evidence_id] = root
            evidence_id = parent
        return root

    def union(self, left: str, right: str) -> str:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return left_root
        left_component = self.components[left_root]
        right_component = self.components[right_root]
        if (right_component.order, right_root) < (left_component.order, left_root):
            left_root, right_root = right_root, left_root
            left_component, right_component = right_component, left_component
        self.parent[right_root] = left_root
        left_component.pending.extend(right_component.pending)
        left_component.pending.sort(key=lambda doc: (doc.published_at, doc.evidence_id))
        left_component.member_ids.update(right_component.member_ids)
        left_component.last_seen = max(left_component.last_seen, right_component.last_seen)
        left_component.generation += 1
        del self.components[right_root]
        self._schedule(left_root)
        return left_root

    def _schedule(self, root: str) -> None:
        component = self.components[root]
        heapq.heappush(
            self.expiry_heap,
            (component.last_seen, component.order, root, component.generation),
        )

    def _remove_active(self, evidence_id: str) -> None:
        document = self.active.pop(evidence_id, None)
        if document is None:
            return
        for token in self.tokens_by_id.pop(evidence_id, ()):
            posting = self.token_index[token]
            posting.pop(evidence_id, None)
            if not posting:
                del self.token_index[token]
        for symbol in document.symbols:
            posting = self.symbol_index[symbol]
            posting.pop(evidence_id, None)
            if not posting:
                del self.symbol_index[symbol]
        for entity in document.entity_names:
            posting = self.entity_index[entity]
            posting.pop(evidence_id, None)
            if not posting:
                del self.entity_index[entity]
        self.no_symbol_index.pop(evidence_id, None)

    def _expire_active(self, cutoff: datetime) -> None:
        while self.active_order and self.active_order[0][0] < cutoff:
            _, evidence_id = self.active_order.popleft()
            self._remove_active(evidence_id)

    def _bundle(self, documents: list[EvidenceDocument]) -> CandidateBundle:
        evidence_ids = [document.evidence_id for document in documents]
        self.stats.bundles_emitted += 1
        return CandidateBundle(
            candidate_id=candidate_id_for(evidence_ids),
            evidence_ids=evidence_ids,
            window_start=min(document.published_at for document in documents),
            window_end=max(document.published_at for document in documents),
            symbols=sorted({symbol for document in documents for symbol in document.symbols}),
            entity_names=sorted({name for document in documents for name in document.entity_names}),
            retrieval_reason="shared entity/symbol and temporally local title similarity",
        )

    def _drain_full_chunks(self, root: str) -> Iterator[CandidateBundle]:
        component = self.components[root]
        while len(component.pending) >= self.max_documents:
            chunk = component.pending[: self.max_documents]
            del component.pending[: self.max_documents]
            yield self._bundle(chunk)

    def _finalize_expired(self, cutoff: datetime) -> Iterator[CandidateBundle]:
        while self.expiry_heap and self.expiry_heap[0][0] < cutoff:
            last_seen, _, root, generation = heapq.heappop(self.expiry_heap)
            component = self.components.get(root)
            if (
                component is None
                or component.generation != generation
                or component.last_seen != last_seen
            ):
                continue
            yield from self._drain_full_chunks(root)
            if component.pending:
                yield self._bundle(component.pending)
            for evidence_id in component.member_ids:
                self.parent.pop(evidence_id, None)
            del self.components[root]

    def _candidate_ids(self, document: EvidenceDocument, tokens: set[str]) -> list[str]:
        # Probe the smallest token/identity posting lists. The scan budget prevents a generic
        # title or hot ticker from turning the active window back into an O(N^2) loop.
        sources: list[tuple[int, str, str, dict[str, None]]] = []
        for token in tokens:
            posting = self.token_index.get(token)
            if posting:
                sources.append((len(posting), "token", token, posting))
        for symbol in document.symbols:
            posting = self.symbol_index.get(symbol)
            if posting:
                sources.append((len(posting), "symbol", symbol, posting))
        for entity in document.entity_names:
            posting = self.entity_index.get(entity)
            if posting:
                sources.append((len(posting), "entity", entity, posting))
        if document.symbols and self.no_symbol_index:
            sources.append((len(self.no_symbol_index), "no_symbol", "", self.no_symbol_index))
        sources.sort(key=lambda item: (item[0], item[1], item[2]))
        selected: list[str] = []
        seen: set[str] = set()
        current_symbols = set(document.symbols)
        current_entities = set(document.entity_names)
        scan_budget = self.max_comparisons * 4
        for _, _, _, posting in sources:
            for evidence_id in reversed(posting):
                if evidence_id in seen:
                    continue
                seen.add(evidence_id)
                self.stats.index_candidates_scanned += 1
                scan_budget -= 1
                previous = self.active[evidence_id]
                shares_identity = bool(
                    current_symbols.intersection(previous.symbols)
                    or current_entities.intersection(previous.entity_names)
                )
                if current_symbols and previous.symbols and not shares_identity:
                    if scan_budget <= 0:
                        return selected
                    continue
                if not tokens.intersection(self.tokens_by_id[evidence_id]):
                    if scan_budget <= 0:
                        return selected
                    continue
                selected.append(evidence_id)
                if len(selected) >= self.max_comparisons:
                    return selected
                if scan_budget <= 0:
                    return selected
        return selected

    def add(self, document: EvidenceDocument) -> Iterator[CandidateBundle]:
        cutoff = document.published_at - self.window
        self._expire_active(cutoff)
        yield from self._finalize_expired(cutoff)

        evidence_id = document.evidence_id
        if evidence_id in self.parent:
            raise ValueError(f"duplicate evidence_id in candidate stream: {evidence_id}")
        tokens = title_tokens(document.title)
        self.parent[evidence_id] = evidence_id
        component = _Component(
            pending=[document],
            last_seen=document.published_at,
            order=self.next_order,
            member_ids={evidence_id},
        )
        self.next_order += 1
        self.components[evidence_id] = component
        self._schedule(evidence_id)

        matches: list[str] = []
        for previous_id in self._candidate_ids(document, tokens):
            self.stats.title_comparisons += 1
            if jaccard(tokens, self.tokens_by_id[previous_id]) >= self.title_threshold:
                matches.append(previous_id)
        root = evidence_id
        for previous_id in matches:
            root = self.union(root, previous_id)
        component = self.components[root]
        component.last_seen = max(component.last_seen, document.published_at)
        component.generation += 1
        self._schedule(root)

        self.active[evidence_id] = document
        self.active_order.append((document.published_at, evidence_id))
        self.tokens_by_id[evidence_id] = tokens
        for token in tokens:
            self.token_index[token][evidence_id] = None
        for symbol in document.symbols:
            self.symbol_index[symbol][evidence_id] = None
        for entity in document.entity_names:
            self.entity_index[entity][evidence_id] = None
        if not document.symbols:
            self.no_symbol_index[evidence_id] = None
        self.stats.documents_seen += 1
        self.stats.peak_active_documents = max(self.stats.peak_active_documents, len(self.active))
        yield from self._drain_full_chunks(root)

    def finish(self) -> Iterator[CandidateBundle]:
        for root, component in sorted(
            self.components.items(), key=lambda item: (item[1].order, item[0])
        ):
            yield from self._drain_full_chunks(root)
            if component.pending:
                yield self._bundle(component.pending)
            for evidence_id in component.member_ids:
                self.parent.pop(evidence_id, None)
        self.components.clear()


def iter_candidate_bundles(
    documents: Iterable[EvidenceDocument],
    *,
    window_days: int = 2,
    title_threshold: float = 0.18,
    max_documents: int = 12,
    max_comparisons_per_document: int = 64,
    stats: RetrievalStats | None = None,
) -> Iterator[CandidateBundle]:
    """Build candidates from a timestamp-ordered stream with bounded document memory."""
    if max_documents < 1 or max_comparisons_per_document < 1:
        raise ValueError("candidate size and comparison budget must be positive")
    stats = stats or RetrievalStats()
    builder = _StreamingBuilder(
        window_days=window_days,
        title_threshold=title_threshold,
        max_documents=max_documents,
        max_comparisons=max_comparisons_per_document,
        stats=stats,
    )
    previous_key: tuple[datetime, str] | None = None
    for document in documents:
        key = (document.published_at, document.evidence_id)
        if previous_key is not None and key < previous_key:
            raise ValueError(
                "candidate evidence stream must be ordered by published_at, evidence_id"
            )
        previous_key = key
        yield from builder.add(document)
    yield from builder.finish()


def build_candidate_bundles(
    documents: list[EvidenceDocument],
    *,
    window_days: int = 2,
    title_threshold: float = 0.18,
    max_documents: int = 12,
    max_comparisons_per_document: int = 64,
    stats: RetrievalStats | None = None,
) -> list[CandidateBundle]:
    """Compatibility wrapper for small in-memory fixtures and evaluation sets."""
    ordered = sorted(documents, key=lambda doc: (doc.published_at, doc.evidence_id))
    return list(
        iter_candidate_bundles(
            ordered,
            window_days=window_days,
            title_threshold=title_threshold,
            max_documents=max_documents,
            max_comparisons_per_document=max_comparisons_per_document,
            stats=stats,
        )
    )
