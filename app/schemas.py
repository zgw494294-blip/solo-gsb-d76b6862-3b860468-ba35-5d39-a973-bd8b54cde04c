"""Pydantic request models."""
from __future__ import annotations

from pydantic import BaseModel, Field


class CreatePoolRequest(BaseModel):
    total: int = Field(..., gt=0, description="Total quota units granted to the pool")


class CreateReservationRequest(BaseModel):
    amount: int = Field(..., gt=0, description="Quota units to reserve")
    ttl_seconds: int | None = Field(
        None, gt=0, description="Time-to-live; defaults to QUOTA_DEFAULT_TTL_SECONDS"
    )


class SettleReservationRequest(BaseModel):
    used_amount: int = Field(
        ..., ge=0, description="Actual usage; must not exceed the reserved amount"
    )
