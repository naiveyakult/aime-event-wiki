from datetime import UTC, datetime

import httpx
from openai import APIConnectionError

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


def test_claim_extraction_chunks_long_evidence_and_preserves_provenance() -> None:
    class RecordingClaimClient(HeuristicStructuredClient):
        def __init__(self) -> None:
            self.body_lengths: list[int] = []

        def _extract_claims(self, context):  # noqa: ANN001
            document = context["evidence"][0]
            self.body_lengths.append(len(document["body"]))
            quote = document["body"][:40].strip()
            proposal = context["proposal"]
            return {
                "claims": [
                    {
                        "claim_id": f"CLAIM_{len(self.body_lengths)}",
                        "subject": proposal["event_subject"],
                        "predicate": "reported",
                        "object_value": quote,
                        "kind": "reported_claim",
                        "event_time": proposal["event_time"],
                        "known_at": proposal["known_at"],
                        "evidence_ids": [document["evidence_id"]],
                        "quote": quote,
                        "confidence": 0.8,
                    }
                ]
            }

    body = " ".join(f"Sentence {index} contains a supported fact." for index in range(120))
    evidence = [document("D1", "Example SEC earnings", body)]
    client = RecordingClaimClient()
    suite = AgentSuite(client)
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    proposal = suite.discover(candidate, evidence)[0]

    claims = suite.extract_claims(proposal, evidence)

    assert len(client.body_lengths) > 1
    assert max(client.body_lengths) <= 1_000
    assert claims
    assert all(claim.evidence_ids == ["D1"] for claim in claims)


def test_claim_extraction_retries_incomplete_response_with_smaller_chunks() -> None:
    class IncompleteReadClient(HeuristicStructuredClient):
        def __init__(self) -> None:
            self.body_lengths: list[int] = []

        def _extract_claims(self, context):  # noqa: ANN001
            document = context["evidence"][0]
            body = document["body"]
            self.body_lengths.append(len(body))
            if len(body) > 700:
                request = httpx.Request("POST", "https://api.example.test/chat/completions")
                try:
                    raise httpx.RemoteProtocolError(
                        "peer closed connection without sending complete message body "
                        "(incomplete chunked read)"
                    )
                except httpx.RemoteProtocolError as cause:
                    raise APIConnectionError(request=request) from cause
            proposal = context["proposal"]
            quote = body[:40].strip()
            return {
                "claims": [
                    {
                        "claim_id": f"CLAIM_{len(self.body_lengths)}",
                        "subject": proposal["event_subject"],
                        "predicate": "reported",
                        "object_value": quote,
                        "kind": "reported_claim",
                        "event_time": proposal["event_time"],
                        "known_at": proposal["known_at"],
                        "evidence_ids": [document["evidence_id"]],
                        "quote": quote,
                        "confidence": 0.8,
                    }
                ]
            }

    body = " ".join(f"Sentence {index} contains a supported fact." for index in range(32))
    evidence = [document("D1", "Example SEC earnings", body)]
    client = IncompleteReadClient()
    suite = AgentSuite(client)
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    proposal = suite.discover(candidate, evidence)[0]

    claims = suite.extract_claims(proposal, evidence)

    assert client.body_lengths[0] > 700
    assert any(length <= 700 for length in client.body_lengths[1:])
    assert claims


def test_claim_extraction_retries_truncated_json_with_smaller_chunks() -> None:
    class TruncatedJsonClient(HeuristicStructuredClient):
        def __init__(self) -> None:
            self.body_lengths: list[int] = []

        def _extract_claims(self, context):  # noqa: ANN001
            document = context["evidence"][0]
            body = document["body"]
            self.body_lengths.append(len(body))
            if len(body) > 700:
                raise ValueError("Invalid JSON: EOF while parsing a string")
            proposal = context["proposal"]
            quote = body[:40].strip()
            return {
                "claims": [
                    {
                        "claim_id": f"CLAIM_{len(self.body_lengths)}",
                        "subject": proposal["event_subject"],
                        "predicate": "reported",
                        "object_value": quote,
                        "kind": "reported_claim",
                        "event_time": proposal["event_time"],
                        "known_at": proposal["known_at"],
                        "evidence_ids": [document["evidence_id"]],
                        "quote": quote,
                        "confidence": 0.8,
                    }
                ]
            }

    body = " ".join(f"Sentence {index} contains a supported fact." for index in range(24))
    evidence = [document("D1", "Example SEC earnings", body)]
    client = TruncatedJsonClient()
    suite = AgentSuite(client)
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    proposal = suite.discover(candidate, evidence)[0]

    claims = suite.extract_claims(proposal, evidence)

    assert client.body_lengths[0] > 700
    assert any(length <= 700 for length in client.body_lengths[1:])
    assert claims


def test_claim_extraction_retries_a_failed_minimum_chunk_once() -> None:
    class FlakyMinimumChunkClient(HeuristicStructuredClient):
        def __init__(self) -> None:
            self.attempts = 0

        def _extract_claims(self, context):  # noqa: ANN001
            self.attempts += 1
            if self.attempts == 1:
                raise ValueError("Invalid JSON: EOF while parsing a string")
            return super()._extract_claims(context)

    evidence = [
        document("D1", "Example SEC earnings", "Example reported revenue of $10 million.")
    ]
    client = FlakyMinimumChunkClient()
    suite = AgentSuite(client)
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    proposal = suite.discover(candidate, evidence)[0]

    claims = suite.extract_claims(proposal, evidence)

    assert client.attempts == 2
    assert claims


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


def test_relation_extraction_chunks_long_evidence_and_merges_duplicates() -> None:
    class RecordingRelationClient(HeuristicStructuredClient):
        def __init__(self) -> None:
            self.body_lengths: list[int] = []

        def _build_relations(self, context):  # noqa: ANN001
            self.body_lengths.append(len(context["evidence"][0]["body"]))
            return super()._build_relations(context)

    body = " ".join(f"Example Corp and EXM relation sentence {index}." for index in range(100))
    evidence = [document("D1", "Example launches a product", body)]
    client = RecordingRelationClient()
    suite = AgentSuite(client)
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    proposal = suite.discover(candidate, evidence)[0]

    relations = suite.build_relations(proposal, evidence)

    assert len(client.body_lengths) > 1
    assert max(client.body_lengths) <= 1_000
    assert len(relations) == 1
    assert relations[0].evidence_ids == ["D1"]


def test_relation_extraction_retries_empty_minimum_chunk_once() -> None:
    class EmptyOnceRelationClient(HeuristicStructuredClient):
        def __init__(self) -> None:
            self.attempts = 0

        def _build_relations(self, context):  # noqa: ANN001
            self.attempts += 1
            if self.attempts == 1:
                raise ValueError("build_relations returned an empty structured response")
            return super()._build_relations(context)

    evidence = [document("D1", "Example launches a product", "Example launched Widget One.")]
    client = EmptyOnceRelationClient()
    suite = AgentSuite(client)
    candidate = CandidateBundle(
        candidate_id="C1",
        evidence_ids=["D1"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
    )
    proposal = suite.discover(candidate, evidence)[0]

    relations = suite.build_relations(proposal, evidence)

    assert client.attempts == 2
    assert relations


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
