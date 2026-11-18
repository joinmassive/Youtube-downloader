from fastapi.testclient import TestClient

from video_jobs import api
from video_jobs.models import Delivery, JobStatus, SubmitJob
from video_jobs.store import SqliteJobStore


def payload() -> dict:
    return {
        "url": "https://www.youtube.com/watch?v=abc123",
        "profile": "best_progressive",
        "delivery": {"type": "local", "key_prefix": "test/"},
    }


def test_submit_poll_and_idempotency(monkeypatch) -> None:
    monkeypatch.setattr(api, "store", SqliteJobStore(":memory:"))
    client = TestClient(api.app)

    first = client.post("/video", json=payload(), headers={"Idempotency-Key": "api-test"})
    second = client.post("/video", json=payload(), headers={"Idempotency-Key": "api-test"})

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert client.get(f"/video/{first.json()['id']}").json()["status"] == "queued"


def test_rejects_non_youtube_url(monkeypatch) -> None:
    monkeypatch.setattr(api, "store", SqliteJobStore(":memory:"))
    response = TestClient(api.app).post("/video", json={**payload(), "url": "https://example.com/video"})
    assert response.status_code == 422


def test_retry_endpoint_resets_terminal_job(monkeypatch) -> None:
    store = SqliteJobStore(":memory:")
    monkeypatch.setattr(api, "store", store)
    job = store.create(SubmitJob.model_validate(payload()))
    store.update(job.id, JobStatus.FAILED, error_code="proxy_capacity")

    response = TestClient(api.app).post(f"/video/{job.id}/retry")
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["attempts"] == 0


def test_one_hundred_jobs_are_bounded_to_thirty_claims() -> None:
    store = SqliteJobStore(":memory:")
    request = SubmitJob(url="https://www.youtube.com/watch?v=abc123", delivery=Delivery(type="local"))
    jobs = [store.create(request) for _ in range(100)]
    claimed = [store.claim() for _ in range(30)]

    assert all(job is not None and job.status == JobStatus.RUNNING for job in claimed)
    assert sum(store.get(job.id).status == JobStatus.RUNNING for job in jobs) == 30
    assert sum(store.get(job.id).status == JobStatus.QUEUED for job in jobs) == 70


def test_webhook_failure_does_not_undo_completed_job(monkeypatch, tmp_path) -> None:
    from video_jobs import worker
    from video_jobs.providers import DownloadedFile

    store = SqliteJobStore(":memory:")
    monkeypatch.setattr(worker, "store", store)
    monkeypatch.setenv("VIDEO_JOBS_LOCAL_STORAGE", str(tmp_path / "artifacts"))
    for name in ("PROXY_USER", "PROXY_PASS", "MASSIVE_PROXY_USERNAME", "MASSIVE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def fake_download(self, url, profile, target_dir, proxy, on_output=None):
        path = target_dir / "abc123.mp4"
        path.write_bytes(b"video")
        return DownloadedFile(path=path, video_id="abc123")

    def broken_webhook(job):
        raise TimeoutError("webhook timed out")

    monkeypatch.setattr(worker.YtDlpDownloader, "download", fake_download)
    monkeypatch.setattr(worker, "deliver_webhook", broken_webhook)

    store.create(SubmitJob.model_validate({**payload(), "webhook_url": "https://example.com/hook"}))
    job = store.claim()
    worker.process(job)

    assert store.get(job.id).status == JobStatus.COMPLETED
    assert store.attempts_for(job.id)[0]["phase"] == "completed"
