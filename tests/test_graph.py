from datetime import UTC, datetime
from tempfile import TemporaryDirectory

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from event_wiki.agents import AgentSuite, HeuristicStructuredClient
from event_wiki.db import Repository
from event_wiki.graph import GraphRunner, build_graph
from event_wiki.models import CandidateBundle, EvidenceDocument

NOW = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)


class FakeRepository:
    def __init__(self) -> None:
        self.candidate = CandidateBundle(
            candidate_id="C1",
            evidence_ids=["D1"],
            window_start=NOW,
            window_end=NOW,
            symbols=["EXM"],
            entity_names=["Example Corp"],
        )
        self.evidence = {
            "D1": EvidenceDocument(
                evidence_id="D1",
                content_type="US_NEWS",
                title="Example reports quarterly earnings",
                body="Example reported revenue of $10 million.",
                published_at=NOW,
                known_at=NOW,
                source_name="Synthetic Wire",
                symbols=["EXM"],
                entity_names=["Example Corp"],
                source_locator="synthetic://D1",
                content_hash="a" * 64,
            )
        }
        self.patches = {}
        self.committed = []

    def get_candidate(self, candidate_id):
        assert candidate_id == "C1"
        return self.candidate

    def get_evidence(self, evidence_ids):
        return [self.evidence[evidence_id] for evidence_id in evidence_ids]

    def find_events(self, **_filters):
        return []

    def save_patch(self, patch):
        self.patches[patch.patch_id] = patch

    def commit_patch(self, patch_id):
        self.committed.append(patch_id)

    def list_candidates(self, **_filters):
        return [self.candidate]

    def list_patches(self):
        return [
            {
                "patch_id": patch.patch_id,
                "thread_id": patch.thread_id,
                "status": "pending",
            }
            for patch in self.patches.values()
        ]

    def get_patch(self, patch_id):
        patch = self.patches.get(patch_id)
        if not patch:
            return None
        return {
            "patch_id": patch.patch_id,
            "thread_id": patch.thread_id,
            "status": "pending",
        }

    def reset_patch_for_rerun(self, patch_id, reviewer="web"):
        patch = self.patches[patch_id]
        return {
            "patch_id": patch.patch_id,
            "thread_id": patch.thread_id,
            "status": "rerun_requested",
        }


def run_to_review(repo: FakeRepository):
    graph = build_graph(repo, AgentSuite(HeuristicStructuredClient()), MemorySaver())
    config = {"configurable": {"thread_id": "C1"}}
    graph.invoke({"thread_id": "C1", "candidate_id": "C1"}, config=config)
    assert len(repo.patches) == 1
    return graph, config


def test_graph_interrupts_for_review_and_commits_only_after_approval() -> None:
    repo = FakeRepository()
    graph, config = run_to_review(repo)
    assert repo.committed == []

    final = graph.invoke(Command(resume={"decision": "approve"}), config=config)

    assert final["review_decision"] == "approve"
    assert repo.committed == [final["patch_id"]]


def test_graph_rejection_never_commits() -> None:
    repo = FakeRepository()
    graph, config = run_to_review(repo)

    final = graph.invoke(Command(resume={"decision": "reject"}), config=config)

    assert final["review_decision"] == "reject"
    assert repo.committed == []


def test_graph_finishes_without_patch_when_candidate_contains_no_target_event() -> None:
    repo = FakeRepository()
    repo.evidence["D1"] = repo.evidence["D1"].model_copy(
        update={"title": "Daily market recap", "body": "Stocks moved during the session."}
    )
    graph = build_graph(repo, AgentSuite(HeuristicStructuredClient()), MemorySaver())
    config = {"configurable": {"thread_id": "C1"}}

    final = graph.invoke({"thread_id": "C1", "candidate_id": "C1"}, config=config)

    assert final["proposals"] == []
    assert repo.patches == {}


def test_graph_runner_runs_pending_and_resumes_patch() -> None:
    repo = FakeRepository()
    runner = GraphRunner(repo, AgentSuite(HeuristicStructuredClient()), MemorySaver())

    assert runner.run_pending() == 1
    patch_id = next(iter(repo.patches))
    runner.resume_patch(patch_id, "approve")

    assert repo.committed == [patch_id]


def test_graph_runner_rerun_uses_fresh_revision_thread() -> None:
    repo = FakeRepository()
    runner = GraphRunner(repo, AgentSuite(HeuristicStructuredClient()), MemorySaver())
    assert runner.run_pending() == 1
    original_patch_id = next(iter(repo.patches))

    result = runner.resume_patch(original_patch_id, "rerun")

    assert result["thread_id"] == f"C1:rerun:{original_patch_id}"
    assert any(
        patch.thread_id == f"C1:rerun:{original_patch_id}" for patch in repo.patches.values()
    )


def test_two_events_create_two_independent_patches_and_versions() -> None:
    with TemporaryDirectory() as directory:
        repo = Repository.from_url(f"sqlite+pysqlite:///{directory}/wiki.db", create_schema=True)
        documents = [
            document
            for document in (
                EvidenceDocument(
                    evidence_id="D1",
                    content_type="US_NEWS",
                    title="Example reports quarterly earnings",
                    body="Example reported revenue of $10 million.",
                    published_at=NOW,
                    known_at=NOW,
                    source_name="Synthetic Wire",
                    symbols=["EXM"],
                    entity_names=["Example Corp"],
                    source_locator="synthetic://D1",
                    content_hash="a" * 64,
                ),
                EvidenceDocument(
                    evidence_id="D2",
                    content_type="US_NEWS",
                    title="Other Corp launches a new product",
                    body="Other Corp launched Widget Two.",
                    published_at=NOW,
                    known_at=NOW,
                    source_name="Synthetic Wire",
                    symbols=["OTH"],
                    entity_names=["Other Corp"],
                    source_locator="synthetic://D2",
                    content_hash="b" * 64,
                ),
            )
        ]
        for item in documents:
            repo.upsert_evidence(item)
        repo.save_candidate(
            CandidateBundle(
                candidate_id="C2",
                evidence_ids=["D1", "D2"],
                window_start=NOW,
                window_end=NOW,
                symbols=["EXM", "OTH"],
                entity_names=["Example Corp", "Other Corp"],
            )
        )
        runner = GraphRunner(repo, AgentSuite(HeuristicStructuredClient()), MemorySaver())

        assert runner.run_pending() == 1
        patches = repo.list_pending_patches()
        assert len(patches) == 2
        first, second = patches
        repo.review_patch(first["patch_id"], "approve", reviewer="test")
        runner.resume_patch(first["patch_id"])
        assert repo.status_counts()["wiki_versions"] == 0
        repo.review_patch(second["patch_id"], "approve", reviewer="test")
        runner.resume_patch(second["patch_id"])

        assert repo.status_counts()["events"] == 2
        assert repo.status_counts()["wiki_versions"] == 2
        versions = repo.list_approved_versions()
        for version in versions:
            event_evidence = set(version["snapshot"]["event"].get("evidence_ids", []))
            for claim in version["snapshot"]["claims"]:
                assert set(claim["evidence_ids"]) <= event_evidence
            for relation in version["snapshot"]["relations"]:
                assert set(relation["evidence_ids"]) <= event_evidence
