"""
schemas/admin.py
Pydantic models for admin-only operations.
"""

from pydantic import BaseModel, EmailStr
from typing import Literal

class AdminCreateUserRequest(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    role: Literal['inspector', 'admin', 'fleet_owner', 'reseller', 'mechanic', 'private_owner']
    phone_number: str | None = None