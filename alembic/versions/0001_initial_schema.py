"""initial schema

Revision ID: 0001
Revises:
Create Date: 2024-01-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "prompt_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("prompt_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("response", sa.Text, nullable=True),
        sa.Column("cached", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("retry_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("processing_time_ms", sa.Integer, nullable=True),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint("uq_prompt_requests_prompt_id", "prompt_requests", ["prompt_id"])
    op.create_index("ix_prompt_requests_prompt_id", "prompt_requests", ["prompt_id"])
    op.create_index("ix_prompt_requests_user_id", "prompt_requests", ["user_id"])
    op.create_index("ix_prompt_requests_status", "prompt_requests", ["status"])
    op.create_index("ix_prompt_requests_task_id", "prompt_requests", ["task_id"])

    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN NEW.updated_at = now(); RETURN NEW; END;
        $$ language 'plpgsql';
    """)
    op.execute("""
        CREATE TRIGGER prompt_requests_updated_at
        BEFORE UPDATE ON prompt_requests
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    """)

    op.create_table(
        "semantic_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("prompt_text", sa.Text, nullable=False),
        sa.Column("embedding_json", sa.Text, nullable=False),
        sa.Column("response", sa.Text, nullable=False),
        sa.Column("hit_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("semantic_cache")
    op.drop_table("prompt_requests")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column CASCADE")