"""
services/kyc_service.py
BVN/NIN verification, bank account resolution, and liveness photo storage.

TEST PHASE NOTICE:
Real Nigerian government verification (NIMC for NIN, NIBSS for BVN and
bank account name-enquiry) is not yet wired in. verify_bvn_nin() and
resolve_bank_account() currently only validate format and return mock
data. Swap their internals for real API calls when the integration is
ready — the function signatures already match what a real NIBSS/NIMC
response would need to provide, so no router changes should be needed.

BVN and NIN are independent Nigerian identifiers issued by different
bodies (banks vs. NIMC) and are NOT expected to numerically match each
other — that's expected and correct, not a bug.
"""
import re
import base64
import io
import boto3
from backend.config import settings


def validate_bvn_format(bvn: str) -> bool:
    return bool(re.fullmatch(r"\d{11}", bvn))


def validate_nin_format(nin: str) -> bool:
    return bool(re.fullmatch(r"\d{11}", nin))


async def verify_bvn_nin(bvn: str, nin: str, full_name: str) -> dict:
    """
    TEST PHASE MOCK — does not call any external API.
    Only validates format (11 digits each). Replace with real NIMC/NIBSS
    calls when ready; keep this same return shape: {"verified": bool, "error": str|None}.
    """
    if not validate_bvn_format(bvn):
        return {"verified": False, "error": "BVN must be exactly 11 digits"}
    if not validate_nin_format(nin):
        return {"verified": False, "error": "NIN must be exactly 11 digits"}
    return {
        "verified": True,
        "mock": True,
        "note": "Test-phase mock — real NIMC/NIBSS verification not yet connected"
    }


async def resolve_bank_account(account_number: str, bank_name: str, full_name: str) -> dict:
    """
    TEST PHASE MOCK — in production this calls a NIBSS name-enquiry API
    to resolve the real account holder's name from account_number + bank code.
    For now it echoes back the inspector's registered full_name.
    """
    if not re.fullmatch(r"\d{10}", account_number):
        return {"resolved": False, "error": "Account number must be exactly 10 digits"}
    return {
        "resolved": True,
        "mock": True,
        "account_name": full_name.upper(),
        "note": "Test-phase mock — real NIBSS name-enquiry not yet connected"
    }


async def upload_liveness_photo(image_base64: str, user_id: str) -> str:
    """
    Decodes a base64 liveness capture and uploads it to S3, using the
    same client pattern as qr_service.py. Stored PRIVATE (not public-read
    like QR codes), since this is biometric data.
    """
    try:
        # Strip a data URL prefix if the frontend sent one, e.g. "data:image/jpeg;base64,..."
        if "," in image_base64 and image_base64.strip().startswith("data:"):
            image_base64 = image_base64.split(",", 1)[1]
        image_bytes = base64.b64decode(image_base64)
    except Exception:
        raise ValueError("Invalid base64 image data")

    if not image_bytes:
        raise ValueError("Empty image data")

    if not settings.AWS_ACCESS_KEY_ID:
        raise ValueError("S3 is not configured — cannot store liveness photo")

    s3_client = boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION
    )

    s3_key = f"inspector-liveness/{user_id}.jpg"
    buffer = io.BytesIO(image_bytes)

    s3_client.upload_fileobj(
        buffer,
        settings.AWS_BUCKET_NAME,
        s3_key,
        ExtraArgs={'ContentType': 'image/jpeg', 'ACL': 'private'}
    )

    return f"https://{settings.AWS_BUCKET_NAME}.s3.{settings.AWS_REGION}.amazonaws.com/{s3_key}"
