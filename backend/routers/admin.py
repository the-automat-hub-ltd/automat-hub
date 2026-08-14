"""
routers/admin.py
Admin dashboard endpoints — internal Automat Hub use only.

Admin can:
- View all DCPs, escrows, users
- Approve/suspend workshops
- Manage reseller API keys
- View platform revenue and metrics
- Manage subscriptions
- Override escrow disputes
- View all vehicle alerts
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from pydantic import BaseModel
import io
import csv
import uuid

from backend.core.database import get_db
from backend.core.security import get_current_user, hash_password
from backend.schemas.admin import AdminCreateUserRequest
from backend.models.dcp import DCPRecord, VerificationLog, DCPHashLedger, InspectionDetail
from backend.models.escrow import EscrowDeal, EscrowEvent
from backend.models.fleet import TrackedVehicle, HourlyScan, VehicleAlert, LocationHistory, Fleet
from backend.models.workshop import Workshop, RepairJob
from backend.models.user import User, ResellerAPIKey, Subscription

router = APIRouter(prefix="/admin", tags=["Admin Dashboard"])


async def require_admin(current_user: dict = Depends(get_current_user)):
    """Dependency: reject non-admin users."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ── PLATFORM OVERVIEW ────────────────────────────────────────

@router.get("/overview", response_model=dict)
async def platform_overview(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """
    Complete platform metrics dashboard.
    Revenue, users, DCPs, escrows, workshops.
    """
    now = datetime.now(timezone.utc)
    this_month = now.replace(day=1, hour=0, minute=0, second=0)
    last_30_days = now - timedelta(days=30)

    # DCP stats
    total_dcps = await db.scalar(select(func.count(DCPRecord.id)))
    dcps_this_month = await db.scalar(
        select(func.count(DCPRecord.id))
        .where(DCPRecord.issued_at >= this_month)
    )

    # Escrow stats
    total_escrow_volume = await db.scalar(
        select(func.sum(EscrowDeal.amount_usd))
        .where(EscrowDeal.status == "COMPLETED")
    ) or 0

    total_platform_fees = await db.scalar(
        select(func.sum(EscrowDeal.platform_fee_amount))
        .where(EscrowDeal.status == "COMPLETED")
    ) or 0

    active_escrows = await db.scalar(
        select(func.count(EscrowDeal.id))
        .where(EscrowDeal.status.in_(["INITIATED", "FUNDED", "DCP_MATCHED"]))
    )

    # User stats
    total_users = await db.scalar(select(func.count(User.id)))
    active_subscriptions = await db.scalar(
        select(func.count(User.id))
        .where(User.subscription_status == "active")
    )

    # Vehicle stats
    total_tracked = await db.scalar(select(func.count(TrackedVehicle.id)))
    critical_vehicles = await db.scalar(
        select(func.count(TrackedVehicle.id))
        .where(TrackedVehicle.status == "critical")
    )

    # Workshop stats
    active_workshops = await db.scalar(
        select(func.count(Workshop.id))
        .where(Workshop.status == "active")
    )
    pending_workshops = await db.scalar(
        select(func.count(Workshop.id))
        .where(Workshop.status == "pending_approval")
    )

    # Scans today
    today_start = now.replace(hour=0, minute=0, second=0)
    scans_today = await db.scalar(
        select(func.count(HourlyScan.id))
        .where(HourlyScan.scanned_at >= today_start)
    )
    
    # Chart Distributions
    dcp_grades = await db.execute(select(DCPRecord.grade, func.count(DCPRecord.id)).group_by(DCPRecord.grade))
    grade_dist = {g: c for g, c in dcp_grades.all()}
    
    escrow_status = await db.execute(select(EscrowDeal.status, func.count(EscrowDeal.id)).group_by(EscrowDeal.status))
    escrow_dist = {s: c for s, c in escrow_status.all()}
    
    veh_status = await db.execute(select(TrackedVehicle.status, func.count(TrackedVehicle.id)).group_by(TrackedVehicle.status))
    veh_dist = {s: c for s, c in veh_status.all()}
    
    # Inspector Revenue (DCPs issued * 5000 NGN)
    inspector_rev_query = await db.execute(
        select(User.full_name, func.count(DCPRecord.id))
        .join(DCPRecord, User.user_id == DCPRecord.auditor_id)
        .group_by(User.full_name)
    )
    inspector_rev_data = {name: count * 5000 for name, count in inspector_rev_query.all() if name}
    
    # 6-Month Trend Data
    trend_labels = []
    vol_data = []
    fees_data = []
    users_data = []
    
    for i in range(5, -1, -1):
        dt = now - timedelta(days=30*i)
        start_dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt.replace(year=start_dt.year+1, month=1) if start_dt.month == 12 else start_dt.replace(month=start_dt.month+1)
            
        trend_labels.append(start_dt.strftime("%b"))
        
        u_count = await db.scalar(select(func.count(User.id)).where(and_(User.created_at >= start_dt, User.created_at < end_dt)))
        users_data.append(u_count or 0)
        
        vol = await db.scalar(select(func.sum(EscrowDeal.amount_usd)).where(and_(EscrowDeal.completed_at >= start_dt, EscrowDeal.completed_at < end_dt, EscrowDeal.status == "COMPLETED")))
        vol_data.append(float(vol or 0))
        
        fees = await db.scalar(select(func.sum(EscrowDeal.platform_fee_amount)).where(and_(EscrowDeal.completed_at >= start_dt, EscrowDeal.completed_at < end_dt, EscrowDeal.status == "COMPLETED")))
        fees_data.append(float(fees or 0))

    return {
        "success": True,
        "generated_at": now.isoformat(),
        "dcps": {
            "total": total_dcps,
            "this_month": dcps_this_month,
        },
        "escrow": {
            "total_volume_usd": float(total_escrow_volume),
            "total_fees_usd": float(total_platform_fees),
            "active_escrows": active_escrows,
        },
        "users": {
            "total": total_users,
            "active_subscriptions": active_subscriptions,
        },
        "fleet": {
            "total_tracked_vehicles": total_tracked,
            "critical_vehicles": critical_vehicles,
            "scans_today": scans_today,
        },
        "workshops": {
            "active": active_workshops,
            "pending_approval": pending_workshops,
        },
        "charts": {
            "trend_labels": trend_labels,
            "escrow_volume": vol_data,
            "escrow_fees": fees_data,
            "new_users": users_data,
            "dcp_grades": grade_dist,
            "escrow_status": escrow_dist,
            "vehicle_status": veh_dist,
            "inspector_revenue": inspector_rev_data
        }
    }


# ── USER MANAGEMENT ──────────────────────────────────────────

@router.get("/users", response_model=dict)
async def list_users(
    role: Optional[str] = None,
    subscription: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """List all users with filters."""
    query = select(User).order_by(desc(User.created_at))

    if role:
        query = query.where(User.role == role)
    if subscription:
        query = query.where(User.subscription_plan == subscription)

    query = query.offset((page - 1) * limit).limit(limit)
    result = await db.execute(query)
    users = result.scalars().all()

    return {
        "success": True,
        "page": page,
        "users": [
            {
                "user_id": u.user_id,
                "full_name": u.full_name,
                "email": u.email,
                "phone": u.phone,
                "role": u.role,
                "subscription_plan": u.subscription_plan,
                "subscription_status": u.subscription_status,
                "vehicle_slots": u.vehicle_slots,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat()
            }
            for u in users
        ]
    }


@router.post("/users/{user_id}/suspend", response_model=dict)
async def suspend_user(
    user_id: str,
    reason: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Suspend a user account."""
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="You cannot suspend your own admin account.")

    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = False
    return {"success": True, "message": f"User {user_id} suspended. Reason: {reason}"}

class GrantSubRequest(BaseModel):
    days: int

@router.post("/users/{user_id}/grant-sub", response_model=dict)
async def grant_subscription(
    user_id: str,
    request: GrantSubRequest,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Grant free subscription access to a user for a specific number of days."""
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.now(timezone.utc)
    
    # If currently active, extend from existing end date, otherwise from now
    if user.subscription_end and user.subscription_status == "active" and user.subscription_end > now:
        user.subscription_end = user.subscription_end + timedelta(days=request.days)
    else:
        user.subscription_start = now
        user.subscription_end = now + timedelta(days=request.days)
        
    user.subscription_status = "active"

    # Add a subscription record
    sub = Subscription(
        user_id=user_id,
        plan=user.subscription_plan or "free",
        vehicle_count=user.vehicle_slots or 1,
        amount_ngn=0,
        billing_period_start=user.subscription_start,
        billing_period_end=user.subscription_end,
        status="active",
        paystack_reference=f"ADMIN-GRANT-{str(uuid.uuid4())[:8].upper()}"
    )
    db.add(sub)
    await db.flush()

    return {"success": True, "message": f"Granted {request.days} days subscription to user."}

@router.delete("/users/{user_id}", response_model=dict)
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Hard delete a user and cascade delete their fleets, vehicles, etc."""
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own admin account.")

    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    vehicles_result = await db.execute(select(TrackedVehicle).where(TrackedVehicle.owner_id == user_id))
    vehicles = vehicles_result.scalars().all()
    v_ids = [v.vehicle_id for v in vehicles]
    vins = [v.vin for v in vehicles]
    
    if v_ids:
        await db.execute(LocationHistory.__table__.delete().where(LocationHistory.vehicle_id.in_(v_ids)))
        await db.execute(HourlyScan.__table__.delete().where(HourlyScan.vehicle_id.in_(v_ids)))
        await db.execute(VehicleAlert.__table__.delete().where(VehicleAlert.vehicle_id.in_(v_ids)))
        await db.execute(RepairJob.__table__.delete().where(RepairJob.vehicle_id.in_(v_ids)))
    
    await db.execute(TrackedVehicle.__table__.delete().where(TrackedVehicle.owner_id == user_id))
    await db.execute(Fleet.__table__.delete().where(Fleet.owner_id == user_id))
    await db.execute(ResellerAPIKey.__table__.delete().where(ResellerAPIKey.user_id == user_id))
    await db.execute(Subscription.__table__.delete().where(Subscription.user_id == user_id))
    
    await db.delete(user)
    await db.flush()

    return {"success": True, "message": "User and all associated data permanently deleted."}

@router.get("/inspectors", response_model=dict)
async def list_inspectors(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """List all certified inspectors and their performance metrics."""
    query = select(User).where(User.role == "inspector")
    result = await db.execute(query)
    users = result.scalars().all()
    
    # Count DCPs issued per inspector
    dcp_result = await db.execute(
        select(DCPRecord.auditor_id, func.count(DCPRecord.id)).group_by(DCPRecord.auditor_id)
    )
    dcp_counts = dict(dcp_result.all())

    inspectors = []
    for u in users:
        inspectors.append({
            "user_id": u.user_id,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "dcp_count": dcp_counts.get(u.user_id, 0),
            "rating": 4.9 # Default high rating for MVP
        })
        
    return {"success": True, "inspectors": inspectors}


@router.post("/users/create", response_model=dict, summary="Admin create a new user")
async def admin_create_user(
    request: AdminCreateUserRequest,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """
    Allows an admin to create any type of user, including inspectors and other admins.
    This is a privileged operation and bypasses public registration validation.
    """
    # Check if user with this email already exists
    existing_user_res = await db.execute(
        select(User).where(User.email == request.email)
    )
    if existing_user_res.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"A user with the email '{request.email}' already exists."
        )

    user_id = f"USR-{str(uuid.uuid4())[:8].upper()}"

    new_user = User(
        user_id=user_id,
        full_name=request.full_name,
        email=request.email,
        password_hash=hash_password(request.password),
        role=request.role,
        phone_number=request.phone_number,
        is_active=True,
        email_verified=True, # Admins create verified users by default
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        subscription_plan='free',
        subscription_status='active',
        vehicle_slots=1 if request.role == 'private_owner' else 5 # Default slots
    )

    db.add(new_user)
    await db.commit()

    return {"success": True, "message": f"User '{request.full_name}' created successfully with role '{request.role}'.", "user_id": user_id}


# ── DCP MANAGEMENT ───────────────────────────────────────────

@router.get("/dcps", response_model=dict)
async def list_dcps(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """List all issued DCPs."""
    result = await db.execute(
        select(DCPRecord)
        .order_by(desc(DCPRecord.issued_at))
        .offset((page - 1) * limit)
        .limit(limit)
    )
    dcps = result.scalars().all()

    total = await db.scalar(select(func.count(DCPRecord.id)))

    return {
        "success": True,
        "total": total,
        "page": page,
        "dcps": [
            {
                "dcp_id": d.dcp_id,
                "vin": d.vin,
                "make": d.make,
                "model": d.model,
                "year": d.year,
                "score": d.score,
                "grade": d.grade,
                "status": d.status,
                "auditor_id": d.auditor_id,
                "issued_at": d.issued_at.isoformat()
            }
            for d in dcps
        ]
    }
    

@router.delete("/dcps/{dcp_id}", response_model=dict)
async def delete_dcp(
    dcp_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Hard delete a single DCP and its associated escrow deals globally."""
    result = await db.execute(select(DCPRecord).where(DCPRecord.dcp_id == dcp_id))
    dcp = result.scalar_one_or_none()
    if not dcp:
        raise HTTPException(status_code=404, detail="DCP not found")
        
    # Delete orphaned escrow links
    await db.execute(EscrowEvent.__table__.delete().where(
        EscrowEvent.escrow_id.in_(select(EscrowDeal.escrow_id).where(EscrowDeal.dcp_id == dcp_id))
    ))
    await db.execute(EscrowDeal.__table__.delete().where(EscrowDeal.dcp_id == dcp_id))
    
    await db.execute(InspectionDetail.__table__.delete().where(InspectionDetail.dcp_id == dcp_id))
    await db.execute(VerificationLog.__table__.delete().where(VerificationLog.dcp_id == dcp_id))
    await db.execute(DCPHashLedger.__table__.delete().where(DCPHashLedger.dcp_id == dcp_id))
    await db.execute(DCPRecord.__table__.delete().where(DCPRecord.dcp_id == dcp_id))
    
    await db.flush()
    return {"success": True, "message": "DCP permanently deleted."}


# ── ESCROW MANAGEMENT ────────────────────────────────────────

@router.get("/escrows", response_model=dict)
async def list_escrows(
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """List all escrow deals."""
    query = select(EscrowDeal).order_by(desc(EscrowDeal.initiated_at))
    if status:
        query = query.where(EscrowDeal.status == status)

    result = await db.execute(query.offset((page - 1) * 50).limit(50))
    deals = result.scalars().all()

    return {
        "success": True,
        "page": page,
        "escrows": [
            {
                "escrow_id": d.escrow_id,
                "dcp_id": d.dcp_id,
                "vin": d.vin,
                "buyer_name": d.buyer_name,
                "seller_name": d.seller_name,
                "amount_usd": float(d.amount_usd),
                "status": d.status,
                "dcp_verified": d.dcp_verified,
                "initiated_at": d.initiated_at.isoformat()
            }
            for d in deals
        ]
    }


@router.post("/escrows/{escrow_id}/resolve-dispute", response_model=dict)
async def resolve_dispute(
    escrow_id: str,
    resolution: str,
    release_to: str = Query(..., regex="^(buyer|seller)$"),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Admin resolves an escrow dispute. Releases funds to buyer or seller."""
    result = await db.execute(
        select(EscrowDeal).where(EscrowDeal.escrow_id == escrow_id)
    )
    deal = result.scalar_one_or_none()

    if not deal:
        raise HTTPException(status_code=404, detail="Escrow not found")

    if deal.status != "DISPUTED":
        raise HTTPException(status_code=400, detail="Escrow is not in disputed state")

    deal.status = "COMPLETED" if release_to == "seller" else "REFUNDED"

    return {
        "success": True,
        "escrow_id": escrow_id,
        "resolution": resolution,
        "funds_released_to": release_to,
        "resolved_by": admin["user_id"]
    }

@router.delete("/escrows/{escrow_id}", response_model=dict)
async def delete_escrow(
    escrow_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Hard delete a stuck escrow deal globally."""
    result = await db.execute(select(EscrowDeal).where(EscrowDeal.escrow_id == escrow_id))
    deal = result.scalar_one_or_none()
    
    if not deal:
        raise HTTPException(status_code=404, detail="Escrow not found")
        
    # Delete events first to prevent foreign key errors
    await db.execute(EscrowEvent.__table__.delete().where(EscrowEvent.escrow_id == escrow_id))
    await db.execute(EscrowDeal.__table__.delete().where(EscrowDeal.escrow_id == escrow_id))
    
    await db.flush()
    return {"success": True, "message": "Escrow permanently deleted."}

# ── WORKSHOP MANAGEMENT ──────────────────────────────────────

@router.get("/workshops/pending", response_model=dict)
async def pending_workshops(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """List workshops awaiting approval."""
    result = await db.execute(
        select(Workshop)
        .where(Workshop.status == "pending_approval")
        .order_by(Workshop.registered_at)
    )
    workshops = result.scalars().all()

    return {
        "success": True,
        "pending_count": len(workshops),
        "workshops": [
            {
                "workshop_id": w.workshop_id,
                "name": w.name,
                "address": w.address,
                "state": w.state,
                "specializations": w.specializations,
                "registered_at": w.registered_at.isoformat()
            }
            for w in workshops
        ]
    }


@router.post("/workshops/{workshop_id}/approve", response_model=dict)
async def approve_workshop(
    workshop_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Approve a workshop application."""
    result = await db.execute(
        select(Workshop).where(Workshop.workshop_id == workshop_id)
    )
    workshop = result.scalar_one_or_none()

    if not workshop:
        raise HTTPException(status_code=404, detail="Workshop not found")

    workshop.status = "active"

    return {
        "success": True,
        "workshop_id": workshop_id,
        "message": f"{workshop.name} approved and now active in the network"
    }


@router.post("/workshops/{workshop_id}/suspend", response_model=dict)
async def suspend_workshop(
    workshop_id: str,
    reason: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Suspend a workshop."""
    result = await db.execute(
        select(Workshop).where(Workshop.workshop_id == workshop_id)
    )
    workshop = result.scalar_one_or_none()

    if not workshop:
        raise HTTPException(status_code=404, detail="Workshop not found")

    workshop.status = "suspended"
    workshop.is_available = False

    return {"success": True, "workshop_id": workshop_id, "reason": reason}


# ── REVENUE REPORTING ────────────────────────────────────────

@router.get("/revenue", response_model=dict)
async def revenue_report(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Platform revenue breakdown."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Escrow fees
    escrow_fees = await db.scalar(
        select(func.sum(EscrowDeal.platform_fee_amount))
        .where(
            and_(
                EscrowDeal.status == "COMPLETED",
                EscrowDeal.completed_at >= since
            )
        )
    ) or 0

    # Subscription revenue (approximated from active subs)
    active_private = await db.scalar(
        select(func.count(User.id))
        .where(
            and_(
                User.subscription_plan == "private",
                User.subscription_status == "active"
            )
        )
    ) or 0

    active_fleet_vehicles = await db.scalar(
        select(func.sum(User.vehicle_slots))
        .where(
            and_(
                User.subscription_plan == "fleet",
                User.subscription_status == "active"
            )
        )
    ) or 0

    active_resellers = await db.scalar(
        select(func.count(User.id))
        .where(
            and_(
                User.subscription_plan == "reseller",
                User.subscription_status == "active"
            )
        )
    ) or 0

    # Repair job commissions
    repair_fees = await db.scalar(
        select(func.sum(RepairJob.platform_fee_ngn))
        .where(
            and_(
                RepairJob.status == "completed",
                RepairJob.completed_at >= since
            )
        )
    ) or 0

    monthly_sub_revenue = (
        active_private * 15000 +
        active_fleet_vehicles * 8000 +
        active_resellers * 50000
    )

    return {
        "success": True,
        "period_days": days,
        "revenue": {
            "escrow_fees_usd": float(escrow_fees),
            "monthly_subscriptions_ngn": monthly_sub_revenue,
            "repair_commissions_ngn": float(repair_fees),
        },
        "subscriptions": {
            "private_owners": active_private,
            "fleet_vehicles": active_fleet_vehicles,
            "resellers": active_resellers,
        }
    }


# ── VEHICLE MANAGEMENT ───────────────────────────────────────

@router.get("/vehicles/export", response_class=StreamingResponse)
async def export_global_vehicles(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin)
):
    """Export all global vehicles to CSV."""
    result = await db.execute(select(TrackedVehicle).order_by(desc(TrackedVehicle.registered_at)))
    vehicles = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Vehicle ID", "VIN", "Owner ID", "Make", "Model", "Year", "Plate Number", "Status", "Health Score", "OBD Method", "Registered At"])
    
    for v in vehicles:
        writer.writerow([
            v.vehicle_id,
            v.vin,
            v.owner_id,
            v.make,
            v.model,
            v.year,
            v.plate_number,
            v.status,
            v.latest_score,
            v.obd_connection_method,
            v.registered_at.isoformat() if v.registered_at else ""
        ])
    
    output.seek(0)
    filename = f"automat_global_vehicles_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
