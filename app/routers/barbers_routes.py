# app/routers/barbers_routes.py

from datetime import datetime, timedelta, date
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import BarberSchedule as BarberScheduleModel, BarberBlock as BarberBlockModel, Appointment
from app.schemas import BarberSchedule as BarberScheduleSchema, BlockCreate, BlockKind, AvailabilityResponse
from app.auth import get_current_user
from app.deps import require_role
from app.core import overlaps
from app.data import SERVICES, shop_settings

router = APIRouter(
    prefix="/barbers",
    tags=["barbers"],
)

@router.put('/me/schedule', response_model=BarberScheduleSchema)
def create_schedule(
    schedule: BarberScheduleSchema,
    session: Session = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    require_role(current_user, "barber")  # only barbers can create
    if not schedule.working_days:
        raise HTTPException(status_code=422, detail="working_days must contain at least one day")
    for day in schedule.working_days:
        if not (0 <= day <= 6):
            raise HTTPException(status_code=422, detail="working_days must be integers between 0 and 6")
    if len(schedule.working_days) != len(set(schedule.working_days)):
        raise HTTPException(status_code=422, detail="working_days cannot contain duplicates")
        
    if schedule.day_start >= schedule.day_end:
        raise HTTPException(status_code=422, detail="day_start cannot be greater than day_end")

    email = current_user["email"]

    # DB upsert: one schedule per barber (barber_email is PK)
    db_schedule = session.get(BarberScheduleModel, email)
    if db_schedule is None:
        db_schedule = BarberScheduleModel(
            barber_email=email,
            working_days=schedule.working_days,
            day_start=schedule.day_start,
            day_end=schedule.day_end,
        )
        session.add(db_schedule)
    else:
        db_schedule.working_days = schedule.working_days
        db_schedule.day_start = schedule.day_start
        db_schedule.day_end = schedule.day_end

    session.commit()
    session.refresh(db_schedule)

    return {
        "barber_email": db_schedule.barber_email,
        "working_days": db_schedule.working_days,
        "day_start": db_schedule.day_start,
        "day_end": db_schedule.day_end,
    }

@router.get("/me/schedule", response_model=BarberScheduleSchema)
def get_my_schedule(
    session: Session = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    require_role(current_user, "barber")

    email = current_user["email"]
    db_schedule = session.get(BarberScheduleModel, email)
    if db_schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not set")

    # Return dict to match schema cleanly
    return {
        "barber_email": db_schedule.barber_email,
        "working_days": db_schedule.working_days,
        "day_start": db_schedule.day_start,
        "day_end": db_schedule.day_end,
    }

@router.put('/me/blocks')
def barber_block(
    block: BlockCreate,
    session: Session = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    require_role(current_user, "barber")  # only barbers can create
    email = current_user["email"]
    schedule = session.get(BarberScheduleModel, email)
    if schedule is None:
        raise HTTPException(status_code=409, detail="Weekly schedule must be set before adding blocks")

    weekday = block.date.weekday()  # 0 = Monday, ..., 6 = Sunday
    if weekday not in schedule.working_days:
        raise HTTPException(status_code=422, detail="Not scheduled to work that day")
    if block.start_time.minute % 15 != 0:
        raise HTTPException(status_code=422, detail="Time must be in increaments of 15")
    work_start = datetime.combine(block.date, schedule.day_start)
    work_end = datetime.combine(block.date, schedule.day_end)
    if block.kind == BlockKind.lunch_break:
        block_start = datetime.combine(block.date, block.start_time)
        block_end = block_start + timedelta(minutes=30)
    else:
        block_start = datetime.combine(block.date, schedule.day_start)
        block_end = datetime.combine(block.date, schedule.day_end)

    if block_start < work_start or block_end > work_end:
        raise HTTPException(status_code=422, detail="Block must be within working hours")
    existing_blocks = session.exec(
        select(BarberBlockModel)
        .where(BarberBlockModel.barber_email == email)
        .where(BarberBlockModel.date == block.date)
    ).all()

    for existing_block in existing_blocks:
        if block_start < existing_block.end and existing_block.start < block_end:
            raise HTTPException(status_code=409, detail="Block overlaps existing block")

    # Create and persist block in DB
    db_block = BarberBlockModel(
        barber_email=email,
        date=block.date,
        start=block_start,
        end=block_end,
        kind=block.kind.value,
    )

    session.add(db_block)
    session.commit()
    session.refresh(db_block)

    new_block = {
        "id": db_block.id,
        "barber_email": db_block.barber_email,
        "date": db_block.date,
        "start": db_block.start,
        "end": db_block.end,
        "kind": db_block.kind,
    }

    return new_block

@router.get("/{barber_email}/availability", response_model=AvailabilityResponse)
def barber_availability(
    barber_email: str,
    date: date,
    session: Session = Depends(get_session),
):

    # 1) Lookup barber schedule
    barber_schedule = session.get(BarberScheduleModel, barber_email)
    if barber_schedule is None:
        raise HTTPException(status_code=404, detail="Barber Not Found")

    # 2) Check working day
    weekday = date.weekday()
    if weekday not in barber_schedule.working_days:
        return {"barber_email": barber_email, "date": date, "available_starts": []}

    # 3) Build working window
    work_start = datetime.combine(date, barber_schedule.day_start)
    work_end = datetime.combine(date, barber_schedule.day_end)

    slot_minutes = shop_settings["slot_minutes"]  # 15
    slot_delta = timedelta(minutes=slot_minutes)

    # 4) Collect blocks for this barber + date
    blocks_for_day = session.exec(
        select(BarberBlockModel)
        .where(BarberBlockModel.barber_email == barber_email)
        .where(BarberBlockModel.date == date)
    ).all()

    # If any day_off exists, no availability
    for b in blocks_for_day:
        if b.kind == BlockKind.day_off.value:
            return {"barber_email": barber_email, "date": date, "available_starts": []}

    # 5) Collect DB appointments for this barber + date
    day_start_dt = datetime.combine(date, datetime.min.time())
    day_end_dt = day_start_dt + timedelta(days=1)

    appts_for_day = session.exec(
        select(Appointment)
        .where(Appointment.barber_email == barber_email)
        .where(Appointment.starts_at >= day_start_dt)
        .where(Appointment.starts_at < day_end_dt)
    ).all()


    # 6) Generate slots, subtract blocks AND appointments
    available = []
    current = work_start
    while current + slot_delta <= work_end:
        slot_start = current
        slot_end = current + slot_delta

        # Block overlap check
        blocked = False
        for b in blocks_for_day:
            if overlaps(slot_start, slot_end, b.start, b.end):
                blocked = True
                break
        if blocked:
            current += slot_delta
            continue


        # Appointment overlap check (requires "service" stored on appointment)
        booked = False
        for a in appts_for_day:
            if a.status != "booked":
                continue
            service_key = a.service
            if service_key not in SERVICES:
                continue
            appt_start = a.starts_at
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
