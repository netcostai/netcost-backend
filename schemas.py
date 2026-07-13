from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal
import uuid

Provider = Literal["openai", "anthropic", "google"]


class VaultVaultCreate(BaseModel):
    """What the API expects when a new client onboards/submits a key"""
    company_name: str = Field(..., example="Acme Corp")
    provider: Provider = Field(..., example="openai")
    raw_provider_key: str = Field(..., example="sk-proj-12345XYZ...")


class VaultVaultResponse(BaseModel):
    """What the database or secure admin panel returns"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    company_name: str
    provider: Provider
    encrypted_provider_key: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        from_attributes = True


class VaultCreateResponse(BaseModel):
    """Returned once, right after onboarding — the customer must save this key.
    We never store or return it again after this point."""
    id: str
    company_name: str
    provider: Provider
    api_key: str
    created_at: datetime

    class Config:
        from_attributes = True