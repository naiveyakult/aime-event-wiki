# Architecture

## Two graphs, different responsibilities

LangGraph is the durable workflow graph. It controls agent execution, checkpoints, human
interrupts, retries, and resume behavior. The Event Wiki knowledge graph is the domain model.
It connects versioned events, entities, atomic claims, relations, and immutable evidence.

```text
Evidence ──supports──> Claim ──describes──> Event
    │                                      │
    └──────────────supports────────────────┤
                                           ├──affects──> Entity
Entity ──supplier/customer/partner/etc.──> Entity
```

Knowledge graph nodes and edges live in PostgreSQL so review approval, optimistic versioning,
and historical cutoff queries are transactional. `event_edges` is a property-edge projection
with `known_at`, `valid_from`, and evidence IDs. Graph JSON and Markdown are derived exports.
Neo4j can be added later as a read projection; it must not become the only source of truth.

## Temporal invariant

Every exported fact or edge must satisfy `known_at <= prediction_cutoff`. Event time describes
when something happened; known time describes when the market could have known it. They are not
interchangeable.

## Write authority

Agents produce validated `WikiPatch` proposals only. The deterministic committer verifies the
schema, evidence foreign keys, temporal constraints, audit result, and `base_version`. During the
MVP every patch pauses at a human review interrupt before a new Wiki version is committed.
