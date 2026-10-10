from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

MONGO_URI = os.getenv("TEST_MONGO_URI", "mongodb://127.0.0.1:27018")
os.environ.setdefault("MONGO_URI", MONGO_URI)
os.environ.setdefault("MONGO_DB", "attendance_assignment_tests")

from app import main


client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500, tz_aware=True, tzinfo=timezone.utc)
try:
    client.admin.command("ping")
except ServerSelectionTimeoutError:
    pytest.skip("integration tests require MongoDB 6+ at TEST_MONGO_URI", allow_module_level=True)
main.client = client
main.db = client["attendance_assignment_tests"]


@pytest.fixture(scope="module")
def api():
    with TestClient(main.app) as test_client:
        yield test_client
    client.drop_database("attendance_assignment_tests")


@pytest.fixture(autouse=True)
def clean_db():
    main.db.employees.delete_many({})
    main.db.attendance_logs.delete_many({})


def epoch(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def employee(code, department="Engineering", joined="2026-07-01", start="09:30", end="18:30"):
    return {"emp_code": code, "name": code, "email": f"{code.lower()}@example.com", "department": department,
            "joined_on": joined, "shift_start": start, "shift_end": end}


def log(code, day, status="PRESENT", late=0, hours=8.0, half=False, overtime=0):
    main.db.attendance_logs.insert_one({"emp_code": code, "date": day, "status": status, "punch_in": None,
        "punch_out": None, "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
        "half_day": half, "history": []})


def test_time_rules_half_up_and_overnight():
    start = datetime(2026, 7, 6, 9, 30, tzinfo=main.IST)
    assert main.compute_late_minutes(start + timedelta(minutes=10), "09:30", "18:30") == 0
    assert main.compute_late_minutes(start + timedelta(minutes=10, seconds=1), "09:30", "18:30") == 10
    assert main.compute_work_hours(start, start + timedelta(seconds=18)) == 0.01
    assert main.compute_work_hours(start, start + timedelta(seconds=16199)) == 4.5
    assert main.compute_overtime(datetime(2026, 7, 6, 18, 59, 59, tzinfo=main.IST), "18:30", "2026-07-06", "09:30") == 0
    assert main.compute_overtime(datetime(2026, 7, 6, 19, 0, tzinfo=main.IST), "18:30", "2026-07-06", "09:30") == 30
    before_midnight = datetime(2026, 7, 7, 5, 0, tzinfo=main.IST)
    assert main._shift_anchor(before_midnight, "22:00", "06:00")[0].date().isoformat() == "2026-07-06"
    assert main.compute_late_minutes(before_midnight, "22:00", "06:00") == 420
    assert main.compute_overtime(datetime(2026, 7, 7, 6, 40, tzinfo=main.IST), "06:00", "2026-07-06", "22:00") == 40


def test_validation_and_employee_serialization(api):
    bad = employee("E1")
    assert api.post("/employees", json=bad).status_code == 422
    assert api.post("/employees", json=employee("EMP0006", joined="2026-02-30")).status_code == 422
    equal_shift = employee("EMP0007", start="09:30", end="09:30")
    assert api.post("/employees", json=equal_shift).status_code == 422
    good = employee("EMP0001")
    response = api.post("/employees", json=good)
    assert response.status_code == 201
    assert isinstance(response.json()["created_at"], int)
    assert "_id" not in response.json()
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1.75}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "1700000000000"}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": True}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1_700_000_000}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 4_102_444_800_001}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "LEAVE"}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "MISSING"}).status_code == 404


