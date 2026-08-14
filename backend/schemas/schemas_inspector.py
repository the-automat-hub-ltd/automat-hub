"""
schemas/inspector.py
Pydantic models for inspector login, the 3-stage registration flow,
profile management, and payout requests.
"""
from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, Literal


class InspectorLoginRequest(BaseModel):
    email: EmailStr
    password: str


class InspectorRegisterStage1(BaseModel):
    full_name: str
    email: EmailStr
    phone: str
    password: str
    sex: Literal['male', 'female', 'other']

    @field_validator('password')
    @classmethod
    def password_length(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters')
        return v


class InspectorRegisterStage2(BaseModel):
    bvn: str
    nin: str
    bank_account_number: str
    bank_name: str

    @field_validator('bvn', 'nin')
    @classmethod
    def eleven_digits(cls, v):
        if not (v.isdigit() and len(v) == 11):
            raise ValueError('Must be exactly 11 digits')
        return v

    @field_validator('bank_account_number')
    @classmethod
    def ten_digits(cls, v):
        if not (v.isdigit() and len(v) == 10):
            raise ValueError('Account number must be exactly 10 digits (NUBAN format)')
        return v


class InspectorRegisterStage3(BaseModel):
    # Base64-encoded JPEG/PNG of the liveness capture
    liveness_image_base64: str


class InspectorProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    sex: Optional[Literal['male', 'female', 'other']] = None
    bank_account_number: Optional[str] = None
    bank_name: Optional[str] = None

    @field_validator('bank_account_number')
    @classmethod
    def ten_digits(cls, v):
        if v is not None and not (v.isdigit() and len(v) == 10):
            raise ValueError('Account number must be exactly 10 digits (NUBAN format)')
        return v