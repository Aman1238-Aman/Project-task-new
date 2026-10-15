
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)]
    )


def epoch_ms_to_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer in milliseconds")
    if not 100000000000 <= value <= 4102444800000:
        raise HTTPException(422, "Timestamp is outside the allowed range")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(
        microsecond=0
    )


def datetime_to_epoch_ms(value: Optional[datetime]):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local_time = punch_in.astimezone(IST)
    hour, minute = map(int, shift_start.split(":"))
    shift_start_time = local_time.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    elapsed = int((local_time - shift_start_time).total_seconds() // 60)
    return elapsed if elapsed > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return round(seconds / 3600, 2)


def compute_overtime(
    punch_out: datetime, shift_end: str, attendance_date: str
) -> int:
    hour, minute = map(int, shift_end.split(":"))
    day = date.fromisoformat(attendance_date)
    end = datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=IST
    )
    local_out = punch_out.astimezone(IST)
    elapsed = int((local_out - end).total_seconds() // 60)
    return elapsed if elapsed >= 30 else 0


    
class EmployeeIn(BaseModel):

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    shift_end: str = Field(
        default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    joined_on: date


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


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
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = datetime_to_epoch_ms(item["created_at"])

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(422, "Invalid punch-in status")

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        ts = epoch_ms_to_datetime(body.punched_at)

    local_ts = ts.astimezone(IST)
    shift_end_hour, shift_end_minute = map(int, emp["shift_end"].split(":"))
    shift_start_hour, shift_start_minute = map(int, emp["shift_start"].split(":"))

    overnight = emp["shift_end"] <= emp["shift_start"]

    if overnight and (
        local_ts.hour * 60 + local_ts.minute
        < shift_end_hour * 60 + shift_end_minute
    ):
        attendance_day = (local_ts.date() - timedelta(days=1)).isoformat()
    else:
        attendance_day = local_ts.date().isoformat()

    if attendance_day < emp["joined_on"]:
        raise HTTPException(422, "Punch-in date is before employee joining date")

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
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
        raise HTTPException(409, "Already punched in for this date")

    return {
        **{key: value for key, value in doc.items() if key != "_id"},
        "punch_in": datetime_to_epoch_ms(doc["punch_in"]),
        "punch_out": None,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if status is not None:
        allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
        if status not in allowed_statuses:
            raise HTTPException(422, "Invalid status")
        query["status"] = status

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()

    total = db.attendance_logs.count_documents(query)

    items = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        for field in ("punch_in", "punch_out"):
            if isinstance(item.get(field), datetime):
                item[field] = datetime_to_epoch_ms(item[field])

        for entry in item.get("history", []):
            if isinstance(entry.get("at"), datetime):
                entry["at"] = datetime_to_epoch_ms(entry["at"])
            for change in entry.get("changes", {}).values():
                for side in ("from", "to"):
                    if isinstance(change.get(side), datetime):
                        change[side] = datetime_to_epoch_ms(change[side])

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# TODO: Implement the remaining required endpoints:

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc)
    else:
        if isinstance(body.punched_at, bool):
            raise HTTPException(422, "Invalid timestamp")
        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000, tz=timezone.utc
            )
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, "Invalid timestamp")

    local_ts = ts.astimezone(IST)
    attendance_date = local_ts.date().isoformat()

    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": attendance_date,
    })

    if record is None:
        raise HTTPException(404, "Attendance record not found")

    if record.get("punch_in") is None:
        raise HTTPException(422, "Punch-in is required")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    if ts <= punch_in_time:
        raise HTTPException(422, "Punch-out must be after punch-in")

    if ts - punch_in_time > timedelta(hours=24):
        raise HTTPException(422, "Punch-out must be within 24 hours")

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime = compute_overtime(ts, emp["shift_end"], record["date"])

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed; retry")

    return {
        "emp_code": body.emp_code,
        "date": record["date"],
        "punch_out": int(ts.timestamp() * 1000),
        "work_hours": work_hours,
        "overtime_minutes": overtime,
    }

# POST  /attendance/punch-out
# PATCH /attendance/{emp_code}/{date}

class AttendancePatchIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    half_day: Optional[bool] = None
 


