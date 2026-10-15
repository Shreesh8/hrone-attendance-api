import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time as Time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

IX_EMP_UNIQUE = "emp_code_unique"
IX_EMP_DEPT_CODE = "dept_emp_code"
IX_EMP_DEPT_JOINED = "dept_joined_on"
IX_EMP_JOINED = "joined_on"
IX_LOG_EMP_DATE = "emp_date_unique"
IX_LOG_DATE_EMP = "date_desc_emp"
IX_LOG_STATUS_DATE = "status_date_desc_emp"

_indexes_ready = False
_index_lock = threading.Lock()


def ensure_indexes() -> None:
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return
        employees.create_indexes([
            IndexModel([("emp_code", ASCENDING)], name=IX_EMP_UNIQUE, unique=True),
            IndexModel([("department", ASCENDING), ("emp_code", ASCENDING)], name=IX_EMP_DEPT_CODE),
            IndexModel([("department", ASCENDING), ("joined_on", ASCENDING)], name=IX_EMP_DEPT_JOINED),
            IndexModel([("joined_on", ASCENDING)], name=IX_EMP_JOINED),
        ])
        logs.create_indexes([
            IndexModel([("emp_code", ASCENDING), ("date", ASCENDING)], name=IX_LOG_EMP_DATE, unique=True),
            IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)], name=IX_LOG_DATE_EMP),
            IndexModel([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
                       name=IX_LOG_STATUS_DATE),
        ])
        _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:
        log.exception("index creation failed at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ABSENCE = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


def unprocessable(loc: list, msg: str):
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms - ms % 1000)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def hhmm(value: str) -> Time:
    h, m = value.split(":")
    return Time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def ist_at(date_str: str, hhmm_str: str, add_days: int = 0) -> datetime:
    d = Date.fromisoformat(date_str) + timedelta(days=add_days)
    return datetime.combine(d, hhmm(hhmm_str), tzinfo=IST)


