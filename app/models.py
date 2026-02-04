# app/models.py

from typing import Optional, List
from datetime import datetime, date as Date, time

from sqlalchemy import UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import SQLModel, Field, Column

class Appointment(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("barber_email", "starts_at", name="uq_barber_start"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    starts_at: datetime
    client_email: str
    barber_email: str
    service: str
    status: str = "booked"

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    role: str # barber or client

class BarberSchedule(SQLModel, table=True):
    barber_email: str = Field(primary_key=True)
    working_days: List[int] = Field(sa_column=Column(JSON))
    day_start: time
    day_end: time

class BarberBlock(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    barber_email: str = Field(index=True)
    date: Date = Field(index=True)
    start: datetime
    end: datetime
    kind: str  # "lunch_break" or "day_off"