@app.patch("/attendance/{emp_code}/{attendance_date}")
def patch_attendance(
    emp_code: str,
    attendance_date: date,
    body: AttendancePatchIn,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": attendance_date.isoformat(),
    })
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(422, "No changes provided")

    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    new_status = updates.get("status", record["status"])

    if new_status not in allowed_statuses:
        raise HTTPException(422, "Invalid status")

    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in updates or "punch_out" in updates:
            raise HTTPException(
                422, "ABSENT or LEAVE cannot have punch times"
            )
        updates["punch_in"] = None
        updates["punch_out"] = None
        updates["work_hours"] = None
        updates["late_minutes"] = 0
        updates["overtime_minutes"] = 0
        updates["half_day"] = False
    else:
        punch_in = record.get("punch_in")
        punch_out = record.get("punch_out")

        if "punch_in" in updates:
            punch_in = (
                epoch_ms_to_datetime(updates["punch_in"])
                if updates["punch_in"] is not None
                else None
            )

        if "punch_out" in updates:
            punch_out = (
                epoch_ms_to_datetime(updates["punch_out"])
                if updates["punch_out"] is not None
                else None
            )

        if punch_in is None:
            raise HTTPException(422, "Punch-in is required")

        if punch_in.date().isoformat() != record["date"]:
            raise HTTPException(422, "Punch-in must be on attendance date")

        if punch_out is not None:
            if punch_out <= punch_in:
                raise HTTPException(
                    422, "Punch-out must be after punch-in"
                )
            if punch_out - punch_in > timedelta(hours=24):
                raise HTTPException(
                    422, "Punch-out must be within 24 hours"
                )
            updates["work_hours"] = compute_work_hours(
                punch_in, punch_out
            )
            updates["overtime_minutes"] = compute_overtime(
                punch_out, emp["shift_end"], record["date"]
            )
        else:
            updates["work_hours"] = None
            updates["overtime_minutes"] = 0

        updates["late_minutes"] = compute_late_minutes(
            punch_in, emp["shift_start"]
        )

    if "half_day" not in updates:
        updates["half_day"] = record.get("half_day", False)

    changes = {}
    for key, value in updates.items():
        old_value = record.get(key)
        if isinstance(old_value, datetime) and isinstance(value, datetime):
            same = old_value == value
        else:
            same = old_value == value

        if not same:
            changes[key] = {"from": old_value, "to": value}

    if not changes:
        raise HTTPException(422, "No changes detected")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
        },
        {
            "$set": updates,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed; retry")

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}, {"_id": 0}
    )

    for field in ("punch_in", "punch_out"):
        if isinstance(updated.get(field), datetime):
            updated[field] = datetime_to_epoch_ms(updated[field])

    for entry in updated.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = datetime_to_epoch_ms(entry["at"])
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = datetime_to_epoch_ms(change[side])

    return updated


# GET   /analytics/employees/{emp_code}/monthly

# GET   /analytics/departments/summary
# GET   /analytics/leaderboard/late
# GET   /analytics/departments/{department}/trend
# GET   /admin/explain/{endpoint}

class AttendancePatchIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None


@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(
    emp_code: str,
    date: date,
    body: AttendancePatchIn,
):
    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date.isoformat(),
    })
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    changes = {}
    updates = body.model_dump(exclude_unset=True)

    if not updates:
        raise HTTPException(422, "No changes supplied")

    if "status" in updates:
        if updates["status"] not in allowed_statuses:
            raise HTTPException(422, "Invalid status")

    for field in ("punch_in", "punch_out"):
        if field in updates and updates[field] is not None:
            updates[field] = epoch_ms_to_datetime(updates[field])

    if updates.get("status") in {"ABSENT", "LEAVE"}:
        if "punch_in" in updates or "punch_out" in updates:
            raise HTTPException(
                422, "ABSENT/LEAVE cannot include punch times"
            )
        updates["punch_in"] = None
        updates["punch_out"] = None

    new_record = {**record, **updates}

    if new_record.get("status") in {"PRESENT", "WFH", "ON_DUTY"}:
        if new_record.get("punch_in") is None:
            raise HTTPException(422, "Punch-in is required")

    pin = new_record.get("punch_in")
    pout = new_record.get("punch_out")

    if pin is not None and pout is not None:
        if pout <= pin:
            raise HTTPException(422, "Punch-out must be after punch-in")
        if pout - pin > timedelta(hours=24):
            raise HTTPException(422, "Punch-out must be within 24 hours")

    if pin is not None:
        new_record["late_minutes"] = compute_late_minutes(
            pin, emp["shift_start"]
        )
    else:
        new_record["late_minutes"] = 0

    if pin is not None and pout is not None:
        new_record["work_hours"] = compute_work_hours(pin, pout)
        new_record["overtime_minutes"] = compute_overtime(
            pout, emp["shift_end"], date.isoformat()
        )
    else:
        new_record["work_hours"] = None
        new_record["overtime_minutes"] = 0

    new_record["half_day"] = (
        new_record["work_hours"] is not None
        and new_record["work_hours"] < 4
    )

    for field in (
        "status", "punch_in", "punch_out", "late_minutes",
        "work_hours", "overtime_minutes", "half_day"
    ):
        if record.get(field) != new_record.get(field):
            changes[field] = {
                "from": record.get(field),
                "to": new_record.get(field),
            }

    if not changes:
        raise HTTPException(422, "No changes detected")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
        },
        {
            "$set": {
                field: new_record.get(field)
                for field in (
                    "status", "punch_in", "punch_out", "late_minutes",
                    "work_hours", "overtime_minutes", "half_day"
                )
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed; retry")

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}, {"_id": 0}
    )

    for field in ("punch_in", "punch_out"):
        if isinstance(updated.get(field), datetime):
            updated[field] = datetime_to_epoch_ms(updated[field])

    for entry in updated.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = datetime_to_epoch_ms(entry["at"])
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = datetime_to_epoch_ms(change[side])

    return updated