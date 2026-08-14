"""
routers/inspector.py
Inspector self-service: 3-stage self-registration (no admin needed),
profile view/edit, revenue tracking, and payout requests.
"""
import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from backend.core.database import get_db
from backend.core.security import hash_password, verify_password, create_access_token, get_current_user
from backend.models.user import User
from backend.models.inspector import InspectorProfile, PayoutRequest, RegistrationStage, PayoutStatus
from backend.models.dcp import DCPRecord
from backend.schemas.inspector import (
    InspectorLoginRequest, InspectorRegisterStage1, InspectorRegisterStage2,
    InspectorRegisterStage3, InspectorProfileUpdate
)
from backend.services.kyc_service import verify_bvn_nin, resolve_bank_account, upload_liveness_photo

router = APIRouter(prefix="/inspectors", tags=["Inspector Self-Service"])

PAYOUT_THRESHOLD_NGN = 20000
FEE_PER_DCP_NGN = 5000


async def require_inspector(current_user: dict = Depends(get_current_user)):
    """Dependency: reject non-inspector users from inspector-only endpoints."""
    if current_user.get("role") != "inspector":
        raise HTTPException(status_code=403, detail="Inspector access required")
    return current_user


# ── LOGIN ──────────────────────────────────────────────────────

@router.post("/login", response_model=dict)
async def inspector_login(request: InspectorLoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Dedicated login for the Inspector Portal. Only 'inspector' and 'admin'
    roles are accepted here — everyone else (fleet_owner, private_owner,
    reseller, mechanic) is rejected, mirroring the frontend's
    Auth.requireAuth(['inspector', 'admin']) check, but enforced
    server-side so it can't be bypassed by calling the API directly.
    """
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")

    if user.role not in ("inspector", "admin"):
        raise HTTPException(403, "This login is for inspectors and admin staff only.")

    profile_result = await db.execute(select(InspectorProfile).where(InspectorProfile.user_id == user.user_id))
    profile = profile_result.scalar_one_or_none()
    registration_stage = profile.registration_stage if profile else None

    # Admins have no inspector_profiles row — always let them through.
    # Inspectors mid-registration (stage 1/2 done, not yet complete) are
    # still allowed to log back in to resume — is_active only blocks
    # accounts that finished registration and were later suspended.
    if user.role == "inspector" and not user.is_active and registration_stage == RegistrationStage.COMPLETE:
        raise HTTPException(403, "Account suspended. Contact support.")

    token = create_access_token(data={"sub": user.user_id, "role": user.role, "email": user.email})
    return {
        "success": True,
        "access_token": token,
        "token_type": "bearer",
        "registration_stage": registration_stage or "complete",  # admins: treat as complete
        "user": {
            "user_id": user.user_id,
            "full_name": user.full_name,
            "email": user.email,
            "role": user.role
        }
    }


def get_current_biweekly_period(now: datetime = None):
    """
    Fixed biweekly cycles: 1st-14th, and 15th-end of month.
    Returns (period_start, period_end) as timezone-aware UTC datetimes.
    """
    now = now or datetime.now(timezone.utc)
    if now.day <= 14:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(day=14, hour=23, minute=59, second=59, microsecond=999999)
    else:
        start = now.replace(day=15, hour=0, minute=0, second=0, microsecond=0)
        if now.month == 12:
            next_month = now.replace(year=now.year + 1, month=1, day=1)
        else:
            next_month = now.replace(month=now.month + 1, day=1)
        last_day = (next_month - timedelta(days=1)).day
        end = now.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


# ── STAGE 1: BASIC DETAILS ────────────────────────────────────

@router.post("/register/stage1", response_model=dict)
async def register_stage1(request: InspectorRegisterStage1, db: AsyncSession = Depends(get_db)):
    """Public endpoint. Creates the user + a blank inspector profile."""
    existing = await db.execute(select(User).where(User.email == request.email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    existing_phone = await db.execute(select(User).where(User.phone == request.phone))
    if existing_phone.scalar_one_or_none():
        raise HTTPException(400, "Phone already registered")

    user_id = f"USR-{str(uuid.uuid4())[:8].upper()}"
    user = User(
        user_id=user_id,
        email=request.email,
        phone=request.phone,
        full_name=request.full_name,
        password_hash=hash_password(request.password),
        role="inspector",
        is_active=False,  # activated only once stage 3 (liveness) completes
        subscription_plan="free",
        subscription_status="trial",
    )
    db.add(user)

    profile = InspectorProfile(
        user_id=user_id,
        sex=request.sex,
        registration_stage=RegistrationStage.DETAILS,
    )
    db.add(profile)
    await db.flush()

    token = create_access_token(data={"sub": user_id, "role": "inspector", "email": request.email})
    return {
        "success": True,
        "message": "Stage 1 complete. Continue to identity verification.",
        "access_token": token,
        "token_type": "bearer",
        "user_id": user_id,
        "registration_stage": "details"
    }


# ── STAGE 2: KYC + BANK DETAILS ───────────────────────────────

@router.post("/register/stage2", response_model=dict)
async def register_stage2(
    request: InspectorRegisterStage2,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Requires the bearer token returned by stage 1.
    BVN/NIN become permanently locked once set here.
    """
    result = await db.execute(select(InspectorProfile).where(InspectorProfile.user_id == current_user["user_id"]))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "Inspector profile not found. Complete stage 1 first.")
    if profile.registration_stage != RegistrationStage.DETAILS:
        raise HTTPException(400, f"Stage 2 not available — current stage is '{profile.registration_stage}'")
    if profile.bvn or profile.nin:
        raise HTTPException(400, "BVN/NIN already submitted and cannot be changed.")

    user_result = await db.execute(select(User).where(User.user_id == current_user["user_id"]))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    kyc_result = await verify_bvn_nin(request.bvn, request.nin, user.full_name)
    if not kyc_result["verified"]:
        raise HTTPException(400, kyc_result.get("error", "BVN/NIN verification failed"))

    bank_result = await resolve_bank_account(request.bank_account_number, request.bank_name, user.full_name)
    if not bank_result["resolved"]:
        raise HTTPException(400, bank_result.get("error", "Could not resolve bank account"))

    profile.bvn = request.bvn
    profile.nin = request.nin
    profile.bank_account_number = request.bank_account_number
    profile.bank_name = request.bank_name
    profile.bank_account_name = bank_result["account_name"]
    profile.kyc_verified = False  # stays False until real gov't API is connected
    profile.registration_stage = RegistrationStage.VERIFICATION
    await db.flush()

    return {
        "success": True,
        "message": "Stage 2 complete. Continue to liveness check.",
        "bank_account_name": bank_result["account_name"],
        "registration_stage": "verification"
    }


