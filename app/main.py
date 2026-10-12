"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
import re

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
@app.on_event("shutdown")
def shutdown():
    client.close()


@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int = 2) -> float:
    factor = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(factor, rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).replace(microsecond=0)


def dt_to_ms(dt: datetime) -> int:
    """datetime -> epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def attendance_date_for_punch(punch_ist: datetime, shift_start: str, shift_end: str) -> str:
    """R1: determine the attendance date for a punch-in."""
    h_end, m_end = map(int, shift_end.split(":"))
    h_start, m_start = map(int, shift_start.split(":"))
    is_overnight = (h_end * 60 + m_end) <= (h_start * 60 + m_start)
    punch_time_minutes = punch_ist.hour * 60 + punch_ist.minute
    shift_end_minutes = h_end * 60 + m_end
    if is_overnight and punch_time_minutes < shift_end_minutes:
        # belongs to shift that started previous day
        return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """R2: grace period 10 min; late_minutes = floor minutes since shift_start."""
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    punch_ist = to_ist(punch_in)
    h, m = map(int, shift_start.split(":"))
    start = punch_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_seconds = int((punch_ist - start).total_seconds())
    if diff_seconds <= 600:  # <= 10 minutes: not late
        return 0
    return diff_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals."""
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str) -> int:
    """R3: floor minutes from shift_end to punch_out, only if >= 30."""
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    punch_out_ist = to_ist(punch_out)
    h, m = map(int, shift_end.split(":"))
    # shift_end on the attendance date in IST
    att_date = datetime.fromisoformat(attendance_date)
    end_ist = att_date.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    # for overnight shifts, shift_end is on the next calendar day
    # detect: if punch_out_ist is before end_ist on same day, end is next day
    if punch_out_ist < end_ist:
        end_ist += timedelta(days=1)
    diff = int((punch_out_ist - end_ist).total_seconds()) // 60
    return diff if diff >= 30 else 0


def serialize_record(doc: dict) -> dict:
    """Convert a MongoDB attendance doc to API response format."""
    doc.pop("_id", None)
    if doc.get("punch_in") is not None:
        doc["punch_in"] = dt_to_ms(doc["punch_in"])
    if doc.get("punch_out") is not None:
        doc["punch_out"] = dt_to_ms(doc["punch_out"])
    if "history" not in doc:
        doc["history"] = []
    if "half_day" not in doc:
        doc["half_day"] = False
    for entry in doc.get("history", []):
        if "at" in entry and isinstance(entry["at"], datetime):
            entry["at"] = dt_to_ms(entry["at"])
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                if isinstance(changes[field].get("from"), datetime):
                    changes[field]["from"] = dt_to_ms(changes[field]["from"])
                if isinstance(changes[field].get("to"), datetime):
                    changes[field]["to"] = dt_to_ms(changes[field]["to"])
    return doc


