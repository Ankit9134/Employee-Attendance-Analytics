# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

# Employee Attendance & Analytics API

FastAPI + MongoDB attendance system.

## Run

```bash
python -m venv .venv && .venv\Scripts\Activate.ps1   # Windows
pip install -r requirements.txt
cp .env.example .env   # set MONGO_URI and MONGO_DB
python sample_seed.py
uvicorn app.main:app --port 8000
```

## Environment

```
MONGO_URI=mongodb+srv://<user>:<password>@cluster.mongodb.net/
MONGO_DB=employe
```
MongoDB 6.0+ required. All indexes are created at startup automatically.

## What is implemented

- All 12 endpoints from the contract
- Business rules R1–R10
- Concurrency safety via unique indexes + atomic updates
- MongoDB aggregation pipelines for all analytics endpoints
- `$densify` + `$setWindowFields` for department trend gap-filling and moving average
- `$rank` window function for leaderboard ties

## Notes
Nothing is missing. All endpoints are implemented and tested against the sample data.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
