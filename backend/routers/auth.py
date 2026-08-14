"""
routers/auth.py
Complete authentication - register, login, forgot password, profile
"""
import uuid, secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr
from typing import Optional
from backend.core.database import get_db
from backend.core.security import verify_password, create_access_token, hash_password, get_current_user
from backend.models.user import User, SubscriptionPlan
from backend.config import settings

router = APIRouter(prefix="/auth", tags=["Authentication"])

class RegisterRequest(BaseModel):
    full_name: str
    email: EmailStr
    phone: str
    password: str
    role: str = "private_owner"
    company_name: Optional[str] = None

class LoginRequest(BaseModel):
    email: str
    password: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    company_name: Optional[str] = None
    address: Optional[str] = None
    state: Optional[str] = None

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

reset_tokens: dict = {}

@router.post("/register")
async def register(request: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == request.email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")
    existing_phone = await db.execute(select(User).where(User.phone == request.phone))
    if existing_phone.scalar_one_or_none():
        raise HTTPException(400, "Phone already registered")
    valid_roles = ["private_owner", "fleet_owner", "reseller", "mechanic"]
    if request.role not in valid_roles:
        raise HTTPException(400, f"Role must be one of: {valid_roles}")
    if len(request.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user_id = f"USR-{str(uuid.uuid4())[:8].upper()}"
    user = User(
        user_id=user_id, email=request.email, phone=request.phone,
        full_name=request.full_name, password_hash=hash_password(request.password),
        role=request.role, company_name=request.company_name,
        subscription_plan=SubscriptionPlan.FREE, subscription_status="trial",
        subscription_end=datetime.now(timezone.utc) + timedelta(days=7)
    )
    db.add(user)
    await db.flush()
    token = create_access_token(data={"sub": user_id, "role": request.role, "email": request.email})
    return {"success": True, "message": "Account created. 7-day trial started.",
            "access_token": token, "token_type": "bearer",
            "user": {"user_id": user_id, "full_name": request.full_name, "email": request.email, "role": request.role}}

@router.post("/login")
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")

    registration_stage = None
    if user.role == "inspector":
        # Inspectors go through a 3-stage self-registration flow, during
        # which is_active stays False until stage 3 completes. Don't treat
        # that as "suspended" — only block accounts that finished
        # registration and were later suspended by an admin.
        from backend.models.inspector import InspectorProfile, RegistrationStage
        profile_result = await db.execute(select(InspectorProfile).where(InspectorProfile.user_id == user.user_id))
        profile = profile_result.scalar_one_or_none()
        registration_stage = profile.registration_stage if profile else None
        if not user.is_active and registration_stage == RegistrationStage.COMPLETE:
            raise HTTPException(403, "Account suspended. Contact support.")
    else:
        if not user.is_active:
            raise HTTPException(403, "Account suspended. Contact support.")

    token = create_access_token(data={"sub": user.user_id, "role": user.role, "email": user.email})
    return {"success": True, "access_token": token, "token_type": "bearer",
            "registration_stage": registration_stage,
            "user": {"user_id": user.user_id, "full_name": user.full_name, "email": user.email,
                     "role": user.role, "subscription_plan": user.subscription_plan,
                     "subscription_status": user.subscription_status, "vehicle_slots": user.vehicle_slots}}

@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalar_one_or_none()
    if user:
        token = secrets.token_urlsafe(32)
        reset_tokens[token] = {"user_id": user.user_id, "expires": datetime.now(timezone.utc) + timedelta(hours=1)}
        reset_url = f"{settings.APP_URL}/reset-password?token={token}"
        background_tasks.add_task(send_reset_email, user.email, user.full_name, reset_url)
    return {"success": True, "message": "If that email exists, a reset link has been sent."}

@router.post("/reset-password")
async def reset_password(request: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    token_data = reset_tokens.get(request.token)
    if not token_data or datetime.now(timezone.utc) > token_data["expires"]:
        raise HTTPException(400, "Invalid or expired reset token")
    if len(request.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    result = await db.execute(select(User).where(User.user_id == token_data["user_id"]))
    user = result.scalar_one_or_none()
    user.password_hash = hash_password(request.new_password)
    del reset_tokens[request.token]
    await db.commit()
    return {"success": True, "message": "Password reset. Please login."}

@router.get("/me")
async def get_profile(db: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    result = await db.execute(select(User).where(User.user_id == current_user["user_id"]))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    return {"success": True, "user": {
        "user_id": user.user_id, "full_name": user.full_name,
        "email": user.email, "phone": user.phone, "role": user.role,
        "company_name": user.company_name, "subscription_plan": user.subscription_plan,
        "subscription_status": user.subscription_status,
        "subscription_end": user.subscription_end.isoformat() if user.subscription_end else None,
        "vehicle_slots": user.vehicle_slots, "created_at": user.created_at.isoformat(),
        "address": getattr(user, "address", ""), "state": getattr(user, "state", "")
    }}

@router.put("/me")
async def update_profile(request: UpdateProfileRequest, db: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    result = await db.execute(select(User).where(User.user_id == current_user["user_id"]))
    user = result.scalar_one_or_none()
    if request.full_name: user.full_name = request.full_name
    if request.phone: user.phone = request.phone
    if request.company_name: user.company_name = request.company_name
    if request.address and hasattr(user, 'address'): user.address = request.address
    if request.state and hasattr(user, 'state'): user.state = request.state
    await db.commit()
    return {"success": True, "message": "Profile updated"}

@router.post("/change-password")
async def change_password(request: ChangePasswordRequest, db: AsyncSession = Depends(get_db), current_user: dict = Depends(get_current_user)):
    result = await db.execute(select(User).where(User.user_id == current_user["user_id"]))
    user = result.scalar_one_or_none()
    if not verify_password(request.current_password, user.password_hash):
        raise HTTPException(401, "Current password incorrect")
    if len(request.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user.password_hash = hash_password(request.new_password)
    await db.commit()
    return {"success": True, "message": "Password changed"}

async def send_reset_email(email: str, name: str, reset_url: str):
    try:
        if not settings.SENDGRID_API_KEY:
            print(f"[DEV] Reset URL for {email}: {reset_url}")
            return
        import sendgrid
        from sendgrid.helpers.mail import Mail
        sg = sendgrid.SendGridAPIClient(api_key=settings.SENDGRID_API_KEY)
        message = Mail(from_email=settings.FROM_EMAIL, to_emails=email,
            subject="Reset Your Automat Hub Password",
            html_content=f'<p>Hi {name}, <a href="{reset_url}">click here</a> to reset your password. Expires in 1 hour.</p>')
        sg.send(message)
    except Exception as e:
        print(f"Email error: {e}")