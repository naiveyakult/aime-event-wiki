from datetime import UTC, datetime

from event_wiki.agents import AgentSuite, HeuristicStructuredClient
from event_wiki.models import CandidateBundle, EventFamily, EvidenceDocument

NOW = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)


def document(evidence_id: str, title: str, body: str) -> EvidenceDocument:
    return EvidenceDocument(
        evidence_id=evidence_id,
        content_type="US_NEWS",
        title=title,
        body=body,
        published_at=NOW,
        known_at=NOW,
        source_name="Synthetic Wire",
        symbols=["EXM"],
        entity_names=["Example Corp"],
        source_locator=f"synthetic://{evidence_id}",
        content_hash=("a" if evidence_id == "D1" else "b") * 64,
    )


def test_discovery_can_emit_multiple_events_from_one_candidate() -> None:
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1", "D2"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    evidence = [
        document("D1", "Example reports quarterly earnings", "Revenue was $10 million."),
        document("D2", "Example launches a new product", "Example launched Widget One."),
    ]

    proposals = AgentSuite(HeuristicStructuredClient()).discover(candidate, evidence)

    assert {proposal.event_family for proposal in proposals} == {
        EventFamily.EARNINGS,
        EventFamily.PRODUCT_PARTNERSHIP,
    }
    assert all(proposal.evidence_ids for proposal in proposals)


def test_claims_and_relations_preserve_evidence_provenance() -> None:
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    evidence = [document("D1", "Example reports quarterly earnings", "Revenue was $10 million.")]
    suite = AgentSuite(HeuristicStructuredClient())
    proposal = suite.discover(candidate, evidence)[0]

    claims = suite.extract_claims(proposal, evidence)
    relations = suite.build_relations(proposal, evidence)

    assert claims and all(claim.evidence_ids == ["D1"] for claim in claims)
    assert relations and all(relation.evidence_ids == ["D1"] for relation in relations)

    decision = suite.resolve([proposal], type("Repo", (), {"find_events": lambda *_a, **_k: []})())
    audit = suite.audit(claims, relations, evidence, NOW)
    patch = suite.propose_patch(
        thread_id="C1",
        proposals=[proposal],
        decisions=decision,
        claims=claims,
        relations=relations,
        audit=audit,
    )
    edge_types = {edge["relation_type"] for edge in patch.payload["edges"]}
    assert {"supports", "supported_by", "involves_entity", "issuer"} <= edge_types
    assert patch.event_id == patch.payload["event"]["event_id"]


def test_claim_evidence_is_rebound_to_documents_containing_the_quote() -> None:
    class MultiEvidenceClaimClient(HeuristicStructuredClient):
        def _extract_claims(self, context):  # noqa: ANN001
            proposal = context["proposal"]
            return {
                "claims": [
                    {
                        "claim_id": "CLAIM_1",
                        "subject": proposal["event_subject"],
                        "predicate": "posted",
                        "object_value": "the investor presentation",
                        "kind": "company_statement",
                        "event_time": proposal["event_time"],
                        "known_at": proposal["known_at"],
                        "evidence_ids": ["D1", "D2"],
                        "quote": "Example posted the investor presentation.",
                        "confidence": 1.0,
                    }
                ]
            }

    evidence = [
        document("D1", "Example SEC Form 8-K", "Please enable JavaScript to use the viewer."),
        document(
            "D2",
            "Example SEC Form 8-K",
            "Example posted the investor presentation. It was furnished as an exhibit.",
        ),
    ]
    suite = AgentSuite(MultiEvidenceClaimClient())
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1", "D2"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    proposal = suite.discover(candidate, evidence)[0].model_copy(
        update={"evidence_ids": ["D1", "D2"]}
    )

    claims = suite.extract_claims(proposal, evidence)

    assert len(claims) == 1
    assert claims[0].evidence_ids == ["D2"]
    assert suite.audit(claims, [], evidence, NOW).issues == []


def test_relation_agent_merges_duplicate_relation_evidence() -> None:
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1", "D2"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    evidence = [
        document("D1", "Example launches a product", "Example launched Widget One."),
        document("D2", "Example launches a product", "Example launched Widget One."),
    ]
    suite = AgentSuite(HeuristicStructuredClient())
    proposal = suite.discover(candidate, evidence)[0].model_copy(
        update={"evidence_ids": ["D1", "D2"]}
    )

    relations = suite.build_relations(proposal, evidence)

    assert len(relations) == 1
    assert relations[0].evidence_ids == ["D1", "D2"]


def test_multiple_proposals_create_isolated_patches() -> None:
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1", "D2"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    evidence = [
        document("D1", "Example reports quarterly earnings", "Revenue was $10 million."),
        document("D2", "Example launches a new product", "Example launched Widget One."),
    ]
    suite = AgentSuite(HeuristicStructuredClient())
    proposals = suite.discover(candidate, evidence)
    decisions = suite.resolve(proposals, type("Repo", (), {"find_events": lambda *_a, **_k: []})())
    claims = [
        claim
        for proposal in proposals
        for claim in suite.extract_claims(
            proposal,
            [item for item in evidence if item.evidence_id in proposal.evidence_ids],
        )
    ]
    relations = [
        relation
        for proposal in proposals
        for relation in suite.build_relations(
            proposal,
            [item for item in evidence if item.evidence_id in proposal.evidence_ids],
        )
    ]
    audit = suite.audit(claims, relations, evidence, NOW)

    patches = suite.propose_patches(
        thread_id="C1",
        proposals=proposals,
        decisions=decisions,
        claims=claims,
        relations=relations,
        audit=audit,
    )

    assert len(patches) == 2
    assert len({patch.patch_id for patch in patches}) == 2
    for patch in patches:
        proposal = patch.payload["proposals"][0]
        assert patch.evidence_ids == proposal["evidence_ids"]
        assert all(
            set(claim["evidence_ids"]) <= set(proposal["evidence_ids"])
            for claim in patch.payload["claims"]
        )