def serialize_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        if not re.match(r'^EMP\d{4,6}$', v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v):
        if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', v):
            raise ValueError("shift time must be HH:MM")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            date_type.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shift_start_ne_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError("punched_at out of range")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError("punched_at out of range")
        return v


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("must be integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError("out of range")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("invalid status")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v):
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 chars")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v):
        if len(v) < 1 or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 chars")
        return v


# --------------------------------------------------------------------------- #
#  Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for doc in items:
        serialize_employee(doc)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    ts_ist = to_ist(ts)
    d = attendance_date_for_punch(ts_ist, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    # find most recent open record with punch_in <= ts
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no open punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_seconds = int((ts - punch_in_dt).total_seconds())
    if diff_seconds <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if diff_seconds > 86400:
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")

    wh = compute_work_hours(punch_in_dt, ts)
    ot = compute_overtime(ts, emp["shift_end"], record["date"])
    hd = wh < 4.50

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=True,
    )
    if not result:
        raise HTTPException(409, "already punched out")
    return serialize_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    for d in docs:
        serialize_record(d)
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status if body.status is not None else record["status"]
    is_absent_leave = new_status in ("ABSENT", "LEAVE")

    # Validate: ABSENT/LEAVE cannot have punch times supplied
    if is_absent_leave and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")

    # Resolve new punch_in / punch_out
    if is_absent_leave:
        new_pi = None
        new_po = None
    else:
        new_pi_dt = ms_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        new_po_dt = ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")

        # presence status requires punch_in
        if new_pi_dt is None:
            raise HTTPException(422, "presence status requires punch_in")

        # punch_in must stay on the record's attendance date (R1)
        if new_pi_dt.tzinfo is None:
            new_pi_dt = new_pi_dt.replace(tzinfo=timezone.utc)
        pi_ist = to_ist(new_pi_dt)
        pi_date = attendance_date_for_punch(pi_ist, emp["shift_start"], emp["shift_end"])
        if pi_date != date:
            raise HTTPException(422, "punch_in date does not match attendance date")

        if new_po_dt is not None:
            if new_po_dt.tzinfo is None:
                new_po_dt = new_po_dt.replace(tzinfo=timezone.utc)
            diff = int((new_po_dt - new_pi_dt).total_seconds())
            if diff <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if diff > 86400:
                raise HTTPException(422, "punch_out more than 24 hours after punch_in")

        new_pi = new_pi_dt
        new_po = new_po_dt

    # Recompute derived fields
    if is_absent_leave:
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False
    else:
        new_late = compute_late_minutes(new_pi, emp["shift_start"])
        if new_po is not None:
            new_wh = compute_work_hours(new_pi, new_po)
            new_ot = compute_overtime(new_po, emp["shift_end"], date)
            new_hd = new_wh < 4.50
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # Build changes dict
    old_pi = record.get("punch_in")
    old_po = record.get("punch_out")
    old_pi_ms = dt_to_ms(old_pi) if old_pi else None
    old_po_ms = dt_to_ms(old_po) if old_po else None
    new_pi_ms = dt_to_ms(new_pi) if new_pi else None
    new_po_ms = dt_to_ms(new_po) if new_po else None

    old_vals = {
        "status": record.get("status"),
        "punch_in": old_pi_ms,
        "punch_out": old_po_ms,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_vals = {
        "status": new_status,
        "punch_in": new_pi_ms,
        "punch_out": new_po_ms,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_hd,
    }
    changes = {k: {"from": old_vals[k], "to": new_vals[k]} for k in old_vals if old_vals[k] != new_vals[k]}

    if not changes:
        raise HTTPException(422, "no changes detected")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {
            k: {
                "from": ms_to_dt(v["from"]) if k in ("punch_in", "punch_out") and v["from"] is not None else v["from"],
                "to": ms_to_dt(v["to"]) if k in ("punch_in", "punch_out") and v["to"] is not None else v["to"],
            }
            for k, v in changes.items()
        },
    }

    update = {
        "$set": {
            "status": new_status,
            "punch_in": new_pi,
            "punch_out": new_po,
            "work_hours": new_wh,
            "late_minutes": new_late,
            "overtime_minutes": new_ot,
            "half_day": new_hd,
        },
        "$push": {"history": history_entry},
    }

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        update,
        return_document=True,
    )
    if not result:
        raise HTTPException(409, "concurrent modification detected")
    return serialize_record(result)


