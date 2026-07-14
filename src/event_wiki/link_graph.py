from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from event_wiki.agents import AgentSuite, _stable_id
from event_wiki.config import Settings
from event_wiki.event_links import audit_event_link
from event_wiki.llm import OpenAICompatibleStructuredClient
from event_wiki.models import EventLink, WikiOperation, WikiPatch


class EventLinkState(TypedDict, total=False):
    thread_id: str
    source_event_id: str
    source: dict[str, Any]
    candidates: list[dict[str, Any]]
    evidence_ids: list[str]
    links: list[dict[str, Any]]
    patch_ids: list[str]
    auto_committed: list[str]
    suppressed: int
    blocked: int


def build_event_link_graph(repository: Any, suite: AgentSuite):
    def retrieve(state: EventLinkState):
        source = repository.get_event(state["source_event_id"])
        if source is None:
            raise KeyError(state["source_event_id"])
        candidates = repository.find_link_candidates(state["source_event_id"], limit=20)[:10]
        evidence_ids = sorted(
            {
                evidence_id
                for event in [source, *candidates]
                for evidence_id in event.get("evidence_ids", [])
            }
        )
        return {"source": source, "candidates": candidates, "evidence_ids": evidence_ids}

    def link_events(state: EventLinkState):
        evidence = repository.list_evidence(evidence_ids=state["evidence_ids"])
        links = suite.link_events(state["source"], state["candidates"], evidence)
        return {"links": [item.model_dump(mode="json") for item in links]}

    def audit_and_persist(state: EventLinkState):
        candidates = {item["event_id"]: item for item in state["candidates"]}
        evidence_list = repository.list_evidence(evidence_ids=state["evidence_ids"])
        evidence = {item.evidence_id: item for item in evidence_list}
        pending: list[str] = []
        automatic: list[str] = []
        suppressed = 0
        blocked = 0
        for raw in state["links"]:
            link = EventLink.model_validate(raw)
            audit = audit_event_link(
                link, state["source"], candidates.get(link.target_event_id), evidence
            )
            if audit.disposition == "suppressed":
                suppressed += 1
                continue
            if audit.disposition == "blocked":
                blocked += 1
                continue
            existing = repository.get_event_link(link.link_id)
            base_version = existing["current_version"] if existing else 0
            patch_id = _stable_id("patch", state["thread_id"], link.link_id, str(base_version))
            if repository.get_patch(patch_id) is not None:
                continue
            patch = WikiPatch(
                patch_id=patch_id,
                thread_id=state["thread_id"],
                operation=(
                    WikiOperation.SUPPLEMENT_EVENT_LINK
                    if existing
                    else WikiOperation.ADD_EVENT_LINK
                ),
                event_id=link.source_event_id,
                link_id=link.link_id,
                base_version=base_version,
                evidence_ids=sorted({item.evidence_id for item in link.evidence_quotes}),
                payload={"event_link": link.model_dump(mode="json")},
                audit=audit,
                model_version=suite.model_version,
                prompt_version=suite.prompt_version,
                created_at=datetime.now(UTC),
            )
            repository.create_patch(patch)
            if audit.disposition == "auto_commit":
                repository.review_patch(patch_id, "approve", reviewer="event-link-policy")
                repository.commit_approved_patch(patch_id)
                automatic.append(patch_id)
            else:
                pending.append(patch_id)
        return {
            "patch_ids": pending,
            "auto_committed": automatic,
            "suppressed": suppressed,
            "blocked": blocked,
        }

    graph = StateGraph(EventLinkState)
    graph.add_node("retrieve_link_candidates", retrieve)
    graph.add_node("link_events", link_events)
    graph.add_node("audit_event_links", audit_and_persist)
    graph.add_edge(START, "retrieve_link_candidates")
    graph.add_conditional_edges(
        "retrieve_link_candidates",
        lambda state: "link_events" if state["candidates"] else END,
        ["link_events", END],
    )
    graph.add_edge("link_events", "audit_event_links")
    graph.add_edge("audit_event_links", END)
    return graph.compile()


class EventLinkRunner:
    def __init__(self, repository: Any, suite: AgentSuite) -> None:
        self.repository = repository
        self.graph = build_event_link_graph(repository, suite)

    @classmethod
    def from_settings(
        cls, repository: Any, *, offline: bool = False, settings_obj: Settings | None = None
    ) -> EventLinkRunner:
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
        return cls(repository, AgentSuite(client))

    def run(self, event_id: str) -> dict[str, Any]:
        version = self.repository.get_event_version(event_id)
        thread_id = f"link:{event_id}:v{version}"
        try:
            result = self.graph.invoke({"thread_id": thread_id, "source_event_id": event_id})
        except Exception as error:
            if hasattr(self.repository, "save_agent_run"):
                self.repository.save_agent_run(
                    thread_id,
                    thread_id=thread_id,
                    status="failed",
                    current_node="event_link",
                    error=str(error),
                )
            raise
        if hasattr(self.repository, "save_agent_run"):
            self.repository.save_agent_run(
                thread_id,
                thread_id=thread_id,
                status="completed",
                current_node="event_link",
                metrics={
                    "pending": len(result.get("patch_ids", [])),
                    "auto_committed": len(result.get("auto_committed", [])),
                    "suppressed": result.get("suppressed", 0),
                    "blocked": result.get("blocked", 0),
                },
            )
        return result
