"""merge telephony streams: siprec + uc + teams + audiohook

Revision ID: 9b8d07fc6ee9
Revises: audiohook_001, siprec_001_initial, teams_001_teams_call_record, uc_001_uc_recording_job
Create Date: 2026-05-07 14:11:39.151817
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = '9b8d07fc6ee9'
down_revision: Union[str, None] = ('audiohook_001', 'siprec_001_initial', 'teams_001_teams_call_record', 'uc_001_uc_recording_job')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
