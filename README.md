# Appointment Booking API (Barber Shop Backend)

A FastAPI-based backend for a barber appointment booking system with authentication, scheduling, availability, and appointment management.

This project was built as a portfolio backend to demonstrate:
- REST API design
- Authentication with JWT
- Database modeling with SQLModel
- Schema migrations with Alembic
- Clean project structure using routers

---

##  Features

### Authentication
- User registration (barber or client)
- Login with OAuth2 Password flow
- JWT-based authorization

### Barber Features
- Set weekly working schedule
- Add blocks (lunch breaks, day off)
- View personal schedule
- View booked appointments

### Client Features
- View barber availability
- Book appointments
- Cancel appointments
- View personal appointment history

### Appointments
- Prevent double-booking
- Enforce time-slot alignment
- Respect schedules and blocks
- Status tracking (booked / canceled)

---

##  Tech Stack

- **Python**
- **FastAPI**
- **SQLModel**
- **SQLite** (development)
- **Alembic** (migrations)
- **JWT Authentication**
- **Swagger UI**

---

##  Project Structure

```text
app/
├── main.py
├── db.py
├── models.py
├── schemas.py
├── auth.py
├── deps.py
├── core.py
├── routers/
│   ├── auth_routes.py
│   ├── users_routes.py
│   ├── barbers_routes.py
│   └── appointments_routes.py
alembic/
├── env.py
├── versions/
