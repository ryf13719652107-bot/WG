"""add_strategy_source

Revision ID: c1a2b3d4e5f6
Revises: 96fcf209e656
Create Date: 2026-07-24 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1a2b3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "96fcf209e656"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        if "source" not in cols:
            op.add_column(
                "strategies",
                sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
            )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if "strategies" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("strategies")]
        if "source" in cols:
            op.drop_column("strategies", "source")
