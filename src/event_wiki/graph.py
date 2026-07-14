from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from event_wiki.agents import AgentSuite
from event_wiki.config import Settings
from event_wiki.llm import OpenAICompatibleStructuredClient
from event_wiki.models import (
    AuditResult,
    Claim,
    EventDecision,
    EventProposal,
    Relation,
)


class GraphState(TypedDict, total=False):
    """LangGraph channel shape; values use the validated public Pydantic contracts."""

    thread_id: str
    candidate_id: str
    evidence_ids: list[str]
    proposals: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    audit: dict[str, Any]
    audits: list[dict[str, Any]]
    patch_id: str
    patch_ids: list[str]
    review_decision: str
    review_decisions: list[str]


def _get_evidence(repository: Any, evidence_ids: list[str]):
    getter = getattr(repository, "get_evidence", None)
    documents = (
        getter(evidence_ids) if getter else repository.list_evidence(evidence_ids=evidence_ids)
    )
    if isinstance(documents, dict):
        return [documents[evidence_id] for evidence_id in evidence_ids]
    return list(documents)


def build_graph(repository: Any, suite: AgentSuite, checkpointer: Any | None = None):
    """Build the resumable Event Wiki workflow over a duck-typed repository."""

    def load_candidate(state):
        candidate = repository.get_candidate(state["candidate_id"])
        return {"evidence_ids": list(candidate.evidence_ids)}

    def discover_event(state):
        candidate = repository.get_candidate(state["candidate_id"])
        evidence = _get_evidence(repository, state["evidence_ids"])
        proposals = suite.discover(candidate, evidence)
        return {"proposals": [item.model_dump(mode="json") for item in proposals]}

    def resolve_identity(state):
        proposals = [EventProposal.model_validate(item) for item in state["proposals"]]
        decisions = suite.resolve(proposals, repository)
        return {"decisions": [item.model_dump(mode="json") for item in decisions]}

    def extract_claims(state):
        evidence = _get_evidence(repository, state["evidence_ids"])
        proposals = [EventProposal.model_validate(item) for item in state["proposals"]]
        claims = [
            claim
            for proposal in proposals
            for claim in suite.extract_claims(
                proposal,
                [item for item in evidence if item.evidence_id in proposal.evidence_ids],
            )
        ]
        return {"claims": [item.model_dump(mode="json") for item in claims]}

    def build_relations(state):
        evidence = _get_evidence(repository, state["evidence_ids"])
        proposals = [EventProposal.model_validate(item) for item in state["proposals"]]
        relations = [
            relation
            for proposal in proposals
            for relation in suite.build_relations(
                proposal,
                [item for item in evidence if item.evidence_id in proposal.evidence_ids],
            )
        ]
        return {"relations": [item.model_dump(mode="json") for item in relations]}

    def audit(state):
        evidence = _get_evidence(repository, state["evidence_ids"])
        proposals = [EventProposal.model_validate(item) for item in state["proposals"]]
        claims = [Claim.model_validate(item) for item in state["claims"]]
        relations = [Relation.model_validate(item) for item in state["relations"]]
        results = []
        for proposal in proposals:
            allowed = set(proposal.evidence_ids)
            proposal_evidence = [item for item in evidence if item.evidence_id in allowed]
            proposal_claims = [
                item
                for item in claims
                if item.subject == proposal.event_subject and set(item.evidence_ids) <= allowed
            ]
            proposal_relations = [
                item
                for item in relations
                if proposal.event_subject in {item.source_entity, item.target_entity}
                and set(item.evidence_ids) <= allowed
            ]
            results.append(
                suite.audit(
                    proposal_claims,
                    proposal_relations,
                    proposal_evidence,
                    proposal.known_at,
                )
            )
        output: dict[str, Any] = {"audits": [result.model_dump(mode="json") for result in results]}
        if len(results) == 1:
            output["audit"] = results[0].model_dump(mode="json")
        return output

    def propose_patch(state):
        version_getter = getattr(repository, "get_event_version", None)
        proposals = [EventProposal.model_validate(item) for item in state["proposals"]]
        decisions = [EventDecision.model_validate(item) for item in state["decisions"]]
        base_versions = []
        for proposal, decision in zip(proposals, decisions, strict=True):
            if not decision.existing_event_id:
                base_versions.append(0)
            elif version_getter:
                base_versions.append(version_getter(decision.existing_event_id))
            else:
                matches = repository.find_existing_events(
                    subject=proposal.event_subject,
                    event_family=proposal.event_family,
                    around=proposal.event_time,
                )
                match = next(
                    item for item in matches if item["event_id"] == decision.existing_event_id
                )
                base_versions.append(int(match["current_version"]))
        patches = suite.propose_patches(
            thread_id=state["thread_id"],
            proposals=proposals,
            decisions=decisions,
            claims=[Claim.model_validate(item) for item in state["claims"]],
            relations=[Relation.model_validate(item) for item in state["relations"]],
            audits=[AuditResult.model_validate(item) for item in state["audits"]],
            base_versions=base_versions,
        )
        saver = getattr(repository, "save_patch", None) or repository.create_patch
        for patch in patches:
            saver(patch)
        result: dict[str, Any] = {"patch_ids": [patch.patch_id for patch in patches]}
        if len(patches) == 1:
            result["patch_id"] = patches[0].patch_id
        return result

    def review(state):
        decisions = []
        for patch_id in state["patch_ids"]:
            response = interrupt(
                {
                    "kind": "wiki_patch_review",
                    "patch_id": patch_id,
                    "audit_status": (repository.get_patch(patch_id) or {})
                    .get("audit", {})
                    .get("status"),
                }
            )
            decision = response.get("decision") if isinstance(response, dict) else response
            if decision not in {"approve", "reject"}:
                raise ValueError("review decision must be 'approve' or 'reject'")
            reviewer = getattr(repository, "review_patch", None)
            if reviewer:
                stored = repository.get_patch(patch_id)
                if stored and stored.get("status") == "pending":
                    reviewer(patch_id, decision, reviewer="langgraph")
            decisions.append(decision)
        result = {"review_decisions": decisions}
        if len(decisions) == 1:
            result["review_decision"] = decisions[0]
        return result

    def commit(state):
        committed_event_ids = []
        for patch_id, decision in zip(state["patch_ids"], state["review_decisions"], strict=True):
            if decision != "approve":
                continue
            committer = getattr(repository, "commit_patch", None)
            if committer:
                committer(patch_id)
            else:
                repository.commit_approved_patch(patch_id)
            stored = repository.get_patch(patch_id) or {}
            if stored.get("event_id"):
                committed_event_ids.append(stored["event_id"])
        if committed_event_ids and hasattr(repository, "find_link_candidates"):
            from event_wiki.link_graph import EventLinkRunner

            linker = EventLinkRunner(repository, suite)
            for event_id in committed_event_ids:
                linker.run(event_id)
        return {}

    graph = StateGraph(GraphState)
    nodes = {
        "load_candidate": load_candidate,
        "discover_event": discover_event,
        "resolve_identity": resolve_identity,
        "extract_claims": extract_claims,
        "build_relations": build_relations,
        "audit": audit,
        "propose_patch": propose_patch,
        "review_interrupt": review,
        "commit_version": commit,
    }
    for name, node in nodes.items():
        graph.add_node(name, node)
    graph.add_edge(START, "load_candidate")
    sequence = [
        "resolve_identity",
        "extract_claims",
        "build_relations",
        "audit",
        "propose_patch",
        "review_interrupt",
    ]
    graph.add_edge("load_candidate", "discover_event")
    graph.add_conditional_edges(
        "discover_event",
        lambda state: "resolve_identity" if state["proposals"] else END,
        ["resolve_identity", END],
    )
    for source, target in zip(sequence, sequence[1:], strict=False):
        graph.add_edge(source, target)
    graph.add_edge("review_interrupt", "commit_version")
    graph.add_edge("commit_version", END)
    return graph.compile(checkpointer=checkpointer)


