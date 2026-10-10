"""Employee Attendance & Analytics API."""
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# load_dotenv does not overwrite process environment variables.
load_dotenv()
mongo_uri = os.getenv("MONGO_URI")
mongo_db = os.getenv("MONGO_DB")
if not mongo_uri or not mongo_db:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set in the environment or local .env")
client = MongoClient(mongo_uri, tz_aware=True, tzinfo=timezone.utc)
db = client[mongo_db]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = PRESENCE + ("ABSENT", "LEAVE")
@asynccontextmanager
async def lifespan(_app):
    _indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def _indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_emp_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_emp")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_status_date_emp")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_late_emp")


def _date(value: str, field: str = "date") -> date:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")


def _month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        first = date.fromisoformat(value + "-01")
        nxt = date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
    except ValueError:
        raise HTTPException(422, "month is outside the supported date range")
    return first, nxt


def _instant(ms: int) -> datetime:
    try:
        return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "timestamp out of range")


def _ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86400 + delta.seconds) * 1000) + delta.microseconds // 1000


def _serialize(value):
    if isinstance(value, datetime):
        return _ms(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items() if k != "_id"}
    return value


def _json_safe(value):
    """Convert BSON values in raw explain output without renaming its internal keys."""
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _record(doc):
    out = _serialize(doc)
    out.setdefault("work_hours", None)
    out.setdefault("punch_in", None)
    out.setdefault("punch_out", None)
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("half_day", False)
    out.setdefault("history", [])
    return out


def _round(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def _shift_anchor(punch_in: datetime, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    local = punch_in.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    overnight = (end_h, end_m) <= (start_h, start_m)
    anchor = local.date()
    if overnight and (local.hour, local.minute) < (end_h, end_m):
        anchor -= timedelta(days=1)
    start = datetime.combine(anchor, time(start_h, start_m), IST)
    end_day = anchor + (timedelta(days=1) if overnight else timedelta())
    end = datetime.combine(end_day, time(end_h, end_m), IST)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str = "23:59") -> int:
    start, _ = _shift_anchor(punch_in, shift_start, shift_end)
    seconds = int((punch_in - start.astimezone(UTC)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return _round(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "00:00") -> int:
    # date_str is the attendance/shift-start date; shift_end rolls to the next day overnight.
    end_h, end_m = map(int, shift_end.split(":"))
    start_h, start_m = map(int, shift_start.split(":"))
    day = _date(date_str)
    if (end_h, end_m) <= (start_h, start_m):
        day += timedelta(days=1)
    end = datetime.combine(day, time(end_h, end_m), IST).astimezone(UTC)
    seconds = int((punch_out - end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def _derived(status, pin, pout, emp, attendance_date):
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pin, emp["shift_start"], emp["shift_end"]) if pin else 0
    if not pout:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(pin, pout)
    return {"work_hours": hours, "late_minutes": late,
            "overtime_minutes": compute_overtime(pout, emp["shift_end"], attendance_date, emp["shift_start"]),
            "half_day": hours < 4.5}


def _rounding_expr(expr, scale=2):
    # Decimal128 plus floor implements half-up; MongoDB $round uses ties-to-even.
    factor = Decimal128(str(10 ** scale))
    value = {"$convert": {"input": expr, "to": "decimal"}}
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [value, factor]}, Decimal128("0.5")]}}, factor]}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value):
        _date(value, "joined_on")
        return value

    @field_validator("shift_end")
    @classmethod
    def different_shift(cls, value, info):
        if info.data.get("shift_start") == value:
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(StrictModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = Field(default=None, le=4_102_444_800_000)
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, value):
        if value is not None and value < 100_000_000_000:
            raise ValueError("must be epoch milliseconds in range")
        return value

    @field_validator("status")
    @classmethod
    def valid_presence(cls, value):
        if value not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return value


class PunchOutIn(StrictModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = Field(default=None, le=4_102_444_800_000)

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, value):
        if value is not None and value < 100_000_000_000:
            raise ValueError("must be epoch milliseconds in range")
        return value


