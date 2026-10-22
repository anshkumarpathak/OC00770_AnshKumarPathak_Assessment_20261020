"""The data contracts the agent expects from the two enterprise APIs.

Every API response is validated against these models before the agent sees it.
If an upstream system sends something that breaks the contract (missing field,
wrong type, totals that don't add up) we fail loudly instead of letting the
agent reason over bad data.
"""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---- Service desk (ITSM) -------------------------------------------------------

class KeyUser(_Base):
    user_id: str = Field(pattern=r"^U\d{3}$")
    name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[a-z]+$")
    client_id: str = Field(pattern=r"^CL\d{2}$")
    client_name: str
    sla_plan: Literal["standard", "premium"]
    job_role: str
    sap_user: str = Field(pattern=r"^[A-Z][A-Z0-9]{2,11}$")


class Ticket(_Base):
    ticket_id: str = Field(pattern=r"^INC-\d{4}$")
    user_id: str = Field(pattern=r"^U\d{3}$")
    subject: str
    status: Literal["open", "in_progress", "resolved", "closed"]
    priority: Literal["P1", "P2", "P3", "P4"]
    created_at: date


# ---- SAP S/4HANA ---------------------------------------------------------------

class POItem(_Base):
    material: str
    qty: float = Field(gt=0)
    unit_price: float = Field(ge=0)


class PurchaseOrder(_Base):
    po_id: str = Field(pattern=r"^PO-\d{4}$")
    client_id: str = Field(pattern=r"^CL\d{2}$")
    requested_by: str
    vendor: str
    status: Literal["awaiting_approval", "approved", "goods_received", "closed", "rejected"]
    approver: str
    created_at: date
    approved_at: date | None = None
    items: list[POItem] = Field(min_length=1)
    total: float = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self):
        expected = round(sum(i.qty * i.unit_price for i in self.items), 2)
        if abs(expected - self.total) > 0.01:
            raise ValueError(f"total {self.total} does not match items sum {expected}")
        if self.status in ("approved", "goods_received", "closed") and self.approved_at is None:
            raise ValueError("approved purchase order has no approved_at date")
        return self


class Invoice(_Base):
    invoice_id: str = Field(pattern=r"^INV-\d{4}$")
    client_id: str = Field(pattern=r"^CL\d{2}$")
    po_id: str = Field(pattern=r"^PO-\d{4}$")
    vendor: str
    amount: float = Field(gt=0)
    status: Literal["posted", "blocked", "paid"]
    block_reason: Literal["price_variance", "quantity_variance", "missing_goods_receipt"] | None = None
    posted_at: date
    due_date: date
    paid_at: date | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.status == "blocked" and not self.block_reason:
            raise ValueError("blocked invoice has no block_reason")
        if self.status == "paid" and self.paid_at is None:
            raise ValueError("paid invoice has no paid_at date")
        return self


class SapUser(_Base):
    sap_user: str = Field(pattern=r"^[A-Z][A-Z0-9]{2,11}$")
    client_id: str = Field(pattern=r"^CL\d{2}$")
    lock_status: Literal["unlocked", "locked_failed_logins", "locked_by_admin"]
    last_login: date | None = None
    roles: list[str]


class ChangeDoc(_Base):
    change_id: str
    sap_user: str
    change: str
