"""Employee Attendance & Analytics API (single-file assignment implementation)."""
from __future__ import annotations

import os
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
client = MongoClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
MIN_TS, MAX_TS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="employee_date_unique")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="employee_latest_punch")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_employee_sort")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="status_date")


def epoch_ms_to_datetime(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_TS <= value <= MAX_TS:
        raise HTTPException(422, "Timestamp must be an integer in milliseconds within the supported range")
    return datetime.fromtimestamp(value // 1000, UTC)


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def epoch(value: datetime | None) -> int | None:
    value = utc(value)
    return int(value.timestamp() * 1000) if value else None


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def as_api_record(record: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in record.items() if key != "_id"}
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    for field in ("punch_in", "punch_out"):
        result[field] = epoch(result.get(field))
    for entry in result["history"]:
        entry["at"] = epoch(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = epoch(change[side])
    return result


def shift_instant(day: str, hhmm: str, overnight_end: bool = False) -> datetime:
    d = date.fromisoformat(day)
    h, m = map(int, hhmm.split(":"))
    if overnight_end:
        d += timedelta(days=1)
    return datetime.combine(d, time(h, m), IST).astimezone(UTC)


def attendance_day(punch: datetime, employee: dict[str, Any]) -> str:
    local = punch.astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"]:
        end_h, end_m = map(int, employee["shift_end"].split(":"))
        if (local.hour, local.minute) < (end_h, end_m):
            day -= timedelta(days=1)
    return day.isoformat()


def late_minutes(punch: datetime, start: str, day: str) -> int:
    elapsed = int((punch - shift_instant(day, start)).total_seconds() // 60)
    return elapsed if (punch - shift_instant(day, start)).total_seconds() > 600 else 0


def derived(punch_in: datetime | None, punch_out: datetime | None, employee: dict[str, Any], day: str) -> dict[str, Any]:
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(punch_in, employee["shift_start"], day)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = round_half_up(Decimal(str((punch_out - punch_in).total_seconds())) / Decimal(3600))
    end = shift_instant(day, employee["shift_end"], employee["shift_end"] <= employee["shift_start"])
    overtime_elapsed = int((punch_out - end).total_seconds() // 60)
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime_elapsed if overtime_elapsed >= 30 else 0, "half_day": hours < 4.5}


def month_bounds(month: str) -> tuple[date, date, int]:
    try:
        first = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")
    if len(month) != 7:
        raise HTTPException(422, "month must be YYYY-MM")
    last = date(first.year, first.month, monthrange(first.year, first.month)[1])
    weekdays = sum(1 for offset in range((last - first).days + 1) if (first + timedelta(days=offset)).weekday() < 5)
    return first, last, weekdays


def present_expr():
    return {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}


def round_expr(expr: Any, places: int = 2) -> dict[str, Any]:
    """Decimal-style half-up using integer scaling (Mongo $round uses ties-to-even)."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, 0.5]}}, scale]}


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: StrictInt | None = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: StrictInt | None = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = epoch(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        item["created_at"] = epoch(item.get("created_at"))
    return {"items": items, "total": db.employees.count_documents(query), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in PRESENCE:
        raise HTTPException(422, "Invalid punch-in status")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = datetime.now(UTC).replace(microsecond=0) if body.punched_at is None else epoch_ms_to_datetime(body.punched_at)
    day = attendance_day(ts, emp)
    if day < emp["joined_on"]:
        raise HTTPException(422, "Punch-in date is before employee joining date")
    record = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None, **derived(ts, None, emp, day), "history": []}
    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
    return as_api_record(record)


def attendance_query(emp_code: str | None, date_from: date | None, date_to: date | None, status: str | None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(422, "Invalid status")
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    return query


def attendance_sort():
    return [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: date | None = None, date_to: date | None = None, status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")
    query = attendance_query(emp_code, date_from, date_to, status)
    docs = db.attendance_logs.find(query, {"_id": 0}).sort(attendance_sort()).skip((page - 1) * page_size).limit(page_size)
    return {"items": [as_api_record(x) for x in docs], "total": db.attendance_logs.count_documents(query), "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = datetime.now(UTC).replace(microsecond=0) if body.punched_at is None else epoch_ms_to_datetime(body.punched_at)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not record:
        future_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": ts}},
            sort=[("punch_in", ASCENDING)],
        )
        if future_record is not None:
            raise HTTPException(422, "Punch-out must be after punch-in")
        raise HTTPException(404, "Punch-in record not found")
    pin = utc(record.get("punch_in"))
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    if ts <= pin:
        raise HTTPException(422, "Punch-out must be after punch-in")
    if ts - pin > timedelta(hours=24):
        raise HTTPException(422, "Punch-out must be within 24 hours")
    values = derived(pin, ts, emp, record["date"])
    result = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": {"punch_out": ts, **values}})
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed; retry")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return as_api_record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    day = date.isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not record:
        raise HTTPException(404, "Attendance record not found")
    supplied = body.model_fields_set & {"status", "punch_in", "punch_out"}
    if not supplied:
        raise HTTPException(422, "No attendance changes supplied")
    status = body.status if "status" in supplied else record["status"]
    if status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    if status in {"ABSENT", "LEAVE"} and supplied & {"punch_in", "punch_out"}:
        raise HTTPException(422, "ABSENT or LEAVE cannot be supplied with punch times")
    pin = epoch_ms_to_datetime(body.punch_in) if "punch_in" in supplied and body.punch_in is not None else (None if "punch_in" in supplied else utc(record.get("punch_in")))
    pout = epoch_ms_to_datetime(body.punch_out) if "punch_out" in supplied and body.punch_out is not None else (None if "punch_out" in supplied else utc(record.get("punch_out")))
    if status in {"ABSENT", "LEAVE"}:
        pin = pout = None
    else:
        if pin is None:
            raise HTTPException(422, "Presence status requires punch_in")
        if attendance_day(pin, emp) != day:
            raise HTTPException(422, "punch_in must belong to attendance date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    values = {"status": status, "punch_in": pin, "punch_out": pout, **derived(pin, pout, emp, day)}
    changes: dict[str, Any] = {}
    for key, new in values.items():
        if key in ("punch_in", "punch_out"):
            old = utc(record.get(key))
        elif key == "late_minutes":
            old = record.get(key, 0)
        elif key == "overtime_minutes":
            old = record.get(key, 0)
        elif key == "half_day":
            old = record.get(key, False)
        else:
            old = record.get(key)
        if old != new:
            changes[key] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(422, "No changes detected")
    history = record.get("history", [])
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    condition: dict[str, Any] = {"_id": record["_id"]}
    condition["history"] = history if "history" in record else {"$exists": False}
    result = db.attendance_logs.update_one(condition, {"$set": values, "$push": {"history": entry}})
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed; retry")
    return as_api_record(db.attendance_logs.find_one({"_id": record["_id"]}))


def employee_month_pipeline(emp_code: str, first: date, last: date):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [present_expr(), {"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
        {"$project": {"_id": 0, "present_days": 1, "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    if not db.employees.find_one({"emp_code": emp_code}, {"_id": 1}):
        raise HTTPException(404, "Employee not found")
    first, last, weekdays = month_bounds(month)
    data = next(iter(db.attendance_logs.aggregate(employee_month_pipeline(emp_code, first, last))), {})
    working = sum(1 for offset in range((last - first).days + 1) if (first + timedelta(days=offset)) >= date.fromisoformat(db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})["joined_on"]) and (first + timedelta(days=offset)).weekday() < 5)
    pct = round_half_up(Decimal(str(data.get("present_days", 0))) / Decimal(working) * 100) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": round_half_up(data.get("present_days", 0)), "leave_days": data.get("leave_days", 0), "late_count": data.get("late_count", 0), "total_late_minutes": data.get("total_late_minutes", 0), "total_overtime_minutes": data.get("total_overtime_minutes", 0), "attendance_pct": pct}


def department_summary_pipeline(first: date, last: date, department: str | None):
    match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]}, {"$lte": ["$date", last.isoformat()]}]}}},
            {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$gte": ["$logs._weekday", 2]}, {"$lte": ["$logs._weekday", 6]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": {"$round": ["$present_days", 2]},
            "avg_work_hours": {"$cond": [{"$gt": [{"$size": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, 0]}, {"$avg": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None):
    first, last, _ = month_bounds(month)
    items = list(db.employees.aggregate(department_summary_pipeline(first, last, department)))
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"])
        item["present_days"] = round_half_up(item["present_days"])
    return {"month": month, "items": items}


def leaderboard_pipeline(first: date, last: date, department: str | None):
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: str | None = None):
    first, last, _ = month_bounds(month)
    pipeline = leaderboard_pipeline(first, last, department)
    pipeline.append({"$match": {"rank": {"$lte": limit}}})
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


def trend_pipeline(department: str, first: date, last: date):
    start_dt = datetime.combine(first, time.min, UTC)
    end_dt = datetime.combine(last, time.min, UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "anchor": {"$min": {"$literal": start_dt}}}},
        {"$project": {"_id": 0, "date": "$anchor"}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
        {"$set": {"date_key": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date_key"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "weekday": {"$dayOfWeek": "$date"}}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date_key"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$match": {"e.department": department}},
            {"$group": {"_id": None, "present": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}}, "late": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late", 0]}, 0]}, "is_working_day": {"$and": [{"$gte": ["$weekday", 2]}, {"$lte": ["$weekday", 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, round_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"_avg7": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_key", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$_avg7", None]}, None, round_expr("$_avg7", 4)]}}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: date = Query(alias="from"), to: date = Query()):
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "Department not found")
    if to < from_:
        raise HTTPException(422, "to cannot be before from")
    if (to - from_).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")
    rows = list(db.employees.aggregate(trend_pipeline(department, from_, to), allowDiskUse=True))
    for row in rows:
        for key in ("attendance_rate", "moving_avg_7d"):
            if row[key] is not None:
                row[key] = round_half_up(row[key], 4)
    return {"department": department, "items": rows}


def explain_for(endpoint: str, p: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if endpoint == "attendance_list":
        page, size = int(p.get("page", 1)), int(p.get("page_size", 20))
        q = attendance_query(p.get("emp_code"), date.fromisoformat(p["date_from"]) if p.get("date_from") else None, date.fromisoformat(p["date_to"]) if p.get("date_to") else None, p.get("status"))
        return "attendance_logs", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * size, "limit": size}
    if endpoint == "employee_monthly":
        if not p.get("emp_code") or not p.get("month"):
            raise HTTPException(422, "emp_code and month are required")
        first, last, _ = month_bounds(p["month"])
        return "attendance_logs", {"aggregate": "attendance_logs", "pipeline": employee_month_pipeline(p["emp_code"], first, last), "cursor": {}}
    if endpoint == "department_summary":
        if not p.get("month"):
            raise HTTPException(422, "month is required")
        first, last, _ = month_bounds(p["month"])
        return "employees", {"aggregate": "employees", "pipeline": department_summary_pipeline(first, last, p.get("department")), "cursor": {}, "allowDiskUse": True}
    if endpoint == "late_leaderboard":
        if not p.get("month"):
            raise HTTPException(422, "month is required")
        first, last, _ = month_bounds(p["month"])
        pipeline = leaderboard_pipeline(first, last, p.get("department")) + [{"$match": {"rank": {"$lte": int(p.get("limit", 10))}}}]
        return "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
    if endpoint == "department_trend":
        if not all(p.get(k) for k in ("department", "from", "to")):
            raise HTTPException(422, "department, from and to are required")
        start, end = date.fromisoformat(p["from"]), date.fromisoformat(p["to"])
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "Trend range must be ordered and at most 92 days")
        return "employees", {"aggregate": "employees", "pipeline": trend_pipeline(p["department"], start, end), "cursor": {}, "allowDiskUse": True}
    raise HTTPException(422, "Unknown endpoint")


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, emp_code: str | None = None, month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None, limit: int = Query(10, ge=1, le=50), date_from: date | None = None, date_to: date | None = None, status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), from_: date | None = Query(None, alias="from"), to: date | None = None):
    p = {"emp_code": emp_code, "month": month, "department": department, "limit": limit, "date_from": date_from.isoformat() if date_from else None, "date_to": date_to.isoformat() if date_to else None, "status": status, "page": page, "page_size": page_size, "from": from_.isoformat() if from_ else None, "to": to.isoformat() if to else None}
    collection, command = explain_for(endpoint, p)
    if collection == "attendance_logs" and endpoint == "attendance_list":
        result = db.command({"explain": {"find": collection, "filter": command["filter"], "sort": command["sort"], "skip": command["skip"], "limit": command["limit"]}, "verbosity": "executionStats"})
    else:
        result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json_util.loads(json_util.dumps(result))}
