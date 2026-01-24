# app/schemas.py

from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime, date, time
from typing import List

class BarberSchedule(BaseModel):
    working_days: list[int]     # 0=Mon, 1=Tues....
    day_start: time
    day_end: time

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UserRole(str, Enum):
    barber = "barber"
    client = "client"

class BlockKind(str, Enum):
    lunch_break = "lunch_break"
    day_off = "day_off"

class UserPublic(BaseModel):
    id: int
    email: str
    role: UserRole

class UserCreate(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=72)
    role: UserRole

class AppointmentCreate(BaseModel):
    starts_at: datetime
    client_email: str
    service: str

class ClientAppointmentCreate(BaseModel):
    starts_at: datetime
    service: str

class AppointmentPublic(BaseModel):
    id: int
    starts_at: datetime
    client_email: str
    barber_email: str
    service: str
    status: str

class BlockCreate(BaseModel):
    date: date
    start_time: time
    kind: BlockKind

class AvailabilityResponse(BaseModel):
    barber_email: str
    date: date
    available_starts: List[str]
