"""Pydantic validators for Supabase insert payloads (not LLM extraction schemas)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ServiceCategory = Literal["Neurotoxin", "Filler", "others"]
UnitType = Literal[
    "unit",
    "syringe",
    "half_syringe",
    "vial",
    "treatment",
    "session",
    "package",
    "area",
    "ml",
    "mg",
    "others",
]


class ClinicServiceInsertRow(BaseModel):
    """Row shape for clinic_services INSERT via Supabase REST."""

    model_config = ConfigDict(extra="forbid")

    business_id: int = Field(gt=0)
    service_name: str = Field(min_length=1)
    regular_price: float = Field(gt=0)
    unit_type: UnitType
    service_category: ServiceCategory = "others"
    source_url: str = Field(min_length=1)
    service_name_raw: Optional[str] = None
    service_area: Optional[str] = None
    updated_at: Optional[datetime] = None

    def to_api_dict(self) -> dict:
        return self.model_dump(exclude_none=True, mode="json")
