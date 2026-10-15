# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Indexes are created automatically at startup (idempotent). Needs MongoDB 6.0+ (`$setWindowFields`, `$lookup` with `localField` + `pipeline`).

## Implemented
Every endpoint in `openapi.yaml`: health, employees, punch-in/out, attendance list, PATCH regularization with audit history,
the four analytics aggregations, and `/admin/explain/{endpoint}`.

## Notes
- Instants are stored as UTC datetimes, truncated to whole seconds, and exchanged as epoch milliseconds.
- The department trend generates the daily rows with `$range` + `$unwind` instead of `$densify` (same result, no dependence on input documents existing).
- Rates in the trend are computed as integers (units of 1e-4) inside the pipeline so rounding is exactly half-up.

## Not done / known gaps
(fill in honestly after you have tested)
