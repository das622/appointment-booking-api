# app/main.py

from fastapi import FastAPI

from app.routers.auth_routes import router as auth_router
from app.routers.users_routes import router as users_router
from app.routers.barbers_routes import router as barbers_router
from app.routers.appointments_routes import router as appointments_router

app = FastAPI()

# Mount routers
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(barbers_router)
app.include_router(appointments_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}