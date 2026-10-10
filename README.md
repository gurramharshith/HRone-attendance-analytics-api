# Employee Attendance & Analytics API

FastAPI service backed by MongoDB. API instants use epoch milliseconds; MongoDB stores UTC BSON dates. Required indexes are created at startup.

## Run

```bash
pip install -r requirements.txt
# Set MONGO_URI and MONGO_DB in the environment (or in a local .env).
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ is required. Set both `MONGO_URI` and `MONGO_DB` before startup.

The API creates its indexes during application startup. No credentials are committed; configure the MongoDB URI through the environment or a local, untracked `.env` file.

## Tests

Start a local MongoDB 6.0+ server, then run `python -m pytest -q`. The integration tests use `TEST_MONGO_URI` if set, otherwise `mongodb://127.0.0.1:27018`, and create/drop the `attendance_assignment_tests` database. They include concurrency, time/validation edge cases, analytics, pre-join months, 100,000-record explain-plan/index checks, and a MongoDB-unavailable health check.

The original assignment contract, seed script, and sample data remain in this working copy. The JSON sample data is ignored for submission because the assignment excludes data dumps. When using the original kit locally, set `MONGO_URI` and `MONGO_DB` and run `python sample_seed.py`; it clears and replaces those collections, so use a disposable database. The seed script and its JSON inputs are not needed to run the API and should not be included in the public submission.

## Verification scope and limitations

The final local audit used Python 3.11 and MongoDB 8.0.12: all 17 tests passed; the restored seed loaded 6 employees and 9 attendance records into a disposable database, and the sample monthly/trend checks passed. Uvicorn startup and `/health` returned HTTP 200. The restored `openapi.yaml`, `DATA_MODEL.md`, and sample data were directly inspected. The original `PROBLEM_STATEMENT.docx` and a separate assignment email were not present in the available project or attachment files; the pasted HROne assignment brief was available and reviewed. MongoDB 6.0 and 7.0 were not separately exercised. Hidden-dataset behavior is not guaranteed by local tests.

The supplied `sample_data/` files are retained locally and ignored by Git because the assignment excludes data dumps from submission. Commit the source, tests, and required documentation rather than those JSON dumps.

See [REVIEW.md](REVIEW.md) for the starter review and [DECISIONS.md](DECISIONS.md) for implementation choices.
