# app/routers/users_routes.py

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import User
from app.schemas import UserCreate, UserPublic
from app.auth import get_current_user, hash_password

router = APIRouter(
    tags=["users"],
)

@router.get("/me", response_model=UserPublic)
def me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "email": current_user["email"],
        "role": current_user["role"],
    }


@router.post("/users", status_code=201, response_model=UserPublic)
def create_user(
    user: UserCreate,
    session: Session = Depends(get_session),
):
    # 1) Check if email already exists
    existing = session.exec(
        select(User).where(User.email == user.email)
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email already registered")

    # 2) Create user in DB
    password_hash = hash_password(user.password)

    db_user = User(
        email=user.email,
        password_hash=password_hash,
        role=user.role.value,
    )

    session.add(db_user)
    session.commit()
    session.refresh(db_user)  # fills db_user.id

    # 3) Return public user
    return {
        "id": db_user.id,
        "email": db_user.email,
        "role": db_user.role,
    }