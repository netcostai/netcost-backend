from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal
import uuid

Provider = Literal["openai", "anthropic", "google"]


class VaultEntryCreate(BaseModel):
    """What the API expects when a logged-in company adds or updates a provider key"""
    provider: Provider = Field(..., example="openai")
    raw_provider_key: str = Field(..., example="sk-proj-12345XYZ...")


class VaultEntryResponse(BaseModel):
    id: str
    provider: Provider
    created_at: datetime

    class Config:
        from_attributes = True