# app/deps.py

from fastapi import HTTPException

def require_role(user: dict, role: str):
    if user["role"] != role:
        raise HTTPException(status_code=403, detail="Forbidden")
