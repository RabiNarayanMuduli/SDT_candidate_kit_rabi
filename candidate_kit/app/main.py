"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, List, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]


def ensure_indexes():
    # employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except Exception:
        pass
    yield


IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: float | Decimal, decimals: int = 2) -> float:
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def validate_epoch_ms(v: Any) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("Must be integer epoch milliseconds")
    if v < 100000000000 or v > 4102444800000:
        raise ValueError("Epoch milliseconds out of range")
    return (v // 1000) * 1000


def epoch_ms_to_dt(ms: int) -> datetime:
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch_ist = punch_in.astimezone(IST)
    h_end, m_end = map(int, shift_end.split(":"))
    h_start, m_start = map(int, shift_start.split(":"))
    is_overnight = (h_end, m_end) <= (h_start, m_start)
    if is_overnight and (punch_ist.hour, punch_ist.minute, punch_ist.second) < (h_end, m_end, 0):
        prev_day = punch_ist.date() - timedelta(days=1)
        return prev_day.isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    punch_ist = punch_in.astimezone(IST).replace(microsecond=0)
    h, m = map(int, shift_start.split(":"))
    year, month, day = map(int, date_str.split("-"))
    start_dt = datetime(year, month, day, h, m, 0, tzinfo=IST)
    diff_sec = int((punch_ist - start_dt).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    p_in = punch_in.replace(microsecond=0)
    p_out = punch_out.replace(microsecond=0)
    seconds = Decimal(int((p_out - p_in).total_seconds()))
    hours = seconds / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    punch_out_ist = punch_out.astimezone(IST).replace(microsecond=0)
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    year, month, day = map(int, date_str.split("-"))
    start_date = datetime(year, month, day, tzinfo=IST)
    if (h_end, m_end) <= (h_start, m_start):
        end_date = start_date + timedelta(days=1)
    else:
        end_date = start_date
    end_dt = end_date.replace(hour=h_end, minute=m_end, second=0, microsecond=0)
    diff_sec = int((punch_out_ist - end_dt).total_seconds())
    if diff_sec >= 1800:
        return diff_sec // 60
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_employee_doc(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at = dt_to_epoch_ms(created_at)
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": created_at,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    hist = []
    for h in doc.get("history") or []:
        changes = {}
        for k, v in (h.get("changes") or {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(f_val, datetime):
                    f_val = dt_to_epoch_ms(f_val)
                if isinstance(t_val, datetime):
                    t_val = dt_to_epoch_ms(t_val)
            changes[k] = {"from": f_val, "to": t_val}

        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_val = dt_to_epoch_ms(at_val)
        hist.append({
            "at": at_val,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })

    p_in = doc.get("punch_in")
    p_out = doc.get("punch_out")
    if isinstance(p_in, datetime):
        p_in = dt_to_epoch_ms(p_in)
    if isinstance(p_out, datetime):
        p_out = dt_to_epoch_ms(p_out)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": p_in,
        "punch_out": p_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": hist,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v):
        if v is not None:
            return validate_epoch_ms(v)
        return v

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        ts = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    d = get_attendance_date(ts, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp.get("shift_start", "09:30"), d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
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
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(422, "Invalid status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v):
        if v is not None:
            return validate_epoch_ms(v)
        return v


class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("Invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamps(cls, v):
        if v is not None:
            return validate_epoch_ms(v)
        return v


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        p_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        p_out_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": p_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    p_in_dt = record["punch_in"]
    if p_in_dt.tzinfo is None:
        p_in_dt = p_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = int((p_out_dt - p_in_dt).total_seconds())
    if diff_sec <= 0:
        raise HTTPException(422, "punched_at must be strictly greater than punch_in")
    if diff_sec > 24 * 3600:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(p_in_dt, p_out_dt)
    overtime_minutes = compute_overtime(
        p_out_dt, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"), record["date"]
    )
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": p_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Record is already punched out")

    return serialize_attendance_doc(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for this date")

    target_status = body.status if body.status is not None else record.get("status")
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch_in/punch_out cannot be supplied when status is ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        calc_date = get_attendance_date(final_punch_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
        if calc_date != date:
            raise HTTPException(422, f"punch_in must stay on the record's attendance date ({date})")

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        if final_punch_out is not None:
            diff_sec = int((final_punch_out - final_punch_in).total_seconds())
            if diff_sec <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if diff_sec > 24 * 3600:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if target_status in ("ABSENT", "LEAVE"):
        final_late_minutes = 0
        final_work_hours = None
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_late_minutes = compute_late_minutes(final_punch_in, emp.get("shift_start", "09:30"), date)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(
                final_punch_out, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"), date
            )
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    changes = {}
    if record.get("status") != target_status:
        changes["status"] = {"from": record.get("status"), "to": target_status}

    old_pin = record.get("punch_in")
    if old_pin != final_punch_in:
        changes["punch_in"] = {"from": old_pin, "to": final_punch_in}

    old_pout = record.get("punch_out")
    if old_pout != final_punch_out:
        changes["punch_out"] = {"from": old_pout, "to": final_punch_out}

    if record.get("work_hours") != final_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": final_work_hours}

    if record.get("late_minutes", 0) != final_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": final_late_minutes}

    if record.get("overtime_minutes", 0) != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": final_overtime_minutes}

    if record.get("half_day", False) != final_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": final_half_day}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    hist_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": hist_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Concurrent modification conflict")

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines Builders
# --------------------------------------------------------------------------- #
import calendar


def get_month_date_range(month_str: str) -> tuple[str, str]:
    year, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, m)
    return f"{month_str}-01", f"{month_str}-{last_day:02d}"


def count_working_days(joined_on_str: str, month_str: str) -> int:
    start_str, end_str = get_month_date_range(month_str)
    if joined_on_str > end_str:
        return 0
    actual_start = max(joined_on_str, start_str)
    d_start = datetime.strptime(actual_start, "%Y-%m-%d").date()
    d_end = datetime.strptime(end_str, "%Y-%m-%d").date()
    curr = d_start
    count = 0
    while curr <= d_end:
        if curr.weekday() < 5:
            count += 1
        curr += timedelta(days=1)
    return count


def build_employee_monthly_pipeline(emp_code: str, month_str: str) -> list:
    start_str, end_str = get_month_date_range(month_str)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_str, "$lte": end_str},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(month_str: str, department: Optional[str] = None) -> list:
    start_str, end_str = get_month_date_range(month_str)
    match_stage = {"joined_on": {"$lte": end_str}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$dept_emps"]},
                                    {"$gte": ["$date", start_str]},
                                    {"$lte": ["$date", end_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(month_str: str, limit: int, department: Optional[str] = None) -> list:
    start_str, end_str = get_month_date_range(month_str)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_str, "$lte": end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def build_department_trend_pipeline(
    emp_codes: list,
    joined_dates: list,
    from_str: str,
    to_str: str,
) -> list:
    from_dt = datetime.strptime(from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_dt = datetime.strptime(to_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_dt_next = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_next],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined_dates,
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
            }
        },
        {"$sort": {"date_dt": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    working_days = count_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = round_half_up(res.get("present_days", 0.0), 2)
        leave_days = res.get("leave_days", 0)
        late_count = res.get("late_count", 0)
        total_late_minutes = res.get("total_late_minutes", 0)
        total_overtime_minutes = res.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        headcount = doc["headcount"]
        logs = doc.get("logs", [])
        present_days = 0.0
        work_hours_list = []
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0

        for log in logs:
            st = log.get("status")
            dow = log.get("dayOfWeek")
            is_weekday = dow in (2, 3, 4, 5, 6)
            if st in ("PRESENT", "WFH", "ON_DUTY"):
                if is_weekday:
                    present_days += 0.5 if log.get("half_day") else 1.0
                if log.get("work_hours") is not None:
                    work_hours_list.append(log["work_hours"])

            lm = log.get("late_minutes", 0)
            if lm > 0:
                late_count += 1
                total_late_minutes += lm

            if st == "LEAVE":
                leave_count += 1
            elif st == "ON_DUTY":
                on_duty_count += 1

        avg_work_hours = (
            round_half_up(sum(work_hours_list) / len(work_hours_list), 2)
            if work_hours_list
            else None
        )

        items.append({
            "department": doc["_id"],
            "headcount": headcount,
            "present_days": round_half_up(present_days, 2),
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    d_from = datetime.strptime(from_date, "%Y-%m-%d").date()
    d_to = datetime.strptime(to_date, "%Y-%m-%d").date()
    if d_to < d_from:
        raise HTTPException(422, "'to' date must be >= 'from' date")
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(404, "Department not found")

    emp_codes = [e["emp_code"] for e in dept_emps]
    joined_dates = sorted([e["joined_on"] for e in dept_emps])

    pipeline = build_department_trend_pipeline(emp_codes, joined_dates, from_date, to_date)
    raw_items = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for item in raw_items:
        ar = item.get("attendance_rate")
        if ar is not None:
            ar = round_half_up(ar, 4)

        ma = item.get("moving_avg_7d")
        if ma is not None:
            ma = round_half_up(ma, 4)

        items.append({
            "date": item["date"],
            "is_working_day": item["is_working_day"],
            "headcount": item["headcount"],
            "present_count": round_half_up(item["present_count"], 2),
            "late_count": item["late_count"],
            "attendance_rate": ar,
            "moving_avg_7d": ma,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"Invalid endpoint: {endpoint}")

    if endpoint == "attendance_list":
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
        skip = (page - 1) * page_size
        explain_res = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
            .explain("executionStats")
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_res = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = build_department_summary_pipeline(month, department)
        explain_res = db.command(
            "aggregate",
            "employees",
            pipeline=pipeline,
            explain=True,
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_res = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required")
        d_from = datetime.strptime(from_date, "%Y-%m-%d").date()
        d_to = datetime.strptime(to_date, "%Y-%m-%d").date()
        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(422, "Invalid from/to range")
        dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]
        joined_dates = sorted([e["joined_on"] for e in dept_emps])
        pipeline = build_department_trend_pipeline(emp_codes, joined_dates, from_date, to_date)
        explain_res = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