@contextmanager
def postgres_checkpointer(database_url: str, *, setup: bool = True) -> Iterator[Any]:
    """Yield a production PostgresSaver without importing its optional driver eagerly."""

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:  # pragma: no cover - depends on optional production extra
        raise RuntimeError(
            "Postgres checkpoint support requires langgraph-checkpoint-postgres"
        ) from exc
    with PostgresSaver.from_conn_string(database_url) as saver:
        if setup:
            saver.setup()
        yield saver


class GraphRunner:
    """Application service used by the CLI and review app."""

    def __init__(self, repository: Any, suite: AgentSuite, checkpointer: Any) -> None:
        self.repository = repository
        self.suite = suite
        self.checkpointer = checkpointer
        self.graph = build_graph(repository, suite, checkpointer)
        self._resources: ExitStack | None = None

    @classmethod
    def from_settings(
        cls,
        repository: Any,
        *,
        offline: bool = False,
        settings_obj: Settings | None = None,
    ) -> GraphRunner:
        from event_wiki.agents import HeuristicStructuredClient

        configuration = settings_obj or Settings()
        client = (
            HeuristicStructuredClient()
            if offline
            else OpenAICompatibleStructuredClient(
                api_key=configuration.openai_api_key,
                base_url=configuration.openai_base_url,
                model=configuration.openai_model,
                timeout=configuration.llm_timeout_seconds,
                max_retries=configuration.llm_max_retries,
            )
        )
        stack = ExitStack()
        try:
            checkpointer = stack.enter_context(
                postgres_checkpointer(configuration.langgraph_database_url)
            )
        except Exception:
            stack.close()
            if not offline:
                raise
            checkpointer = MemorySaver()
            stack = ExitStack()
        runner = cls(repository, AgentSuite(client), checkpointer)
        runner._resources = stack
        return runner

    def close(self) -> None:
        if self._resources:
            self._resources.close()
            self._resources = None

    def run_pending(self, *, limit: int | None = None) -> int:
        candidates = self.repository.list_candidates(status="pending", limit=limit)
        listed = getattr(self.repository, "list_patches", None)
        existing_threads = {
            item["thread_id"] for item in (listed() if listed else []) if item.get("thread_id")
        }
        count = 0
        status_updater = getattr(self.repository, "update_candidate_status", None)
        for candidate in candidates:
            if candidate.candidate_id in existing_threads:
                if status_updater:
                    status_updater(candidate.candidate_id, "awaiting_review")
                continue
            config = {"configurable": {"thread_id": candidate.candidate_id}}
            if status_updater:
                status_updater(candidate.candidate_id, "processing")
            try:
                result = self.graph.invoke(
                    {"thread_id": candidate.candidate_id, "candidate_id": candidate.candidate_id},
                    config=config,
                )
            except Exception:
                if status_updater:
                    status_updater(candidate.candidate_id, "error")
                raise
            if status_updater:
                status_updater(
                    candidate.candidate_id,
                    "awaiting_review" if result.get("patch_ids") else "no_event",
                )
            count += 1
        return count

    def retry(self, run_id: str) -> Any:
        config = {"configurable": {"thread_id": run_id}}
        snapshot = self.graph.get_state(config)
        if snapshot.values:
            return self.graph.invoke(None, config=config)
        candidate = self.repository.get_candidate(run_id)
        if candidate is None:
            raise KeyError(run_id)
        return self.graph.invoke({"thread_id": run_id, "candidate_id": run_id}, config=config)

    def resume_patch(self, patch_id: str, decision: str | None = None) -> Any:
        patch = self.repository.get_patch(patch_id)
        if patch is None:
            raise KeyError(patch_id)
        normalized = decision.lower() if decision else None
        status = str(patch.get("status", "pending")).lower()
        if patch.get("operation") in {"add_event_link", "supplement_event_link"}:
            if normalized not in {"approve", "reject"}:
                raise ValueError("event link review decision must be approve or reject")
            if status == "pending":
                self.repository.review_patch(patch_id, normalized, reviewer="langgraph")
                status = normalized
            if normalized == "approve" and status in {"approve", "approved"}:
                return self.repository.commit_approved_patch(patch_id)
            return None
        if normalized is None:
            if status in {"approved", "committed"}:
                normalized = "approve"
            elif status == "rejected":
                normalized = "reject"
            else:
                raise ValueError("a pending patch requires an explicit review decision")
        if normalized == "rerun":
            if status == "pending":
                reset = getattr(self.repository, "reset_patch_for_rerun", None)
                if reset is None:
                    raise ValueError("repository does not support rerun")
                patch = reset(patch_id, reviewer="langgraph")
            elif status != "rerun_requested":
                raise ValueError(f"patch cannot be rerun from status {status}")
            original_thread = str(patch["thread_id"])
            candidate_id = original_thread.split(":rerun:", 1)[0]
            rerun_thread = f"{candidate_id}:rerun:{patch_id}"
            config = {"configurable": {"thread_id": rerun_thread}}
            return self.graph.invoke(
                {"thread_id": rerun_thread, "candidate_id": candidate_id},
                config=config,
            )
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve, reject, or rerun")
        config = {"configurable": {"thread_id": patch["thread_id"]}}
        snapshot = self.graph.get_state(config)
        if not snapshot.values:
            reviewer = getattr(self.repository, "review_patch", None)
            if status == "pending":
                if reviewer is None:
                    raise ValueError("repository does not support review recovery")
                reviewer(patch_id, normalized, reviewer="langgraph-recovery")
            if normalized == "reject":
                return None
            version = self.repository.commit_approved_patch(patch_id)
            stored = self.repository.get_patch(patch_id) or patch
            event_id = stored.get("event_id")
            result = {"patch_id": patch_id, "event_id": event_id, "version": version}
            if event_id and hasattr(self.repository, "find_link_candidates"):
                from event_wiki.link_graph import EventLinkRunner

                result["event_links"] = EventLinkRunner(self.repository, self.suite).run(event_id)
            return result
        if snapshot.interrupts:
            current = snapshot.interrupts[0].value
            expected_patch_id = current.get("patch_id") if isinstance(current, dict) else None
            if expected_patch_id and expected_patch_id != patch_id:
                raise ValueError(f"patch {expected_patch_id} must be reviewed before {patch_id}")
        return self.graph.invoke(
            Command(resume={"decision": normalized}),
            config=config,
        )