def test_missing_resources_and_health_database_failure(api, monkeypatch):
    assert api.post("/attendance/punch-out", json={"emp_code": "UNKNOWN"}).status_code == 404
    assert api.get("/analytics/employees/UNKNOWN/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/departments/Unknown/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert api.patch("/attendance/UNKNOWN/2026-07-01", json={"reason": "not found", "regularized_by": "hr"}).status_code == 404

    def fail_ping(*_args, **_kwargs):
        raise ServerSelectionTimeoutError("test database unavailable")

    monkeypatch.setattr(main, "client", SimpleNamespace(admin=SimpleNamespace(command=fail_ping)))
    assert api.get("/health").status_code == 503


def test_overnight_punch_date_and_second_truncation(api):
    api.post("/employees", json=employee("EMP0003", start="22:00", end="06:00"))
    instant = epoch("2026-07-06T23:30:00") + 987  # 05:00 IST the following calendar day
    response = api.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": instant})
    assert response.status_code == 201
    record = response.json()
    assert record["date"] == "2026-07-06"
    assert record["punch_in"] == instant - 987
    assert record["late_minutes"] == 420


def test_list_pagination_filtered_totals_and_sort(api):
    api.post("/employees", json=employee("EMP0501", "A"))
    api.post("/employees", json=employee("EMP0502", "A"))
    api.post("/employees", json=employee("EMP0503", "B"))
    for code, day in (("EMP0501", "2026-07-06"), ("EMP0502", "2026-07-07"), ("EMP0503", "2026-07-08")):
        log(code, day)
    employees = api.get("/employees", params={"department": "A", "page": 2, "page_size": 1}).json()
    assert employees["total"] == 2 and [x["emp_code"] for x in employees["items"]] == ["EMP0502"]
    records = api.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-08", "page": 2, "page_size": 1}).json()
    assert records["total"] == 3
    assert [(r["date"], r["emp_code"]) for r in records["items"]] == [("2026-07-07", "EMP0502")]
    assert api.get("/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-02-30"}).status_code == 422
    assert api.get("/attendance", params={"page_size": 101}).status_code == 422


def test_punch_in_out_concurrency(api):
    api.post("/employees", json=employee("EMP0001"))
    body = {"emp_code": "EMP0001", "punched_at": epoch("2026-07-06T04:00:00")}
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: api.post("/attendance/punch-in", json=body), range(8)))
    assert sorted(r.status_code for r in responses) == [201] + [409] * 7
    punchout = {"emp_code": "EMP0001", "punched_at": epoch("2026-07-06T13:30:00")}
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: api.post("/attendance/punch-out", json=punchout), range(8)))
    assert sorted(r.status_code for r in responses) == [200] + [409] * 7
    result = next(r.json() for r in responses if r.status_code == 200)
    assert result["work_hours"] == 9.5
    assert result["overtime_minutes"] == 30
    assert result["history"] == []


def test_employee_creation_concurrency(api):
    body = employee("EMP0005")
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: api.post("/employees", json=body), range(8)))
    assert sorted(response.status_code for response in responses) == [201] + [409] * 7


def test_regularization_audit_and_conflict_safety(api):
    api.post("/employees", json=employee("EMP0002"))
    created = api.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": epoch("2026-07-06T04:30:00")})
    assert created.status_code == 201
    main.db.attendance_logs.update_one({"emp_code": "EMP0002"}, {"$unset": {"history": ""}})
    body_a = {"punch_in": epoch("2026-07-06T04:00:00"), "reason": "corrected time", "regularized_by": "hr.one"}
    body_b = {"punch_in": epoch("2026-07-06T04:10:00"), "reason": "another correction", "regularized_by": "hr.two"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda body: api.patch("/attendance/EMP0002/2026-07-06", json=body), [body_a, body_b]))
    assert sorted(r.status_code for r in responses) in ([200, 409], [200, 200])
    record = api.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"][0]
    successful = sum(r.status_code == 200 for r in responses)
    assert len(record["history"]) == successful
    assert all("punch_in" in event["changes"] for event in record["history"])
    assert api.patch("/attendance/EMP0002/2026-07-06", json={"punch_in": None, "reason": "bad null", "regularized_by": "hr"}).status_code == 422


