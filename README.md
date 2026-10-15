# Employee Attendance & Analytics API

FastAPI and MongoDB service for employee attendance, audited regularizations, and monthly/daily analytics. Requires Python 3.11+ and MongoDB 6.0+.

## Run locally

1. Start MongoDB locally, or create an Atlas database user and connection URI.
2. From this folder, create and activate a virtual environment: `python -m venv .venv` then `.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate` (macOS/Linux).
3. Install dependencies: `python -m pip install -r requirements.txt`.
4. Set `MONGO_URI` and `MONGO_DB` in your shell. For a local server, use `MONGO_URI=mongodb://127.0.0.1:27017` and `MONGO_DB=attendance_db`. Shell environment variables take precedence; a local `.env` is also supported and must not be committed.
5. Start the API: `uvicorn app.main:app --port 8000 --reload`.

Open `http://127.0.0.1:8000/docs`; `GET /health` checks the database connection. Indexes are created automatically at startup. The candidate kit's sample seed script can populate a disposable local database if you choose to use the sample data locally.

`candidate_kit_reference/` contains the original assignment kit for local reference, including its problem statement and sample data. It is intentionally excluded from Git; only the implementation and submission documents at the project root belong in the public submission.

## Included endpoints

Employee management, attendance listing and punches, audited manual corrections, employee and department monthly analytics, late leaderboard, department daily trend, and MongoDB explain diagnostics. Analytics are computed in MongoDB aggregation pipelines.

Before submitting, remove any local `.env`, virtual environment, and private connection details from the Git working tree. Do not publish the candidate problem statement or sample data.