def attendance_date(emp: dict, punch_in: datetime) -> str:
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(emp["shift_start"], emp["shift_end"]) and ist.time() < hhmm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(date_str: str, shift_start: str, punch_in: datetime) -> int:
    secs = int((punch_in - ist_at(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > 600 else 0


def round_half_up(value, places: int) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(date_str: str, shift_start: str, shift_end: str, punch_out: datetime) -> int:
    end = ist_at(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    secs = int((punch_out - end).total_seconds())
    mins = secs // 60 if secs > 0 else 0
    return mins if mins >= 30 else 0


def derive(emp: dict, date_str: str, status: str, punch_in: Optional[datetime],
           punch_out: Optional[datetime]) -> dict:
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(date_str, emp["shift_start"], punch_in)
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(date_str, emp["shift_start"], emp["shift_end"], punch_out)
        out["half_day"] = Decimal(str(wh)) < Decimal("4.50")
    return out


def parse_ymd(value: str, loc: list) -> Date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value or ""):
        unprocessable(loc, "must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date")


def month_bounds(month: str) -> tuple[Date, Date]:
    year, mon = int(month[:4]), int(month[5:])
    if year < 1:
        unprocessable(["query", "month"], "year out of range")
    return Date(year, mon, 1), Date(year, mon, calendar.monthrange(year, mon)[1])


MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def serialize_history_entry(e: dict) -> dict:
    changes = {}
    for key, ch in (e.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"):
            changes[key] = {"from": to_ms(ch.get("from")), "to": to_ms(ch.get("to"))}
        else:
            changes[key] = {"from": ch.get("from"), "to": ch.get("to")}
    return {"at": to_ms(e.get("at")), "by": e.get("by"), "reason": e.get("reason"), "changes": changes}


def serialize_record(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": int(d.get("late_minutes") or 0),
        "overtime_minutes": int(d.get("overtime_minutes") or 0),
        "half_day": bool(d.get("half_day") or False),
        "history": [serialize_history_entry(e) for e in (d.get("history") or [])],
    }


def serialize_employee(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"], "name": d["name"], "email": d["email"], "department": d["department"],
        "shift_start": d["shift_start"], "shift_end": d["shift_end"], "joined_on": d["joined_on"],
        "created_at": to_ms(d.get("created_at")),
    }


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
            raise ValueError("must be YYYY-MM-DD")
        Date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)
    cur = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(emp, ts)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           **derive(emp, d, body.status, ts, None), "history": []}
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    for _ in range(3):
        rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                            sort=[("date", DESCENDING)], hint=IX_LOG_EMP_DATE)
        if rec is None:
            raise HTTPException(404, "no punch-in found")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        p_in = rec["punch_in"]
        if ts <= p_in or ts - p_in > timedelta(hours=24):
            unprocessable(["body", "punched_at"], "must be after punch_in and within 24 hours of it")
        fields = {"punch_out": ts, **derive(emp, rec["date"], rec["status"], p_in, ts)}
        res = logs.update_one({"_id": rec["_id"], "punch_out": None, "punch_in": p_in, "status": rec["status"]},
                              {"$set": fields})
        if res.matched_count:
            return serialize_record({**rec, **fields})
    raise HTTPException(409, "record changed concurrently, retry")


def build_attendance_query(emp_code, date_from, date_to, status):
    if date_from:
        parse_ymd(date_from, ["query", "date_from"])
    if date_to:
        parse_ymd(date_to, ["query", "date_to"])
    if date_from and date_to and date_from > date_to:
        unprocessable(["query", "date_from"], "date_from must be <= date_to")
    q: dict[str, Any] = {}
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
    if emp_code:
        hint = IX_LOG_EMP_DATE
    elif status:
        hint = IX_LOG_STATUS_DATE
    else:
        hint = IX_LOG_DATE_EMP
    return q, hint


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q, hint = build_attendance_query(emp_code, date_from, date_to, status)
    total = logs.count_documents(q)
    cur = logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size).hint(hint)
    return {"items": [serialize_record(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    d = parse_ymd(date, ["path", "date"])
    d_str = d.isoformat()
    emp = get_employee(emp_code)
    for _ in range(5):
        rec = logs.find_one({"emp_code": emp_code, "date": d_str})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")

        new_status = body.status or old_status
        if new_status in ABSENCE:
            if body.punch_in is not None or body.punch_out is not None:
                unprocessable(["body"], "ABSENT/LEAVE clear punch times; do not supply them")
            new_in = new_out = None
        else:
            new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
            new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out
            if new_in is None:
                unprocessable(["body", "punch_in"], "a presence status requires punch_in")
            if attendance_date(emp, new_in) != d_str:
                unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
            if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):
                unprocessable(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")

        new_derived = derive(emp, d_str, new_status, new_in, new_out)
        before = {"status": old_status, "punch_in": old_in, "punch_out": old_out,
                  "work_hours": rec.get("work_hours"), "late_minutes": int(rec.get("late_minutes") or 0),
                  "overtime_minutes": int(rec.get("overtime_minutes") or 0),
                  "half_day": bool(rec.get("half_day") or False)}
        after = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **new_derived}
        changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
        if not changes:
            unprocessable(["body"], "request changes nothing")

        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        history = rec.get("history")
        guard = {"history": {"$size": len(history)}} if isinstance(history, list) else {"history": {"$exists": False}}
        res = logs.update_one(
            {"emp_code": emp_code, "date": d_str, "status": old_status, "punch_in": old_in,
             "punch_out": old_out, **guard},
            {"$set": {k: v["to"] for k, v in changes.items()}, "$push": {"history": entry}},
        )
        if res.matched_count:
            return serialize_record({**rec, **after, "history": (history or []) + [entry]})
    raise HTTPException(409, "record is being modified concurrently, retry")


IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
LATE = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE, 0]}
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}
PRESENT_WEEKDAY_VALUE = {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$in": [{"$type": "$work_hours"}, ["double", "int", "long", "decimal"]]}]}


def build_employee_monthly(emp_code: str, month: str):
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_WEEKDAY_VALUE},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return pipeline, IX_LOG_EMP_DATE


def working_days(month: str, joined_on: str) -> int:
    start, end = month_bounds(month)
    d = max(start, Date.fromisoformat(joined_on))
    n = 0
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee(emp_code)
    pipeline, hint = build_employee_monthly(emp_code, month)
    row = next(iter(logs.aggregate(pipeline, hint=hint)), {})
    wd = working_days(month, emp["joined_on"])
    present = float(row.get("present_days", 0))
    pct = float(round_half_up(Decimal(str(present)) / wd * 100, 2)) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": int(row.get("leave_days", 0)), "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)), "attendance_pct": pct}