def test_regularization_absent_clears_times_and_noop_is_rejected(api):
    api.post("/employees", json=employee("EMP0004"))
    created = api.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": epoch("2026-07-06T04:00:00")})
    assert created.status_code == 201
    response = api.patch("/attendance/EMP0004/2026-07-06", json={"status": "ABSENT", "reason": "mark absent", "regularized_by": "hr"})
    assert response.status_code == 200
    row = response.json()
    assert row["punch_in"] is None and row["punch_out"] is None and row["late_minutes"] == 0
    assert set(row["history"][-1]["changes"]) == {"status", "punch_in"}
    assert api.patch("/attendance/EMP0004/2026-07-06", json={"reason": "no change", "regularized_by": "hr"}).status_code == 422


def test_monthly_summary_join_date_weekends_and_legacy_defaults(api):
    api.post("/employees", json=employee("EMP0010", joined="2026-07-15"))
    log("EMP0010", "2026-07-15", late=12, hours=8.25)
    log("EMP0010", "2026-07-18", late=30, hours=4.0, half=True, overtime=5)
    main.db.attendance_logs.update_one({"emp_code": "EMP0010", "date": "2026-07-15"}, {"$unset": {"half_day": ""}})
    summary = api.get("/analytics/employees/EMP0010/monthly", params={"month": "2026-07"})
    assert summary.status_code == 200
    assert summary.json() == {"emp_code": "EMP0010", "month": "2026-07", "working_days": 13, "present_days": 1.0,
        "leave_days": 0, "late_count": 2, "total_late_minutes": 42, "total_overtime_minutes": 5, "attendance_pct": 7.6923}
    listed = api.get("/attendance", params={"emp_code": "EMP0010", "date_from": "2026-07-15", "date_to": "2026-07-15"}).json()["items"][0]
    assert listed["half_day"] is False and listed["history"] == []
    assert api.get("/analytics/employees/EMP0010/monthly", params={"month": "0000-01"}).status_code == 422


def test_month_before_join_date_has_zero_working_days(api):
    api.post("/employees", json=employee("EMP0999", joined="2026-07-13"))
    response = api.get("/analytics/employees/EMP0999/monthly", params={"month": "2026-06"})
    assert response.status_code == 200
    result = response.json()
    assert result["working_days"] == 0
    assert result["present_days"] == 0
    assert result["attendance_pct"] is None


def test_department_headcount_no_logs_and_average_over_records(api):
    api.post("/employees", json=employee("EMP0100", "Design"))
    api.post("/employees", json=employee("EMP0101", "Design"))
    api.post("/employees", json=employee("EMP0103", "Design"))
    api.post("/employees", json=employee("EMP0102", "Design", joined="2026-08-01"))
    log("EMP0100", "2026-07-06", hours=8.0, late=10)
    log("EMP0100", "2026-07-07", hours=8.01, late=0)
    log("EMP0101", "2026-07-06", status="LEAVE", hours=None)
    rows = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert rows == [{"department": "Design", "headcount": 3, "present_days": 2.0, "avg_work_hours": 8.01,
        "late_count": 1, "total_late_minutes": 10, "leave_count": 1, "on_duty_count": 0}]


def test_leaderboard_competition_tie_at_limit(api):
    for code in ("EMP0201", "EMP0202", "EMP0203", "EMP0204"):
        api.post("/employees", json=employee(code, "Ops"))
    for code, mins in (("EMP0201", 30), ("EMP0202", 20), ("EMP0203", 20), ("EMP0204", 5)):
        log(code, "2026-07-06", late=mins)
    rows = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in rows] == [
        (1, "EMP0201", 30), (2, "EMP0202", 20), (2, "EMP0203", 20)]


