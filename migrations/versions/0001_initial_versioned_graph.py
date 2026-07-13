"""Initial versioned Event Wiki and temporal knowledge graph schema."""

from collections.abc import Sequence

from alembic import op

from event_wiki.db import Base

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The initial revision deliberately creates the complete immutable baseline from
    # one metadata definition. Later revisions must use explicit ALTER operations.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    # Reverse dependency order keeps PostgreSQL foreign-key teardown deterministic.
    for table_name in (
        "agent_runs",
        "review_actions",
        "wiki_versions",
        "event_edges",
        "relations",
        "claims",
        "event_aliases",
        "wiki_patches",
        "events",
        "candidate_bundles",
        "evidence_documents",
    ):
        op.drop_table(table_name)
