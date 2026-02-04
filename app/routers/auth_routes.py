# app/routers/auth_routes.py

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select

from app.db import get_session
from app.models import User
from app.schemas import Token
from app.auth import verify_password, create_access_token

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)


@router.post("/login", response_model=Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
):
    email = form_data.username
    password = form_data.password

    user = session.exec(
        select(User).where(User.email == email)
    ).first()

    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user.email})
    return {"access_token": token, "token_type": "bearer"}