def test_trend_full_date_spine_headcount_and_moving_average(api):
    api.post("/employees", json=employee("EMP0301", "Support", joined="2026-07-07"))
    log("EMP0301", "2026-07-07", late=5, hours=4.0, half=True)
    log("EMP0301", "2026-07-08", late=0, hours=8.0)
    response = api.get("/analytics/departments/Support/trend", params={"from": "2026-07-06", "to": "2026-07-12"})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 7
    assert [row["date"] for row in items] == [f"2026-07-{day:02d}" for day in range(6, 13)]
    assert items[0]["headcount"] == 0 and items[0]["attendance_rate"] is None
    assert items[1]["headcount"] == 1 and items[1]["present_count"] == 0.5 and items[1]["attendance_rate"] == 0.5
    assert items[2]["attendance_rate"] == 1.0
    assert items[6]["moving_avg_7d"] == 0.375
    assert api.get("/analytics/departments/Support/trend", params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422


def test_explain_queries_use_indexes(api):
    api.post("/employees", json=employee("EMP0401", "Explain"))
    log("EMP0401", "2026-07-06", late=22)
    args = {
        "attendance_list": {"emp_code": "EMP0401", "date_from": "2026-07-01", "date_to": "2026-07-31"},
        "employee_monthly": {"emp_code": "EMP0401", "month": "2026-07"},
        "department_summary": {"month": "2026-07", "department": "Explain"},
        "late_leaderboard": {"month": "2026-07", "limit": 1},
        "department_trend": {"department": "Explain", "from": "2026-07-06", "to": "2026-07-12"},
    }
    for endpoint, params in args.items():
        response = api.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 200, (endpoint, response.text)
        serialized = str(response.json()["explain"])
        assert "IXSCAN" in serialized, (endpoint, serialized)
        assert "COLLSCAN" not in serialized, (endpoint, serialized)


def test_indexes_created_at_startup(api):
    employee_indexes = main.db.employees.index_information()
    attendance_indexes = main.db.attendance_logs.index_information()
    assert employee_indexes["uq_emp_code"]["unique"] is True
    assert attendance_indexes["uq_emp_date"]["unique"] is True
    assert "attendance_date_late_emp" in attendance_indexes
    assert "/attendance/{emp_code}/{date}" in [route.path for route in main.app.routes]


def test_indexed_list_and_monthly_queries_on_100k_logs(api):
    people = [employee(f"EMP{1000 + i:04d}", "Scale", "2020-01-01") for i in range(100)]
    main.db.employees.insert_many(people)
    first = datetime(2023, 1, 1).date()
    batch = []
    for person in people:
        for offset in range(1000):
            day = (first + timedelta(days=offset)).isoformat()
            batch.append({"emp_code": person["emp_code"], "date": day, "status": "PRESENT", "punch_in": None,
                "punch_out": None, "work_hours": 8.0, "late_minutes": offset % 60, "overtime_minutes": 0, "half_day": False})
        if len(batch) >= 5000:
            main.db.attendance_logs.insert_many(batch)
            batch.clear()
    if batch:
        main.db.attendance_logs.insert_many(batch)

    page = api.get("/attendance", params={"emp_code": "EMP1000", "page_size": 20})
    assert page.status_code == 200 and page.json()["total"] == 1000 and len(page.json()["items"]) == 20
    monthly = api.get("/analytics/employees/EMP1000/monthly", params={"month": "2023-01"})
    assert monthly.status_code == 200 and monthly.json()["working_days"] == 22
    department = api.get("/analytics/departments/summary", params={"month": "2023-01", "department": "Scale"})
    assert department.status_code == 200 and department.json()["items"][0]["headcount"] == 100
    leaderboard = api.get("/analytics/leaderboard/late", params={"month": "2023-01", "department": "Scale"})
    assert leaderboard.status_code == 200
    trend = api.get("/analytics/departments/Scale/trend", params={"from": "2023-01-01", "to": "2023-01-07"})
    assert trend.status_code == 200 and len(trend.json()["items"]) == 7

    explain_args = {
        "attendance_list": {"emp_code": "EMP1000", "page_size": 20},
        "employee_monthly": {"emp_code": "EMP1000", "month": "2023-01"},
        "department_summary": {"month": "2023-01", "department": "Scale"},
        "late_leaderboard": {"month": "2023-01", "department": "Scale"},
        "department_trend": {"department": "Scale", "from": "2023-01-01", "to": "2023-01-07"},
    }
    for endpoint, params in explain_args.items():
        explain = api.get(f"/admin/explain/{endpoint}", params=params)
        assert explain.status_code == 200, (endpoint, explain.text)
        plan = str(explain.json()["explain"])
        assert "IXSCAN" in plan and "COLLSCAN" not in plan, (endpoint, plan[:1000])