class RegularizeIn(StrictModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(default=None, le=4_102_444_800_000)
    punch_out: Optional[StrictInt] = Field(default=None, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_epoch(cls, value):
        if value is not None and value < 100_000_000_000:
            raise ValueError("must be epoch milliseconds in range")
        return value


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _serialize(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_serialize(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an epoch millisecond integer")
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = _instant(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    local = ts.astimezone(IST)
    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))
    if (end_h, end_m) <= (start_h, start_m) and (local.hour, local.minute) < (end_h, end_m):
        local = local - timedelta(days=1)
    day = local.date().isoformat()
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return _record(doc)


def _attendance_query(emp_code, date_from, date_to, status):
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds = {}
        if date_from is not None: bounds["$gte"] = _date(date_from, "date_from").isoformat()
        if date_to is not None: bounds["$lte"] = _date(date_to, "date_to").isoformat()
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not exceed date_to")
        q["date"] = bounds
    if status is not None:
        if status not in STATUSES: raise HTTPException(422, "invalid status")
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = _attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an epoch millisecond integer")
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp: raise HTTPException(404, "employee not found")
    ts = _instant(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not doc: raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None: raise HTTPException(409, "already punched out")
    delta = (ts - doc["punch_in"]).total_seconds()
    if delta <= 0 or delta > 86400: raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    values = _derived(doc["status"], doc["punch_in"], ts, emp, doc["date"])
    updated = db.attendance_logs.find_one_and_update({"_id": doc["_id"], "punch_out": None}, {"$set": {"punch_out": ts, **values}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "already punched out")
    return _record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    if any(key in body.model_fields_set and getattr(body, key) is None for key in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "provided fields cannot be null")
    attendance_date = date
    _date(attendance_date)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_date})
    if not emp or not doc: raise HTTPException(404, "employee or attendance record not found")
    old = {"status": doc.get("status"), "punch_in": doc.get("punch_in"), "punch_out": doc.get("punch_out"),
           "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
           "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False)}
    status = body.status if body.status is not None else old["status"]
    if status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
    pin = _instant(body.punch_in) if body.punch_in is not None else old["punch_in"]
    pout = _instant(body.punch_out) if body.punch_out is not None else old["punch_out"]
    if status in PRESENCE and pin is None: raise HTTPException(422, "presence status requires punch_in")
    if status in ("ABSENT", "LEAVE"):
        pin = pout = None
    if pin is not None and pin.astimezone(IST).date().isoformat() != attendance_date:
        # Overnight punch-ins before shift_end belong to the previous shift date.
        local = pin.astimezone(IST)
        sh, sm = map(int, emp["shift_start"].split(":")); eh, em = map(int, emp["shift_end"].split(":"))
        actual_day = (local.date() - timedelta(days=1) if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em) else local.date()).isoformat()
        if actual_day != attendance_date: raise HTTPException(422, "punch_in must belong to the record date")
    if pout is not None:
        if pin is None or (pout - pin).total_seconds() <= 0 or (pout - pin).total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    values = _derived(status, pin, pout, emp, attendance_date)
    new = {"status": status, "punch_in": pin, "punch_out": pout, **values}
    changes = {}
    for key, value in new.items():
        if old.get(key) != value:
            changes[key] = {"from": old.get(key), "to": value}
    if not changes: raise HTTPException(422, "request does not change the record")
    at = datetime.now(UTC).replace(microsecond=0)
    entry = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap makes simultaneous regularizations conflict rather than lose history.
    cas_query = {"_id": doc["_id"], "status": doc["status"], "punch_in": doc.get("punch_in"), "punch_out": doc.get("punch_out"),
                 "$or": [{"history": doc.get("history", [])}, {"history": {"$exists": False}}]}
    updated = db.attendance_logs.find_one_and_update(
        cas_query,
        {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry")
    return _record(updated)


def _month_match(month):
    first, nxt = _month(month)
    return first.isoformat(), nxt.isoformat()


def _working_days(start: date, end_exclusive: date) -> int:
    """Kept as a pure utility for verification; endpoint calculations use MongoDB."""
    days = (end_exclusive - start).days
    whole_weeks, remainder = divmod(days, 7)
    count = whole_weeks * 5
    for offset in range(remainder):
        if (start.weekday() + offset) % 7 < 5:
            count += 1
    return count


def _working_days_pipeline(month: str, joined_on: str):
    first, nxt = _month(month)
    weekday_array = {
        "$map": {
            "input": {"$range": [0, "$count"]},
            "as": "offset",
            "in": {"$dayOfWeek": {"$dateAdd": {
                "startDate": "$start", "unit": "day", "amount": "$$offset"
            }}},
        }
    }
    return [
        {"$documents": [{"first": first.isoformat(), "next": nxt.isoformat(), "joined_on": joined_on}]},
        {"$set": {"start": {"$dateFromString": {"dateString": {"$min": ["$next", {"$max": ["$first", "$joined_on"]}]}}},
                   "finish": {"$dateFromString": {"dateString": "$next"}}}},
        {"$set": {"count": {"$dateDiff": {"startDate": "$start", "endDate": "$finish", "unit": "day"}}}},
        {"$set": {"days": weekday_array}},
        {"$project": {"_id": 0, "working_days": {"$size": {"$filter": {
            "input": "$days", "as": "dow", "cond": {"$and": [
                {"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}
            ]}
        }}}}},
    ]


def _employee_monthly_pipeline(emp_code: str, month: str):
    first_s, next_s = _month_match(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lt": next_s}}},
        {"$set": {"_present": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}, {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}}},
        {"$group": {"_id": None, "present_days": {"$sum": "$_present"}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp: raise HTTPException(404, "employee not found")
    work_row = next(iter(db.command({"aggregate": 1, "pipeline": _working_days_pipeline(month, emp["joined_on"]), "cursor": {}})["cursor"]["firstBatch"]), {})
    working = work_row.get("working_days", 0)
    row = next(iter(db.attendance_logs.aggregate(_employee_monthly_pipeline(emp_code, month), allowDiskUse=True)), {})
    present = _round(Decimal(str(row.get("present_days", 0))), 2)
    pct = _round(Decimal(str(present)) / Decimal(working) * 100, 4) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present, "leave_days": row.get("leave_days", 0),
            "late_count": row.get("late_count", 0), "total_late_minutes": row.get("total_late_minutes", 0),
            "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": pct}


def _department_pipeline(month, department=None):
    first_s, next_s = _month_match(month)
    match = {"joined_on": {"$lte": (date.fromisoformat(next_s) - timedelta(days=1)).isoformat()}}
    if department is not None: match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "dept": "$department"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lt": ["$date", next_s]}]}}},
            {"$set": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, "$_weekday"]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, {"$toDecimal": "$work_hours"}, 0]}},
                "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}
        ], "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
                    "hours_sum": {"$sum": {"$ifNull": ["$stats.hours_sum", 0]}}, "hours_count": {"$sum": {"$ifNull": ["$stats.hours_count", 0]}},
                    "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
                    "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}}, "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}}}},
        {"$set": {"department": "$_id", "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, _rounding_expr({"$divide": ["$hours_sum", "$hours_count"]}, 2), None]},
                   "present_days": _rounding_expr("$present_days", 2)}},
        {"$sort": {"department": 1}}, {"$project": {"_id": 0, "department": 1, "headcount": 1, "present_days": 1,
            "avg_work_hours": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    items = list(db.employees.aggregate(_department_pipeline(month, department), allowDiskUse=True))
    return {"month": month, "items": _serialize(items)}


def _leaderboard_pipeline(month, department=None, limit=10):
    first_s, next_s = _month_match(month)
    pipeline = [{"$match": {"date": {"$gte": first_s, "$lt": next_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(_leaderboard_pipeline(month, department, limit), allowDiskUse=True))
    return {"month": month, "items": items}


def _trend_pipeline(department, start, end):
    # Start from the indexed department query, then generate the full calendar range in MongoDB.
    start_d, end_d = _date(start, "from"), _date(end, "to")
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {"startDate": {"$dateFromString": {"dateString": {"$literal": start_d.isoformat()}}},
                   "endDate": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": {"$literal": end_d.isoformat()}}}, "unit": "day", "amount": 1}}}},
        {"$set": {"dates": {"$map": {"input": {"$range": [0, {"$dateDiff": {"startDate": "$startDate", "endDate": "$endDate", "unit": "day"}}]},
            "as": "offset", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": "$startDate", "unit": "day", "amount": "$$offset"}}, "format": "%Y-%m-%d"}}}}}},
        {"$unwind": "$dates"},
        {"$set": {"date": "$dates", "eligible": {"$filter": {"input": "$employees", "as": "employee",
            "cond": {"$lte": ["$$employee.joined_on", "$dates"]}}}}},
        {"$set": {"headcount": {"$size": "$eligible"}}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date", "codes": "$eligible.emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
            {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                   "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
                   "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$set": {"attendance_rate": {"$cond": [{"$ne": ["$attendance_rate", None]}, _rounding_expr("$attendance_rate", 4), None]}, "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]}, _rounding_expr("$moving_avg_7d", 4), None]}}},
        {"$sort": {"date": 1}}, {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to: str = Query(...)):
    start = from_date
    end = to
    start_d, end_d = _date(start, "from"), _date(end, "to")
    if end_d < start_d or (end_d - start_d).days + 1 > 92: raise HTTPException(422, "invalid date range (maximum 92 days)")
    if not db.employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "department not found")
    pipeline = _trend_pipeline(department, start, end)
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    return {"department": department, "items": _serialize(items)}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_date: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed: raise HTTPException(422, "unknown endpoint")
    try:
        if endpoint == "attendance_list":
            query = _attendance_query(emp_code, date_from, date_to, status)
            cmd = {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
            collection, explain = "attendance_logs", db.command("explain", cmd, verbosity="executionStats")
        elif endpoint == "employee_monthly":
            if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
            collection = "attendance_logs"
            explain = db.command("explain", {"aggregate": collection, "pipeline": _employee_monthly_pipeline(emp_code, month), "cursor": {}}, verbosity="executionStats")
        elif endpoint == "department_summary":
            if not month: raise HTTPException(422, "month is required")
            # Explain the same cross-collection employee-led aggregation.
            collection = "employees"
            explain = db.command("explain", {"aggregate": collection, "pipeline": _department_pipeline(month, department), "cursor": {}}, verbosity="executionStats")
        elif endpoint == "late_leaderboard":
            if not month: raise HTTPException(422, "month is required")
            collection = "attendance_logs"
            explain = db.command("explain", {"aggregate": collection, "pipeline": _leaderboard_pipeline(month, department, limit), "cursor": {}}, verbosity="executionStats")
        else:
            start = from_date
            if start is None or to is None or not department: raise HTTPException(422, "department, from and to are required")
            sd, ed = _date(start, "from"), _date(to, "to")
            if ed < sd or (ed - sd).days + 1 > 92: raise HTTPException(422, "invalid date range")
            collection = "employees"
            explain = db.command("explain", {"aggregate": "employees", "pipeline": _trend_pipeline(department, start, to), "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": _json_safe(json_util.loads(json_util.dumps(explain)))}
    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(422, f"unable to explain query: {exc.__class__.__name__}")