def _month_bounds(month: str):
    """Return (first_day_str, last_day_str) for a YYYY-MM month string."""
    year, mon = map(int, month.split("-"))
    first = date_type(year, mon, 1)
    if mon == 12:
        last = date_type(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date_type(year, mon + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _count_working_days(start_str: str, end_str: str) -> int:
    """Count Mon-Fri days between start and end inclusive."""
    start = date_type.fromisoformat(start_str)
    end = date_type.fromisoformat(end_str)
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    first_day, last_day = _month_bounds(month)
    # working days from max(joined_on, first_day) to last_day
    effective_start = max(emp["joined_on"], first_day)
    if effective_start > last_day:
        working_days = 0
    else:
        working_days = _count_working_days(effective_start, last_day)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$addFields": {
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$addFields": {
            "is_weekday": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
        }},
        {"$group": {
            "_id": None,
            # present_days: only Mon-Fri (R7)
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        "$is_weekday",
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    ]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                    0
                ]
            }},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            # late_count and overtime: all days including weekends (R7)
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days = round_half_up(r["present_days"], 2)
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    att_pct = None
    if working_days > 0:
        att_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
):
    first_day, last_day = _month_bounds(month)

    emp_match = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"codes": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$in": ["$emp_code", "$$codes"]},
                        {"$gte": ["$date", first_day]},
                        {"$lte": ["$date", last_day]},
                    ]
                }}},
                {"$addFields": {
                    "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                }},
                {"$addFields": {
                    "is_weekday": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
                }},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": "$_id",
            "headcount": 1,
            "present_days": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as": "l",
                    "in": {"$cond": [
                        {"$and": [
                            "$$l.is_weekday",
                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        ]},
                        {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                        0
                    ]}
                }
            }},
            "avg_work_hours": {"$avg": {
                "$map": {
                    "input": {"$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {"$and": [
                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$$l.work_hours", None]},
                            {"$gt": ["$$l.work_hours", None]},
                        ]}
                    }},
                    "as": "l",
                    "in": "$$l.work_hours"
                }
            }},
            "late_count": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as": "l",
                    "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}
                }
            }},
            "total_late_minutes": {"$sum": {
                "$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}
            }},
            "leave_count": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as": "l",
                    "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
                }
            }},
            "on_duty_count": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as": "l",
                    "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
                }
            }},
        }},
        {"$sort": {"department": ASCENDING}},
    ]

    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        avg_wh = r.get("avg_work_hours")
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(avg_wh, 2) if avg_wh is not None else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = _month_bounds(month)

    emp_match = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if emp_match:
        pipeline.append({"$match": {f"emp.{k}": v for k, v in emp_match.items()}})

    pipeline += [
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}},
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        from_d = date_type.fromisoformat(from_date)
        to_d = date_type.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "invalid date format")

    if to_d < from_d:
        raise HTTPException(422, "to must be >= from")
    if (to_d - from_d).days > 92:
        raise HTTPException(422, "range exceeds 92 days")

    # Check department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    pipeline = [
        # Start from attendance logs in range for this department's employees
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                    0
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        # Densify to fill gaps
        {"$densify": {
            "field": "_id",
            "range": {"step": 1, "unit": "day", "bounds": [from_date, to_date]},
        }},
        {"$addFields": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}},
        }},
        {"$addFields": {
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
        }},
        {"$sort": {"_id": ASCENDING}},
        # headcount: employees in dept with joined_on <= that day
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$_id"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq": ["$department", department]},
                        {"$lte": ["$joined_on", "$$day"]},
                    ]
                }}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        # 7-day moving average over attendance_rate (non-null only)
        {"$setWindowFields": {
            "sortBy": {"_id": ASCENDING},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                },
            },
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    result = []
    for r in rows:
        ar = r.get("attendance_rate")
        ma = r.get("moving_avg_7d")
        result.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": r["present_count"],
            "late_count": r["late_count"],
            "attendance_rate": round_half_up(ar, 4) if ar is not None else None,
            "moving_avg_7d": round_half_up(ma, 4) if ma is not None else None,
        })
    return {"department": department, "items": result}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    valid = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid:
        raise HTTPException(422, f"endpoint must be one of {sorted(valid)}")

    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explained = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    if endpoint == "employee_monthly":
        if not month:
            raise HTTPException(422, "month is required")
        if not emp_code:
            raise HTTPException(422, "emp_code is required")
        first_day, last_day = _month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explained = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first_day, last_day = _month_bounds(month)
        emp_match = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        explained = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": explained}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first_day, last_day = _month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        explained = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    if endpoint == "department_trend":
        if not from_date or not to_date:
            raise HTTPException(422, "from and to are required")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
        ]
        explained = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}
