from __future__ import annotations

import os
from fastapi import FastAPI, Header, HTTPException, Response

from .config import load_dotenv
from .models import Job, SubmitJob
from .store import SqliteJobStore

load_dotenv()
store = SqliteJobStore(os.getenv("VIDEO_JOBS_DATABASE", "video-jobs.db"))
app = FastAPI(title="video-jobs", version="0.1.0")


@app.post("/video", response_model=Job, status_code=202)
def submit(request: SubmitJob, response: Response, idempotency_key: str | None = Header(default=None)) -> Job:
    if request.url.host not in {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}:
        raise HTTPException(422, "Only configured downloader providers are accepted in this deployment")
    job = store.create(request, idempotency_key)
    response.status_code = 200 if job.status != "queued" else 202
    return job


@app.get("/video/{job_id}", response_model=Job)
def status(job_id: str) -> Job:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return job


@app.post("/video/{job_id}/retry", response_model=Job, status_code=202)
def retry(job_id: str) -> Job:
    job = store.retry_terminal(job_id)
    if not job:
        existing = store.get(job_id)
        if not existing:
            raise HTTPException(404, "Unknown job")
        raise HTTPException(409, "Only failed jobs can be retried manually")
    return job


@app.get("/video/{job_id}/attempts")
def attempts(job_id: str) -> list[dict]:
    if not store.get(job_id):
        raise HTTPException(404, "Unknown job")
    return store.attempts_for(job_id)


def run() -> None:
    import uvicorn
    uvicorn.run("video_jobs.api:app", host="0.0.0.0", port=8000, reload=True)
