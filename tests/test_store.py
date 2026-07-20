from video_jobs.models import Delivery, JobStatus, SubmitJob
from video_jobs.store import SqliteJobStore
from video_jobs.errors import DownloadError, classify
from video_jobs.providers import MassiveStrictProxy
from video_jobs.providers import YtDlpDownloader


def request() -> SubmitJob:
    return SubmitJob(url="https://www.youtube.com/watch?v=abc", delivery=Delivery(type="local"))


def test_idempotency_and_claim() -> None:
    store = SqliteJobStore(":memory:")
    first = store.create(request(), "same-request")
    assert store.create(request(), "same-request").id == first.id
    claimed = store.claim()
    assert claimed and claimed.id == first.id
    assert claimed.status == JobStatus.RUNNING


def test_recovers_unleased_running_job() -> None:
    store = SqliteJobStore(":memory:")
    job = store.create(request())
    store.claim()
    assert store.recover_stale() == 0
    store.db.execute("UPDATE jobs SET lease_expires_at = NULL WHERE id = ?", (job.id,))
    store.db.commit()
    assert store.recover_stale() == 1
    assert store.get(job.id).status == JobStatus.QUEUED


def test_permanent_download_error_does_not_retry() -> None:
    result = classify(DownloadError(1, "ERROR: Private video"))
    assert result.code == "video_private"
    assert not result.retryable


def test_bot_wall_with_typographic_apostrophe_is_terminal() -> None:
    result = classify(DownloadError(1, "Sign in to confirm you’re not a bot"))
    assert result.code == "bot_wall"
    assert not result.retryable


def test_proxy_url_encodes_credentials_and_adds_controls() -> None:
    proxy = MassiveStrictProxy("user name", "key:/?@", country="US", session_ttl=30)
    url = proxy.for_attempt("job-id", 1)
    assert "key%3A%2F%3F%40" in url
    assert "sessionttl-30-country-US" in url


def test_proxy_route_is_unique_per_attempt_and_strict() -> None:
    proxy = MassiveStrictProxy("user", "key")
    first = proxy.route_for_attempt("job-id", 1)
    second = proxy.route_for_attempt("job-id", 2)
    assert first.session_hash != second.session_hash
    assert "sessionmode-strict" in first.url


def test_proxy_503_is_retryable_capacity_failure() -> None:
    result = classify(DownloadError(1, "Tunnel connection failed: 503 Service Unavailable"))
    assert result.code == "proxy_capacity"
    assert result.retryable


def test_operator_can_requeue_terminal_failure() -> None:
    store = SqliteJobStore(":memory:")
    job = store.create(request())
    store.update(job.id, JobStatus.FAILED, error_code="proxy_capacity")
    retried = store.retry_terminal(job.id)
    assert retried and retried.status == JobStatus.QUEUED and retried.attempts == 0


def test_records_attempt_telemetry() -> None:
    store = SqliteJobStore(":memory:")
    job = store.create(request())
    attempt_id = store.start_attempt(job.id, 1, "sessionhash", "android_vr,web_embedded", {"ip": "203.0.113.42"})
    store.attempt_phase(attempt_id, "uploading", bytes_written=12)
    store.finish_attempt(attempt_id, "completed", bytes_written=12)
    attempt = store.attempts_for(job.id)[0]
    assert attempt["phase"] == "completed" and attempt["exit_ip"] == "203.0.113.42"
    assert attempt["elapsed_ms"] is not None and attempt["bytes"] == 12


def test_downloader_uses_yt_dlp_python_api(monkeypatch, tmp_path) -> None:
    import yt_dlp

    observed = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            observed.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, _url, download):
            assert download is True
            path = tmp_path / "abc.mp4"
            path.write_bytes(b"video")
            return {"id": "abc", "duration": 1, "width": 640, "height": 360,
                    "requested_downloads": [{"filepath": str(path)}]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    result = YtDlpDownloader().download("https://www.youtube.com/watch?v=abc", "best_progressive", tmp_path,
                                        "http://user:secret@proxy.example:65534")
    assert result.path.name == "abc.mp4"
    assert observed["proxy"] == "http://user:secret@proxy.example:65534"
    assert observed["extractor_args"]["youtube"]["player_client"] == ["android_vr", "web_embedded"]
