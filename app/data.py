# app/data.py

users_db = []
appointments_db = []
barber_schedules_db = []
barber_blocks_db = []

SERVICES = {
    "shape_up": 15,
    "beard_trim": 15,
    "haircut": 30,
    "fade": 30,
    "scissors_cut": 30,
    "cut_and_beard": 45,
}

shop_settings = {
    "timezone": "America/New_York",
    "open_time": "09:00",
    "close_time": "18:00",
    "slot_minutes": 15,
}
