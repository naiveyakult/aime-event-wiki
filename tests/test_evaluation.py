from event_wiki.agents import AgentSuite, HeuristicStructuredClient
from event_wiki.evaluation import evaluate_discovery
from event_wiki.retrieval import build_candidate_bundles
from tests.fixtures.synthetic_gold import build_synthetic_gold


def test_synthetic_gold_has_required_scale_and_meets_discovery_gate() -> None:
    documents, gold = build_synthetic_gold()
    assert len(documents) == 200
    assert len(gold) == 30

    by_id = {document.evidence_id: document for document in documents}
    suite = AgentSuite(HeuristicStructuredClient())
    proposals = []
    for candidate in build_candidate_bundles(documents):
        evidence = [by_id[evidence_id] for evidence_id in candidate.evidence_ids]
        proposals.extend(suite.discover(candidate, evidence))
    metrics = evaluate_discovery(proposals, gold)
    assert metrics.recall >= 0.8
    assert metrics.false_merge_rate < 0.05