def build_department_summary(month: str, department: Optional[str]):
    start, end = month_bounds(month)
    emp_match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department:
        emp_match["department"] = department
    per_employee = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_WEEKDAY_VALUE},
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": LATE},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "wh_sum": {"$sum": {"$cond": [HAS_HOURS, {"$toDecimal": "$work_hours"}, Decimal128("0")]}},
            "wh_n": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
        }},
    ]
    zero = Decimal128("0")
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "agg"}},
        {"$set": {"agg": {"$ifNull": [{"$arrayElemAt": ["$agg", 0]}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$agg.present_days", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$agg.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$agg.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$agg.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$agg.on_duty_count", 0]}},
            "wh_sum": {"$sum": {"$ifNull": ["$agg.wh_sum", zero]}},
            "wh_n": {"$sum": {"$ifNull": ["$agg.wh_n", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    return pipeline, (IX_EMP_DEPT_JOINED if department else IX_EMP_JOINED)


def _dec(v) -> Decimal:
    return v.to_decimal() if isinstance(v, Decimal128) else Decimal(str(v))


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    pipeline, hint = build_department_summary(month, department)
    items = []
    for r in employees.aggregate(pipeline, hint=hint):
        n = int(r["wh_n"])
        avg = float(round_half_up(_dec(r["wh_sum"]) / n, 2)) if n else None
        items.append({"department": r["_id"], "headcount": int(r["headcount"]),
                      "present_days": float(r["present_days"]), "avg_work_hours": avg,
                      "late_count": int(r["late_count"]), "total_late_minutes": int(r["total_late_minutes"]),
                      "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"])})
    return {"month": month, "items": items}


def build_leaderboard(month: str, limit: int, department: Optional[str]):
    start, end = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE},
                    "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline, IX_LOG_DATE_EMP


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=MONTH_PATTERN), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    pipeline, hint = build_leaderboard(month, limit, department)
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
              "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
             for r in logs.aggregate(pipeline, hint=hint)]
    return {"month": month, "items": items}


def validate_trend_range(frm: str, to: str) -> tuple[Date, Date]:
    d_from = parse_ymd(frm, ["query", "from"])
    d_to = parse_ymd(to, ["query", "to"])
    if d_to < d_from:
        unprocessable(["query", "to"], "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        unprocessable(["query", "to"], "range must not exceed 92 days")
    return d_from, d_to


def build_trend(department: str, d_from: Date, d_to: Date):
    n_days = (d_to - d_from).days + 1
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    win = {"documents": [-6, 0]}
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        {"$lookup": {"from": "attendance_logs", "localField": "codes", "foreignField": "emp_code",
                     "pipeline": [
                         {"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
                         {"$group": {"_id": "$date", "present": {"$sum": PRESENT_VALUE},
                                     "late": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
                     ], "as": "daily"}},
        {"$project": {"_id": 0, "joined": 1, "daily": 1, "offset": {"$range": [0, n_days]}}},
        {"$unwind": "$offset"},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": {
            "$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$offset"}}}}}},
        {"$set": {
            "day": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "x",
                                                  "cond": {"$eq": ["$$x._id", "$date"]}}}, 0]},
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j",
                                                "cond": {"$lte": ["$$j", "$date"]}}}},
            "is_working_day": IS_WEEKDAY,
        }},
        {"$set": {"present_count": {"$ifNull": ["$day.present", 0]}, "late_count": {"$ifNull": ["$day.late", 0]}}},
        {"$set": {"r4": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_count", 20000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$set": {"has_rate": {"$cond": [{"$eq": ["$r4", None]}, 0, 1]}}},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "win_sum": {"$sum": "$r4", "window": win}, "win_n": {"$sum": "$has_rate", "window": win}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "r4": 1,
                      "m4": {"$cond": [{"$gt": ["$win_n", 0]},
                                       {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$win_sum"]}, "$win_n"]},
                                                               {"$multiply": [2, "$win_n"]}]}},
                                       None]}}},
    ]
    return pipeline, IX_EMP_DEPT_JOINED


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, frm: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = validate_trend_range(frm, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    pipeline, hint = build_trend(department, d_from, d_to)
    items = [{"date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
              "present_count": float(r["present_count"]), "late_count": int(r["late_count"]),
              "attendance_rate": None if r["r4"] is None else int(r["r4"]) / 10000,
              "moving_avg_7d": None if r["m4"] is None else int(r["m4"]) / 10000}
             for r in employees.aggregate(pipeline, hint=hint)]
    return {"department": department, "items": items}


def _jsonable(doc: dict) -> dict:
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(**vals):
        for name, v in vals.items():
            if not v:
                unprocessable(["query", name], "required for this endpoint")

    if endpoint == "attendance_list":
        q, hint = build_attendance_query(emp_code, date_from, date_to, status)
        cmd = {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT),
               "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}
        collection = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            pipeline, hint = build_employee_monthly(emp_code, month)
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            need(month=month)
            pipeline, hint = build_department_summary(month, department)
            collection = "employees"
        elif endpoint == "late_leaderboard":
            need(month=month)
            pipeline, hint = build_leaderboard(month, limit, department)
            collection = "attendance_logs"
        else:
            need(department=department, **{"from": frm}, to=to)
            d_from, d_to = validate_trend_range(frm, to)
            pipeline, hint = build_trend(department, d_from, d_to)
            collection = "employees"
        cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": _jsonable(result)}