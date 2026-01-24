# app/main.py

from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime, timedelta, date

from .schemas import (
    UserCreate, UserPublic, Token,
    BarberSchedule, BlockCreate, BlockKind,
    AppointmentCreate, ClientAppointmentCreate, AppointmentPublic,
    AvailabilityResponse
)
from .app.auth import get_current_user, hash_password, verify_password, create_access_token
from .deps import require_role
from .core import overlaps
from .data import (
    users_db, appointments_db, barber_schedules_db, barber_blocks_db,
    SERVICES, shop_settings
)

app = FastAPI()

@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/appointments", response_model=AppointmentPublic, status_code=201)
def create_appointment(
    appt: AppointmentCreate,
    current_user: dict = Depends(get_current_user),
):
    require_role(current_user, "barber")  # only barbers can create
    barber_email = current_user["email"]

    # 1) Validate service
    if appt.service not in SERVICES:
        raise HTTPException(status_code=422, detail="Service not available")
    # 2) Validate slot alignment (15-min grid)
    slot_minutes = shop_settings["slot_minutes"]
    if appt.starts_at.minute % slot_minutes != 0:
        raise HTTPException(status_code=422, detail="Start time must be in 15-minute increments")
    
    # 3) Build appointment interval
    appt_start = appt.starts_at
    appt_end = appt_start + timedelta(minutes=SERVICES[appt.service])
    appt_date = appt_start.date()

    # 4) Find barber schedule and validate working hours/day
    schedule = None
    for s in barber_schedules_db:
        if s["barber_email"] == barber_email:
            schedule = s
            break
    if schedule is None:
        raise HTTPException(status_code=409, detail="Weekly schedule must be set before creating appointments")

    if appt_date.weekday() not in schedule["working_days"]:
        raise HTTPException(status_code=422, detail="Barber is not scheduled to work that day")
    
    work_start = datetime.combine(appt_date, schedule["day_start"])
    work_end = datetime.combine(appt_date, schedule["day_end"])

    if appt_start < work_start or appt_end > work_end:
        raise HTTPException(status_code=422, detail="Appointment must be within working hours")
    
     # 5) Reject overlaps with blocks (lunch/day off)
    for b in barber_blocks_db:
        if b["barber_email"] != barber_email:
            continue
        if b["date"] != appt_date:
            continue
        if overlaps(appt_start, appt_end, b["start"], b["end"]):
            raise HTTPException(status_code=409, detail="Appointment overlaps a block")
        
      # 6) Reject overlaps with existing appointments (double-booking)
    for a in appointments_db:
        if a["barber_email"] != barber_email:
            continue
        if a["starts_at"].date() != appt_date:
            continue
        service_key = a.get("service")
        if service_key not in SERVICES:
            # Ignore legacy/bad records (or you can choose to raise)
            continue
        existing_start = a["starts_at"]
        existing_end = existing_start + timedelta(minutes=SERVICES[service_key])

        if overlaps(appt_start, appt_end, existing_start, existing_end):
            raise HTTPException(status_code=409, detail="Appointment overlaps an existing appointment")


    new_appt = {
        "id": len(appointments_db) + 1,
        "starts_at": appt.starts_at,
        "client_email": appt.client_email,
        "barber_email": current_user["email"],
        "service": appt.service,
        "status": "booked",   # ← ADD THIS
    }

    appointments_db.append(new_appt)
    return new_appt


@app.get("/me", response_model=UserPublic)
def me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "email": current_user["email"],
        "role": current_user["role"],
    }


# creating a user, using hashing
@app.post("/users", status_code=201, response_model=UserPublic)
def create_user(user: UserCreate):
    for existing_user in users_db:
        if existing_user["email"] == user.email:
            raise HTTPException(
                status_code=409,
                detail="Email already registered"
            )
    password_hash = hash_password(user.password)
    new_user = {
        "id": len(users_db) + 1,
        "email": user.email,
        "password_hash": password_hash,
        "role": user.role.value
    }
    users_db.append(new_user)
    public_user = {
    "id": new_user["id"],
    "email": new_user["email"],
    "role": new_user["role"],
    }
    return public_user

