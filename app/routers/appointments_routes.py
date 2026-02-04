# app/routers/appointments_routes.py

from datetime import datetime, timedelta, date
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app.models import Appointment, BarberSchedule as BarberScheduleModel, BarberBlock as BarberBlockModel
from app.schemas import (
    AppointmentCreate,
    AppointmentPublic,
    ClientAppointmentCreate,
)
from app.auth import get_current_user
from app.deps import require_role
from app.core import overlaps
from app.data import SERVICES, shop_settings
 

router = APIRouter(
    tags=["appointments"],
)
 
@router.post("/appointments", response_model=AppointmentPublic, status_code=201)
def create_appointment(
    appt: AppointmentCreate,
    session: Session = Depends(get_session),
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

     # 0) Prevent booking in the past (naive local time)
    if appt_start < datetime.now():
        raise HTTPException(status_code=422, detail="Cannot book an appointment in the past")

    # 4) Find barber schedule and validate working hours/day
    schedule = session.get(BarberScheduleModel, barber_email)
    if schedule is None:
        raise HTTPException(status_code=409, detail="Weekly schedule must be set before creating appointments")

    if appt_date.weekday() not in schedule.working_days:
        raise HTTPException(status_code=422, detail="Barber is not scheduled to work that day")
    
    work_start = datetime.combine(appt_date, schedule.day_start)
    work_end = datetime.combine(appt_date, schedule.day_end)

    if appt_start < work_start or appt_end > work_end:
        raise HTTPException(status_code=422, detail="Appointment must be within working hours")
    
     # 5) Reject overlaps with blocks (lunch/day off)
    blocks_for_day = session.exec(
        select(BarberBlockModel)
        .where(BarberBlockModel.barber_email == barber_email)
        .where(BarberBlockModel.date == appt_date)
    ).all()

    for b in blocks_for_day:
        if overlaps(appt_start, appt_end, b.start, b.end):
            raise HTTPException(status_code=409, detail="Appointment overlaps a block")
        
    # 6) Reject overlaps with existing DB appointments (during migration)
    day_start_dt = datetime.combine(appt_date, schedule.day_start)
    day_end_dt = datetime.combine(appt_date, schedule.day_end)

    db_appts = session.exec(
        select(Appointment)
        .where(Appointment.barber_email == barber_email)
        .where(Appointment.starts_at >= day_start_dt)
        .where(Appointment.starts_at < day_end_dt)
    ).all()

    for a in db_appts:
        if a.status != "booked":
            continue
        if a.service not in SERVICES:
            continue
        existing_start = a.starts_at
        existing_end = existing_start + timedelta(minutes=SERVICES[a.service])

        if overlaps(appt_start, appt_end, existing_start, existing_end):
            raise HTTPException(status_code=409, detail="Appointment overlaps an existing appointment")



    db_appt = Appointment(
        starts_at=appt_start,
        client_email=appt.client_email,
        barber_email=barber_email,
        service=appt.service,
        status="booked",
    )

    session.add(db_appt)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Appointment already exists for that start time")

    session.refresh(db_appt)  # fills db_appt.id
    return db_appt

@router.patch("/appointments/{appt_id}/cancel", response_model=AppointmentPublic)
def cancel_appointment(
    appt_id: int,
    session: Session = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    # 1) Find the appointment in DB
    target = session.get(Appointment, appt_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Appointment not found")

    # 2) Already canceled?
    if target.status == "canceled":
        raise HTTPException(status_code=409, detail="Appointment already canceled")

    # 3) Authorization: client who booked OR barber
    user_email = current_user["email"]
    if user_email != target.client_email and user_email != target.barber_email:
        raise HTTPException(status_code=403, detail="Forbidden")

    # 4) Cancel and persist
    target.status = "canceled"
    session.add(target)
    session.commit()
    session.refresh(target)

    return target

 
@router.get("/barbers/me/appointments", response_model=List[AppointmentPublic])
def list_barber_appointments(
    status: Optional[str] = "booked",
    on_date: Optional[date] = None,
    session: Session = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    require_role(current_user, "barber")
    email = current_user["email"]

    if status not in ("booked", "canceled", "all"):
        raise HTTPException(status_code=422, detail="status must be 'booked', 'canceled', or 'all'")

    stmt = select(Appointment).where(Appointment.barber_email == email)

    if on_date is not None:
        day_start_dt = datetime.combine(on_date, datetime.min.time())
        day_end_dt = day_start_dt + timedelta(days=1)
        stmt = stmt.where(Appointment.starts_at >= day_start_dt).where(Appointment.starts_at < day_end_dt)

    if status != "all":
        stmt = stmt.where(Appointment.status == status)

    stmt = stmt.order_by(Appointment.starts_at)

    appts = session.exec(stmt).all()
    return appts

@router.post("/barbers/{barber_email}/appointments", response_model=AppointmentPublic, status_code=201)
def client_create_appointment(
    barber_email: str,
    appt: ClientAppointmentCreate,
    session: Session = Depends(get_session),
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

    # 0) Prevent booking in the past (naive local time)
    if appt_start < datetime.now():
        raise HTTPException(status_code=422, detail="Cannot book an appointment in the past")


    # 4) Find barber schedule and validate working hours/day
    schedule = session.get(BarberScheduleModel, barber_email)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Barber Not Found")

    if appt_date.weekday() not in schedule.working_days:
        raise HTTPException(status_code=422, detail="Barber is not scheduled to work that day")

    work_start = datetime.combine(appt_date, schedule.day_start)
    work_end = datetime.combine(appt_date, schedule.day_end)

    if appt_start < work_start or appt_end > work_end:
        raise HTTPException(status_code=422, detail="Appointment must be within working hours")

    # 5) Reject overlaps with blocks (lunch/day off)
    blocks_for_day = session.exec(
        select(BarberBlockModel)
        .where(BarberBlockModel.barber_email == barber_email)
        .where(BarberBlockModel.date == appt_date)
    ).all()

    for b in blocks_for_day:
        if overlaps(appt_start, appt_end, b.start, b.end):
            raise HTTPException(status_code=409, detail="Appointment overlaps a block")

    # 6) Reject overlaps with existing DB appointments (during migration)
    day_start_dt = datetime.combine(appt_date, schedule.day_start)
    day_end_dt = datetime.combine(appt_date, schedule.day_end)

    db_appts = session.exec(
        select(Appointment)
        .where(Appointment.barber_email == barber_email)
        .where(Appointment.starts_at >= day_start_dt)
        .where(Appointment.starts_at < day_end_dt)
    ).all()

    for a in db_appts:
        if a.status != "booked":
            continue
        if a.service not in SERVICES:
            continue
        existing_start = a.starts_at
        existing_end = existing_start + timedelta(minutes=SERVICES[a.service])

        if overlaps(appt_start, appt_end, existing_start, existing_end):
            raise HTTPException(status_code=409, detail="Appointment overlaps an existing appointment")


    # 7) Create and save appointment
    db_appt = Appointment(
        starts_at=appt_start,
        client_email=current_user["email"],
        barber_email=barber_email,
        service=appt.service,
        status="booked",
    )

    session.add(db_appt)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Appointment already exists for that start time")

    session.refresh(db_appt)
    return db_appt

@router.get("/clients/me/appointments", response_model=List[AppointmentPublic])
def list_my_appointments(
    status: Optional[str] = "booked",
    session: Session = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    require_role(current_user, "client")
    email = current_user["email"]

    if status not in ("booked", "canceled", "all"):
        raise HTTPException(status_code=422, detail="status must be 'booked', 'canceled', or 'all'")

    stmt = select(Appointment).where(Appointment.client_email == email)

    if status != "all":
        stmt = stmt.where(Appointment.status == status)

    stmt = stmt.order_by(Appointment.starts_at)

    appts = session.exec(stmt).all()
    return appts
