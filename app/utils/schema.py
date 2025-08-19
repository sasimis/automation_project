
from pydantic import BaseModel, Field
from typing import Optional

class LeadRecord(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    service: Optional[str] = None
    source: Optional[str] = Field(default=None, description="forms|email|other")

class InvoiceRecord(BaseModel):
    number: Optional[str] = None
    date: Optional[str] = None
    client: Optional[str] = None
    amount: Optional[float] = 0.0
    vat: Optional[float] = 24.0
