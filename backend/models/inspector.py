"""
models/inspector.py
Inspector-specific profile data: KYC (BVN/NIN), bank details, liveness
verification, and payout tracking. Kept separate from the core `users`
table since only inspectors need these fields.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Numeric, Integer, Index, Text
)
from sqlalchemy.dialects.postgresql import UUID
from backend.core.database import Base
import enum


class RegistrationStage(str, enum.Enum):
    DETAILS = "details"
    VERIFICATION = "verification"
    LIVENESS = "liveness"
    COMPLETE = "complete"


class PayoutStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    PAID = "paid"
    REJECTED = "rejected"


class InspectorProfile(Base):
    """
    One row per inspector. Created at stage 1 of registration and
    filled in progressively through stage 3.
    """
    __tablename__ = "inspector_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(50), ForeignKey("users.user_id"), unique=True, nullable=False, index=True)

    sex = Column(String(10))

    # KYC — set once at stage 2, immutable after (enforced in router, not DB)
    bvn = Column(String(11))
    nin = Column(String(11))
    kyc_verified = Column(Boolean, default=False)  # flips True once real gov't API is wired in

    # Bank details — editable, account_name is auto-resolved (read-only to user)
    bank_account_number = Column(String(10))
    bank_name = Column(String(100))
    bank_account_name = Column(String(150))

    # Liveness
    liveness_verified = Column(Boolean, default=False)  # mock until real face-match API exists
    liveness_photo_url = Column(String(500))

    registration_stage = Column(String(20), default=RegistrationStage.DETAILS, nullable=False)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index('idx_inspector_profile_stage', 'registration_stage'),
    )


class PayoutRequest(Base):
    """
    One row per payout request. An inspector can request at most one
    payout per fixed biweekly period (1st-14th, 15th-end of month),
    and only if that period's revenue meets the minimum threshold.
    """
    __tablename__ = "payout_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payout_id = Column(String(50), unique=True, nullable=False, index=True)
    inspector_id = Column(String(50), ForeignKey("users.user_id"), nullable=False, index=True)

    amount_ngn = Column(Numeric(12, 2), nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    dcp_count = Column(Integer, default=0)

    status = Column(String(20), default=PayoutStatus.PENDING, nullable=False)

    requested_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    processed_at = Column(DateTime(timezone=True))
    processed_by = Column(String(50))  # admin user_id who approved/rejected/paid
    notes = Column(Text)

    __table_args__ = (
        Index('idx_payout_inspector', 'inspector_id'),
        Index('idx_payout_status', 'status'),
    )