# ── STAGE 3: LIVENESS CHECK ───────────────────────────────────

@router.post("/register/stage3", response_model=dict)
async def register_stage3(
    request: InspectorRegisterStage3,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Final stage. Activates the account on success."""
    result = await db.execute(select(InspectorProfile).where(InspectorProfile.user_id == current_user["user_id"]))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "Inspector profile not found.")
    if profile.registration_stage != RegistrationStage.VERIFICATION:
        raise HTTPException(400, f"Stage 3 not available — current stage is '{profile.registration_stage}'")

    try:
        photo_url = await upload_liveness_photo(request.liveness_image_base64, current_user["user_id"])
    except ValueError as e:
        raise HTTPException(400, str(e))

    profile.liveness_photo_url = photo_url
    profile.liveness_verified = True  # mock — real face-match not yet connected
    profile.registration_stage = RegistrationStage.COMPLETE
    await db.flush()

    user_result = await db.execute(select(User).where(User.user_id == current_user["user_id"]))
    user = user_result.scalar_one_or_none()
    user.is_active = True
    user.email_verified = True
    await db.flush()

    return {
        "success": True,
        "message": "Registration complete. You can now start accepting inspections.",
        "registration_stage": "complete"
    }


# ── PROFILE ────────────────────────────────────────────────────

@router.get("/me", response_model=dict)
async def get_my_profile(db: AsyncSession = Depends(get_db), current_user: dict = Depends(require_inspector)):
    user_result = await db.execute(select(User).where(User.user_id == current_user["user_id"]))
    user = user_result.scalar_one_or_none()
    profile_result = await db.execute(select(InspectorProfile).where(InspectorProfile.user_id == current_user["user_id"]))
    profile = profile_result.scalar_one_or_none()

    if not user or not profile:
        raise HTTPException(404, "Profile not found")

    return {
        "success": True,
        "profile": {
            "user_id": user.user_id,
            "full_name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "sex": profile.sex,
            "bvn": profile.bvn,
            "nin": profile.nin,
            "kyc_verified": profile.kyc_verified,
            "bank_account_number": profile.bank_account_number,
            "bank_name": profile.bank_name,
            "bank_account_name": profile.bank_account_name,
            "liveness_verified": profile.liveness_verified,
            "registration_stage": profile.registration_stage,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }
    }


@router.put("/me", response_model=dict)
async def update_my_profile(
    request: InspectorProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_inspector)
):
    """BVN/NIN are intentionally not editable here — that's enforced by simply
    not including them in InspectorProfileUpdate at all."""
    user_result = await db.execute(select(User).where(User.user_id == current_user["user_id"]))
    user = user_result.scalar_one_or_none()
    profile_result = await db.execute(select(InspectorProfile).where(InspectorProfile.user_id == current_user["user_id"]))
    profile = profile_result.scalar_one_or_none()

    if not user or not profile:
        raise HTTPException(404, "Profile not found")

    if request.full_name:
        user.full_name = request.full_name
    if request.phone:
        user.phone = request.phone
    if request.sex:
        profile.sex = request.sex

    if request.bank_account_number and request.bank_name:
        bank_result = await resolve_bank_account(request.bank_account_number, request.bank_name, user.full_name)
        if not bank_result["resolved"]:
            raise HTTPException(400, bank_result.get("error", "Could not resolve bank account"))
        profile.bank_account_number = request.bank_account_number
        profile.bank_name = request.bank_name
        profile.bank_account_name = bank_result["account_name"]

    await db.flush()
    return {"success": True, "message": "Profile updated"}


# ── REVENUE ────────────────────────────────────────────────────

@router.get("/me/revenue", response_model=dict)
async def get_my_revenue(db: AsyncSession = Depends(get_db), current_user: dict = Depends(require_inspector)):
    inspector_id = current_user["user_id"]
    period_start, period_end = get_current_biweekly_period()

    period_count = await db.scalar(
        select(func.count(DCPRecord.id)).where(
            and_(
                DCPRecord.auditor_id == inspector_id,
                DCPRecord.issued_at >= period_start,
                DCPRecord.issued_at <= period_end
            )
        )
    ) or 0

    lifetime_count = await db.scalar(
        select(func.count(DCPRecord.id)).where(DCPRecord.auditor_id == inspector_id)
    ) or 0

    period_revenue = period_count * FEE_PER_DCP_NGN
    lifetime_revenue = lifetime_count * FEE_PER_DCP_NGN

    existing_request = await db.execute(
        select(PayoutRequest).where(
            and_(
                PayoutRequest.inspector_id == inspector_id,
                PayoutRequest.period_start == period_start,
                PayoutRequest.period_end == period_end,
            )
        )
    )
    already_requested = existing_request.scalar_one_or_none() is not None

    return {
        "success": True,
        "current_period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "dcps_issued": period_count,
            "revenue_ngn": period_revenue,
        },
        "lifetime": {
            "dcps_issued": lifetime_count,
            "revenue_ngn": lifetime_revenue,
        },
        "payout_threshold_ngn": PAYOUT_THRESHOLD_NGN,
        "eligible_for_payout": period_revenue >= PAYOUT_THRESHOLD_NGN and not already_requested,
        "already_requested_this_period": already_requested,
    }


# ── PAYOUTS ────────────────────────────────────────────────────

@router.post("/me/payout-request", response_model=dict)
async def request_payout(db: AsyncSession = Depends(get_db), current_user: dict = Depends(require_inspector)):
    inspector_id = current_user["user_id"]
    period_start, period_end = get_current_biweekly_period()

    existing = await db.execute(
        select(PayoutRequest).where(
            and_(
                PayoutRequest.inspector_id == inspector_id,
                PayoutRequest.period_start == period_start,
                PayoutRequest.period_end == period_end,
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "You have already requested a payout for this period.")

    period_count = await db.scalar(
        select(func.count(DCPRecord.id)).where(
            and_(
                DCPRecord.auditor_id == inspector_id,
                DCPRecord.issued_at >= period_start,
                DCPRecord.issued_at <= period_end
            )
        )
    ) or 0
    period_revenue = period_count * FEE_PER_DCP_NGN

    if period_revenue < PAYOUT_THRESHOLD_NGN:
        raise HTTPException(
            400,
            f"Minimum payout threshold is NGN {PAYOUT_THRESHOLD_NGN:,}. "
            f"Your current period revenue is NGN {period_revenue:,}."
        )

    payout_id = f"PAY-{str(uuid.uuid4())[:8].upper()}"
    payout = PayoutRequest(
        payout_id=payout_id,
        inspector_id=inspector_id,
        amount_ngn=period_revenue,
        period_start=period_start,
        period_end=period_end,
        dcp_count=period_count,
        status=PayoutStatus.PENDING,
    )
    db.add(payout)
    await db.flush()

    return {
        "success": True,
        "message": "Payout request submitted.",
        "payout_id": payout_id,
        "amount_ngn": period_revenue,
    }


@router.get("/me/payouts", response_model=dict)
async def list_my_payouts(db: AsyncSession = Depends(get_db), current_user: dict = Depends(require_inspector)):
    result = await db.execute(
        select(PayoutRequest)
        .where(PayoutRequest.inspector_id == current_user["user_id"])
        .order_by(PayoutRequest.requested_at.desc())
    )
    payouts = result.scalars().all()
    return {
        "success": True,
        "payouts": [
            {
                "payout_id": p.payout_id,
                "amount_ngn": float(p.amount_ngn),
                "period_start": p.period_start.isoformat(),
                "period_end": p.period_end.isoformat(),
                "dcp_count": p.dcp_count,
                "status": p.status,
                "requested_at": p.requested_at.isoformat(),
                "processed_at": p.processed_at.isoformat() if p.processed_at else None,
            }
            for p in payouts
        ]
    }