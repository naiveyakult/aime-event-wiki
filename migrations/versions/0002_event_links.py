"""增加跨事件链接及其独立版本。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    patch_columns = {item["name"] for item in inspector.get_columns("wiki_patches")}
    if "link_id" not in patch_columns:
        op.add_column("wiki_patches", sa.Column("link_id", sa.String(255), nullable=True))
        op.create_index("ix_wiki_patches_link_id", "wiki_patches", ["link_id"])
    if inspector.has_table("event_links"):
        return
    op.create_table(
        "event_links",
        sa.Column("link_id", sa.String(255), primary_key=True),
        sa.Column("source_event_id", sa.String(255), sa.ForeignKey("events.event_id")),
        sa.Column("target_event_id", sa.String(255), sa.ForeignKey("events.event_id")),
        sa.Column("link_type", sa.String(64), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("snapshot", sa.JSON(), nullable=False),
    )
    op.create_index("ix_event_links_source_event_id", "event_links", ["source_event_id"])
    op.create_index("ix_event_links_target_event_id", "event_links", ["target_event_id"])
    op.create_index("ix_event_links_link_type", "event_links", ["link_type"])
    op.create_index("ix_event_links_status", "event_links", ["status"])
    op.create_table(
        "event_link_versions",
        sa.Column("version_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("link_id", sa.String(255), sa.ForeignKey("event_links.link_id")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("patch_id", sa.String(255), sa.ForeignKey("wiki_patches.patch_id"), unique=True),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("link_id", "version", name="uq_event_link_version"),
    )
    op.create_index("ix_event_link_versions_link_id", "event_link_versions", ["link_id"])
    op.create_index("ix_event_link_versions_known_at", "event_link_versions", ["known_at"])


def downgrade() -> None:
    op.drop_table("event_link_versions")
    op.drop_table("event_links")
    op.drop_index("ix_wiki_patches_link_id", table_name="wiki_patches")
    op.drop_column("wiki_patches", "link_id")