@app.post("/auth/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    # Swagger OAuth2 "password" flow uses "username" field
    email = form_data.username
    password = form_data.password

    for existing_user in users_db:
        if existing_user["email"] == email:
            if verify_password(password, existing_user["password_hash"]):
                token = create_access_token({"sub": existing_user["email"]})
                return {"access_token": token, "token_type": "bearer"}
            raise HTTPException(status_code=401, detail="Invalid credentials")

    raise HTTPException(status_code=401, detail="Invalid credentials")


# Creates or updates their schedule
@app.put('/barbers/me/schedule', response_model=BarberSchedule)
def create_schedule(schedule: BarberSchedule, current_user: dict = Depends(get_current_user)):
    require_role(current_user, "barber")  # only barbers can create
    if not schedule.working_days:
        raise HTTPException(status_code=422, detail="working_days must contain at least one day")
    for day in schedule.working_days:
        if not (0 <= day <= 6):
            raise HTTPException(status_code=422, detail="working_days must be integers between 0 and 6")
    if len(schedule.working_days) != len(set(schedule.working_days)):
        raise HTTPException(status_code=422, detail="working_days cannot contain duplicates")
        
    # if int(schedule.day_start) >=  int(schedule.day_end):
    if schedule.day_start >= schedule.day_end:
        raise HTTPException(status_code=422, detail="day_start cannot be greater than day_end")

    new_schedule = {
        "barber_email": current_user["email"],
        "working_days": schedule.working_days,     
        "day_start": schedule.day_start,              
        "day_end": schedule.day_end,              
    }

    barber_schedules_db.append(new_schedule)
    return new_schedule

@app.put('/barbers/me/blocks')
def barber_block(block:BlockCreate, current_user: dict = Depends(get_current_user)):
    require_role(current_user,"barber") # only barbers can creates
    email = current_user["email"]
    found = False
    for record in barber_schedules_db: #checking for the current_user's schedule
        if email == record["barber_email"]:
            schedule = record
            found = True
            break
    if not found:
        raise HTTPException(status_code=409, detail="Weekly schedule must be set before adding blocks")
    
    weekday = block.date.weekday() # turns date into # 0-6, e.g. 1-14-2026 -> 2, since 0 = Monday, 1 = Tuesday, 2 = Wednesday etc.
    # check to see if day blocked is in working schedule.
    if weekday not in schedule['working_days']:
        raise HTTPException(status_code=422, detail="Not scheduled to work that day")
    #cannot have a break at 9:17, must be increamants of 15
    if block.start_time.minute % 15 != 0:
        raise HTTPException(status_code=422, detail="Time must be in increaments of 15")
    work_start = datetime.combine(block.date, schedule["day_start"])
    work_end = datetime.combine(block.date,schedule["day_end"])
    if block.kind == BlockKind.lunch_break:
        block_start = datetime.combine(block.date, block.start_time)
        block_end = block_start + timedelta(minutes=30)
    else:
        block_start = datetime.combine(block.date, schedule["day_start"])
        block_end = datetime.combine(block.date,schedule["day_end"])
        

    if block_start < work_start or block_end > work_end:
        raise HTTPException(status_code=422, detail="Block must be within working hours")
    for existing_block in barber_blocks_db:
        if existing_block["barber_email"] != email:
            continue
        if existing_block["date"] != block.date:
            continue

        if block_start < existing_block["end"] and existing_block["start"] < block_end:
            raise HTTPException(status_code=409, detail="Block overlaps existing block")
        
    new_block = {
        "id": len(barber_blocks_db) + 1,
        "barber_email": email,
        "date":  block.date,
        "start": block_start,
        "end":  block_end,
        "kind":  block.kind.value
    }
    barber_blocks_db.append(new_block)

    return new_block


@app.get("/barbers/{barber_email}/availability", response_model=AvailabilityResponse)
def barber_availability(barber_email: str, date: date):
    # 1) Lookup barber schedule
    barber_schedule = None
    for barber_record in barber_schedules_db:
        if barber_record["barber_email"] == barber_email:
            barber_schedule = barber_record
            break
    if barber_schedule is None:
        raise HTTPException(status_code=404, detail="Barber Not Found")

    # 2) Check working day
    weekday = date.weekday()
    if weekday not in barber_schedule["working_days"]:
        return {"barber_email": barber_email, "date": date, "available_starts": []}

    # 3) Build working window
    work_start = datetime.combine(date, barber_schedule["day_start"])
    work_end = datetime.combine(date, barber_schedule["day_end"])

    slot_minutes = shop_settings["slot_minutes"]  # 15
    slot_delta = timedelta(minutes=slot_minutes)

    # 4) Collect blocks for this barber + date
    blocks_for_day = []
    for b in barber_blocks_db:
        if b["barber_email"] != barber_email:
            continue
        if b["date"] != date:
            continue
        blocks_for_day.append(b)

    # If any day_off exists, no availability
    for b in blocks_for_day:
        if b.get("kind") == BlockKind.day_off.value:
            return {"barber_email": barber_email, "date": date, "available_starts": []}

    # 5) Collect appointments for this barber + date
    appts_for_day = []
    for a in appointments_db:
        if a["barber_email"] != barber_email:
            continue
        if a["starts_at"].date() != date:
            continue
        appts_for_day.append(a)

    # 6) Generate slots, subtract blocks AND appointments
    available = []
    current = work_start
    while current + slot_delta <= work_end:
        slot_start = current
        slot_end = current + slot_delta

        # Block overlap check
        blocked = False
        for b in blocks_for_day:
            if overlaps(slot_start, slot_end, b["start"], b["end"]):
                blocked = True
                break
        if blocked:
            current += slot_delta
            continue

        # Appointment overlap check (requires "service" stored on appointment)
        booked = False
        for a in appts_for_day:
            if a.get("status") != "booked":
                continue
            service_key = a.get("service")
            if service_key not in SERVICES:
                # If older appointments exist without a service, skip them (or choose a default)
                continue
            appt_start = a["starts_at"]
            appt_end = appt_start + timedelta(minutes=SERVICES[service_key])

            if overlaps(slot_start, slot_end, appt_start, appt_end):
                booked = True
                break
        if booked:
            current += slot_delta
            continue

        available.append(slot_start.time().strftime("%H:%M"))
        current += slot_delta

    return {"barber_email": barber_email, "date": date, "available_starts": available}

@app.post("/barbers/{barber_email}/appointments", response_model=AppointmentPublic, status_code=201)
def client_create_appointment(
    barber_email: str,
    appt: ClientAppointmentCreate,
    current_user: dict = Depends(get_current_user),
):
    require_role(current_user, "client")

    # 1) Validate service
    if appt.service not in SERVICES:
        raise HTTPException(status_code=422, detail="Service not available")

    # 2) Validate slot alignment (15-min grid)
    slot_minutes = shop_settings["slot_minutes"]
    if appt.starts_at.minute % slot_minutes != 0:
        raise HTTPException(status_code=422, detail="Start time must be in 15-minute increments")

    # 3) Build appointment interval
    appt_start = appt.starts_at
    appt_end = appt_start + timedelta(minutes=SERVICES[appt.service])
    appt_date = appt_start.date()

    # 4) Find barber schedule and validate working hours/day
    schedule = None
    for s in barber_schedules_db:
        if s["barber_email"] == barber_email:
            schedule = s
            break
    if schedule is None:
        raise HTTPException(status_code=404, detail="Barber Not Found")

    if appt_date.weekday() not in schedule["working_days"]:
        raise HTTPException(status_code=422, detail="Barber is not scheduled to work that day")

    work_start = datetime.combine(appt_date, schedule["day_start"])
    work_end = datetime.combine(appt_date, schedule["day_end"])
    if appt_start < work_start or appt_end > work_end:
        raise HTTPException(status_code=422, detail="Appointment must be within working hours")

    # 5) Reject overlaps with blocks (lunch/day off)
    for b in barber_blocks_db:
        if b["barber_email"] != barber_email:
            continue
        if b["date"] != appt_date:
            continue
        if overlaps(appt_start, appt_end, b["start"], b["end"]):
            raise HTTPException(status_code=409, detail="Appointment overlaps a block")

    # 6) Reject overlaps with existing appointments (double-booking)
    for a in appointments_db:
        if a["barber_email"] != barber_email:
            continue
        if a["starts_at"].date() != appt_date:
            continue
        service_key = a.get("service")
        if service_key not in SERVICES:
            continue
        existing_start = a["starts_at"]
        existing_end = existing_start + timedelta(minutes=SERVICES[service_key])

        if overlaps(appt_start, appt_end, existing_start, existing_end):
            raise HTTPException(status_code=409, detail="Appointment overlaps an existing appointment")

    # 7) Create and save appointment
    new_appt = {
        "id": len(appointments_db) + 1,
        "starts_at": appt.starts_at,
        "client_email": current_user["email"],  # client is the logged-in user
        "barber_email": barber_email,           # barber is from the path
        "service": appt.service,
        "status": "booked"
    }
    appointments_db.append(new_appt)
    return new_appt

@app.patch("/appointments/{appt_id}/cancel", response_model=AppointmentPublic)
def cancel_appointment(
    appt_id: int,
    current_user: dict = Depends(get_current_user),
):
    # 1) Find the appointment
    target = None
    for a in appointments_db:
        if a["id"] == appt_id:
            target = a
            break

    if target is None:
        raise HTTPException(status_code=404, detail="Appointment not found")

    # 2) Already canceled?
    if target.get("status") == "canceled":
        raise HTTPException(status_code=409, detail="Appointment already canceled")

    # 3) Authorization:
    # must be either the client who booked OR the barber
    user_email = current_user["email"]
    if user_email != target["client_email"] and user_email != target["barber_email"]:
        raise HTTPException(status_code=403, detail="Forbidden")

    # 4) Cancel the appointment
    target["status"] = "canceled"
    return target















# from fastapi import FastAPI, Depends
# from pydantic import BaseModel, Field
# from fastapi import HTTPException
# from passlib.context import CryptContext
# from enum import Enum
# from datetime import datetime, timedelta, date, time
# from jose import jwt, JWTError
# from fastapi import Depends
# from fastapi.security import OAuth2PasswordBearer
# from fastapi.security import OAuth2PasswordRequestForm
# from fastapi import status
# from typing import List

# SECRET_KEY = "change-me-later"
# ALGORITHM = "HS256"
# ACCESS_TOKEN_EXPIRE_MINUTES = 30

# app = FastAPI()
# users_db = []
# appointments_db = []
# barber_schedules_db = []   # per barber weekly rules
# barber_blocks_db = []      # per barber date-specific blocks (lunch, day off, etc.)
# SERVICES = {
#      "shape_up": 15,
#     "beard_trim": 15,
#     "haircut": 30,
#     "fade": 30,
#     "scissors_cut": 30,
#     "cut_and_beard": 45,
# }
# shop_settings = {
#   "timezone": "America/New_York",
#   "open_time": "09:00",
#   "close_time": "18:00",
#   "slot_minutes": 15,
# }


# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# def create_access_token(data: dict, expires_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES) -> str:
#     to_encode = data.copy()
#     expire = datetime.utcnow() + timedelta(minutes= expires_minutes)
#     to_encode["exp"] = expire
#     return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

# def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
#     try:
#         payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
#         email: str | None = payload.get("sub")
#         if email is None:
#             raise HTTPException(status_code=401, detail="Invalid token")
#     except JWTError:
#         raise HTTPException(status_code=401, detail="Invalid token")

#     for user in users_db:
#         if user["email"] == email:
#             return user

#     raise HTTPException(status_code=401, detail="User not found")



# def hash_password(password: str) -> str:
#     return pwd_context.hash(password)

# def verify_password(plain: str, hashed: str) -> bool:
#     return pwd_context.verify(plain, hashed)

# def overlaps(start_a, end_a, start_b, end_b) -> bool:
#     return start_a < end_b and start_b < end_a


# class BarberSchedule(BaseModel):
#     working_days: list[int]     # 0=Mon, 1=Tues....
#     day_start: time              
#     day_end: time               
# class UserLogin(BaseModel):
#     email: str
#     password: str = Field(min_length=8, max_length=72)


# class Token(BaseModel):
#     access_token: str
#     token_type: str = "bearer"

# #client or barber role in app
# class UserRole(str, Enum):
#     barber = "barber"
#     client = "client"
# class BlockKind(str,Enum):
#     lunch_break = "lunch_break"
#     day_off = "day_off"


# #public user
# class UserPublic(BaseModel):
#     id: int
#     email: str
#     role: UserRole


# class UserCreate(BaseModel):
#     email : str
#     password: str = Field(min_length=8, max_length=72)
#     role : UserRole

# class AppointmentCreate(BaseModel):
#     starts_at:  datetime
#     client_email: str
#     service: str

# class AppointmentPublic(BaseModel):
#     id: int
#     starts_at: datetime
#     client_email: str
#     barber_email: str
#     service: str
#     status: str


# class BlockCreate(BaseModel):
#     date: date
#     start_time: time
#     kind: BlockKind
    
# class ClientAppointmentCreate(BaseModel):
#     starts_at: datetime
#     service: str


# def require_role(user: dict, role: str):
#     if user["role"] != role:
#         raise HTTPException(status_code=403, detail="Forbidden")
# @app.post("/appointments", response_model=AppointmentPublic, status_code=201)
# def create_appointment(
#     appt: AppointmentCreate,
#     current_user: dict = Depends(get_current_user),
# ):
#     require_role(current_user, "barber")  # only barbers can create
#     barber_email = current_user["email"]

#      # 1) Validate service
#     if appt.service not in SERVICES:
#         raise HTTPException(status_code=422, detail="Service not available")
#     # 2) Validate slot alignment (15-min grid)
#     slot_minutes = shop_settings["slot_minutes"]
#     if appt.starts_at.minute % slot_minutes != 0:
#         raise HTTPException(status_code=422, detail="Start time must be in 15-minute increments")
    
#     # 3) Build appointment interval
#     appt_start = appt.starts_at
#     appt_end = appt_start + timedelta(minutes=SERVICES[appt.service])
#     appt_date = appt_start.date()

#     # 4) Find barber schedule and validate working hours/day
#     schedule = None
#     for s in barber_schedules_db:
#         if s["barber_email"] == barber_email:
#             schedule = s
#             break
#     if schedule is None:
#         raise HTTPException(status_code=409, detail="Weekly schedule must be set before creating appointments")

#     if appt_date.weekday() not in schedule["working_days"]:
#         raise HTTPException(status_code=422, detail="Barber is not scheduled to work that day")
    
#     work_start = datetime.combine(appt_date, schedule["day_start"])
#     work_end = datetime.combine(appt_date, schedule["day_end"])

#     if appt_start < work_start or appt_end > work_end:
#         raise HTTPException(status_code=422, detail="Appointment must be within working hours")
    
#      # 5) Reject overlaps with blocks (lunch/day off)
#     for b in barber_blocks_db:
#         if b["barber_email"] != barber_email:
#             continue
#         if b["date"] != appt_date:
#             continue
#         if overlaps(appt_start, appt_end, b["start"], b["end"]):
#             raise HTTPException(status_code=409, detail="Appointment overlaps a block")
        
#       # 6) Reject overlaps with existing appointments (double-booking)
#     for a in appointments_db:
#         if a["barber_email"] != barber_email:
#             continue
#         if a["starts_at"].date() != appt_date:
#             continue
#         service_key = a.get("service")
#         if service_key not in SERVICES:
#             # Ignore legacy/bad records (or you can choose to raise)
#             continue
#         existing_start = a["starts_at"]
#         existing_end = existing_start + timedelta(minutes=SERVICES[service_key])

#         if overlaps(appt_start, appt_end, existing_start, existing_end):
#             raise HTTPException(status_code=409, detail="Appointment overlaps an existing appointment")


#     new_appt = {
#         "id": len(appointments_db) + 1,
#         "starts_at": appt.starts_at,
#         "client_email": appt.client_email,
#         "barber_email": current_user["email"],
#         "service": appt.service,
#         "status": "booked",   # ← ADD THIS
#     }

#     appointments_db.append(new_appt)
#     return new_appt
# #start up
# @app.get("/health")
# def health_check():
#     return {"status": "ok"}

# @app.get("/me", response_model=UserPublic)
# def me(current_user: dict = Depends(get_current_user)):
#     return {
#         "id": current_user["id"],
#         "email": current_user["email"],
#         "role": current_user["role"],
#     }


# # creating a user, using hashing
# @app.post("/users", status_code=201, response_model=UserPublic)
# def create_user(user: UserCreate):
#     for existing_user in users_db:
#         if existing_user["email"] == user.email:
#             raise HTTPException(
#                 status_code=409,
#                 detail="Email already registered"
#             )
#     password_hash = hash_password(user.password)
#     new_user = {
#         "id": len(users_db) + 1,
#         "email": user.email,
#         "password_hash": password_hash,
#         "role": user.role.value
#     }
#     users_db.append(new_user)
#     public_user = {
#     "id": new_user["id"],
#     "email": new_user["email"],
#     "role": new_user["role"],
#     }
#     return public_user

# @app.post("/auth/login", response_model=Token)
# def login(form_data: OAuth2PasswordRequestForm = Depends()):
#     # Swagger OAuth2 "password" flow uses "username" field
#     email = form_data.username
#     password = form_data.password

#     for existing_user in users_db:
#         if existing_user["email"] == email:
#             if verify_password(password, existing_user["password_hash"]):
#                 token = create_access_token({"sub": existing_user["email"]})
#                 return {"access_token": token, "token_type": "bearer"}
#             raise HTTPException(status_code=401, detail="Invalid credentials")

#     raise HTTPException(status_code=401, detail="Invalid credentials")


# # Creates or updates their schedule
# @app.put('/barbers/me/schedule', response_model=BarberSchedule)
# def create_schedule(schedule: BarberSchedule, current_user: dict = Depends(get_current_user)):
#     require_role(current_user, "barber")  # only barbers can create
#     if not schedule.working_days:
#         raise HTTPException(status_code=422, detail="working_days must contain at least one day")
#     for day in schedule.working_days:
#         if not (0 <= day <= 6):
#             raise HTTPException(status_code=422, detail="working_days must be integers between 0 and 6")
#     if len(schedule.working_days) != len(set(schedule.working_days)):
#         raise HTTPException(status_code=422, detail="working_days cannot contain duplicates")
        
#     # if int(schedule.day_start) >=  int(schedule.day_end):
#     if schedule.day_start >= schedule.day_end:
#         raise HTTPException(status_code=422, detail="day_start cannot be greater than day_end")

#     new_schedule = {
#         "barber_email": current_user["email"],
#         "working_days": schedule.working_days,     
#         "day_start": schedule.day_start,              
#         "day_end": schedule.day_end,              
#     }

#     barber_schedules_db.append(new_schedule)
#     return new_schedule

# @app.put('/barbers/me/blocks')
# def barber_block(block:BlockCreate, current_user: dict = Depends(get_current_user)):
#     require_role(current_user,"barber") # only barbers can creates
#     email = current_user["email"]
#     found = False
#     for record in barber_schedules_db: #checking for the current_user's schedule
#         if email == record["barber_email"]:
#             schedule = record
#             found = True
#             break
#     if not found:
#         raise HTTPException(status_code=409, detail="Weekly schedule must be set before adding blocks")
    
#     weekday = block.date.weekday() # turns date into # 0-6, e.g. 1-14-2026 -> 2, since 0 = Monday, 1 = Tuesday, 2 = Wednesday etc.
#     # check to see if day blocked is in working schedule.
#     if weekday not in schedule['working_days']:
#         raise HTTPException(status_code=422, detail="Not scheduled to work that day")
#     #cannot have a break at 9:17, must be increamants of 15
#     if block.start_time.minute % 15 != 0:
#         raise HTTPException(status_code=422, detail="Time must be in increaments of 15")
#     work_start = datetime.combine(block.date, schedule["day_start"])
#     work_end = datetime.combine(block.date,schedule["day_end"])
#     if block.kind == BlockKind.lunch_break:
#         block_start = datetime.combine(block.date, block.start_time)
#         block_end = block_start + timedelta(minutes=30)
#     else:
#         block_start = datetime.combine(block.date, schedule["day_start"])
#         block_end = datetime.combine(block.date,schedule["day_end"])
        

#     if block_start < work_start or block_end > work_end:
#         raise HTTPException(status_code=422, detail="Block must be within working hours")
#     for existing_block in barber_blocks_db:
#         if existing_block["barber_email"] != email:
#             continue
#         if existing_block["date"] != block.date:
#             continue

#         if block_start < existing_block["end"] and existing_block["start"] < block_end:
#             raise HTTPException(status_code=409, detail="Block overlaps existing block")
        
#     new_block = {
#         "id": len(barber_blocks_db) + 1,
#         "barber_email": email,
#         "date":  block.date,
#         "start": block_start,
#         "end":  block_end,
#         "kind":  block.kind.value
#     }
#     barber_blocks_db.append(new_block)

#     return new_block


# class AvailabilityResponse(BaseModel):
#     barber_email: str
#     date: date
#     available_starts: List[str]

# @app.get("/barbers/{barber_email}/availability", response_model=AvailabilityResponse)
# def barber_availability(barber_email: str, date: date):
#     # 1) Lookup barber schedule
#     barber_schedule = None
#     for barber_record in barber_schedules_db:
#         if barber_record["barber_email"] == barber_email:
#             barber_schedule = barber_record
#             break
#     if barber_schedule is None:
#         raise HTTPException(status_code=404, detail="Barber Not Found")

#     # 2) Check working day
#     weekday = date.weekday()
#     if weekday not in barber_schedule["working_days"]:
#         return {"barber_email": barber_email, "date": date, "available_starts": []}

#     # 3) Build working window
#     work_start = datetime.combine(date, barber_schedule["day_start"])
#     work_end = datetime.combine(date, barber_schedule["day_end"])

#     slot_minutes = shop_settings["slot_minutes"]  # 15
#     slot_delta = timedelta(minutes=slot_minutes)

#     # 4) Collect blocks for this barber + date
#     blocks_for_day = []
#     for b in barber_blocks_db:
#         if b["barber_email"] != barber_email:
#             continue
#         if b["date"] != date:
#             continue
#         blocks_for_day.append(b)

#     # If any day_off exists, no availability
#     for b in blocks_for_day:
#         if b.get("kind") == BlockKind.day_off.value:
#             return {"barber_email": barber_email, "date": date, "available_starts": []}

#     # 5) Collect appointments for this barber + date
#     appts_for_day = []
#     for a in appointments_db:
#         if a["barber_email"] != barber_email:
#             continue
#         if a["starts_at"].date() != date:
#             continue
#         appts_for_day.append(a)

#     # 6) Generate slots, subtract blocks AND appointments
#     available = []
#     current = work_start
#     while current + slot_delta <= work_end:
#         slot_start = current
#         slot_end = current + slot_delta

#         # Block overlap check
#         blocked = False
#         for b in blocks_for_day:
#             if overlaps(slot_start, slot_end, b["start"], b["end"]):
#                 blocked = True
#                 break
#         if blocked:
#             current += slot_delta
#             continue

#         # Appointment overlap check (requires "service" stored on appointment)
#         booked = False
#         for a in appts_for_day:
#             if a.get("status") != "booked":
#                 continue
#             service_key = a.get("service")
#             if service_key not in SERVICES:
#                 # If older appointments exist without a service, skip them (or choose a default)
#                 continue
#             appt_start = a["starts_at"]
#             appt_end = appt_start + timedelta(minutes=SERVICES[service_key])

#             if overlaps(slot_start, slot_end, appt_start, appt_end):
#                 booked = True
#                 break
#         if booked:
#             current += slot_delta
#             continue

#         available.append(slot_start.time().strftime("%H:%M"))
#         current += slot_delta

#     return {"barber_email": barber_email, "date": date, "available_starts": available}

# @app.post("/barbers/{barber_email}/appointments", response_model=AppointmentPublic, status_code=201)
# def client_create_appointment(
#     barber_email: str,
#     appt: ClientAppointmentCreate,
#     current_user: dict = Depends(get_current_user),
# ):
#     require_role(current_user, "client")

#     # 1) Validate service
#     if appt.service not in SERVICES:
#         raise HTTPException(status_code=422, detail="Service not available")

#     # 2) Validate slot alignment (15-min grid)
#     slot_minutes = shop_settings["slot_minutes"]
#     if appt.starts_at.minute % slot_minutes != 0:
#         raise HTTPException(status_code=422, detail="Start time must be in 15-minute increments")

#     # 3) Build appointment interval
#     appt_start = appt.starts_at
#     appt_end = appt_start + timedelta(minutes=SERVICES[appt.service])
#     appt_date = appt_start.date()

#     # 4) Find barber schedule and validate working hours/day
#     schedule = None
#     for s in barber_schedules_db:
#         if s["barber_email"] == barber_email:
#             schedule = s
#             break
#     if schedule is None:
#         raise HTTPException(status_code=404, detail="Barber Not Found")

#     if appt_date.weekday() not in schedule["working_days"]:
#         raise HTTPException(status_code=422, detail="Barber is not scheduled to work that day")

#     work_start = datetime.combine(appt_date, schedule["day_start"])
#     work_end = datetime.combine(appt_date, schedule["day_end"])
#     if appt_start < work_start or appt_end > work_end:
#         raise HTTPException(status_code=422, detail="Appointment must be within working hours")

#     # 5) Reject overlaps with blocks (lunch/day off)
#     for b in barber_blocks_db:
#         if b["barber_email"] != barber_email:
#             continue
#         if b["date"] != appt_date:
#             continue
#         if overlaps(appt_start, appt_end, b["start"], b["end"]):
#             raise HTTPException(status_code=409, detail="Appointment overlaps a block")

#     # 6) Reject overlaps with existing appointments (double-booking)
#     for a in appointments_db:
#         if a["barber_email"] != barber_email:
#             continue
#         if a["starts_at"].date() != appt_date:
#             continue
#         service_key = a.get("service")
#         if service_key not in SERVICES:
#             continue
#         existing_start = a["starts_at"]
#         existing_end = existing_start + timedelta(minutes=SERVICES[service_key])

#         if overlaps(appt_start, appt_end, existing_start, existing_end):
#             raise HTTPException(status_code=409, detail="Appointment overlaps an existing appointment")

#     # 7) Create and save appointment
#     new_appt = {
#         "id": len(appointments_db) + 1,
#         "starts_at": appt.starts_at,
#         "client_email": current_user["email"],  # client is the logged-in user
#         "barber_email": barber_email,           # barber is from the path
#         "service": appt.service,
#         "status": "booked"
#     }
#     appointments_db.append(new_appt)
#     return new_appt

# @app.patch("/appointments/{appt_id}/cancel", response_model=AppointmentPublic)
# def cancel_appointment(
#     appt_id: int,
#     current_user: dict = Depends(get_current_user),
# ):
#     # 1) Find the appointment
#     target = None
#     for a in appointments_db:
#         if a["id"] == appt_id:
#             target = a
#             break

#     if target is None:
#         raise HTTPException(status_code=404, detail="Appointment not found")

#     # 2) Already canceled?
#     if target.get("status") == "canceled":
#         raise HTTPException(status_code=409, detail="Appointment already canceled")

#     # 3) Authorization:
#     # must be either the client who booked OR the barber
#     user_email = current_user["email"]
#     if user_email != target["client_email"] and user_email != target["barber_email"]:
#         raise HTTPException(status_code=403, detail="Forbidden")

#     # 4) Cancel the appointment
#     target["status"] = "canceled"
#     return target
