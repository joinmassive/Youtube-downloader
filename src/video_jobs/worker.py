from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import tempfile
import time
import threading
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

from .api import store
from .config import load_dotenv
from .models import Job, JobStatus
from .providers import LocalStorage, S3Storage, YtDlpDownloader, massive_from_environment
from . import terminal
from .errors import classify


class StartPacer:
    """Stagger starts while allowing active downloads to fill worker slots."""
    def __init__(self, starts_per_second: float) -> None:
        self.interval = 1 / starts_per_second if starts_per_second > 0 else 0
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            scheduled = max(now, self.next_start)
            self.next_start = scheduled + self.interval
        time.sleep(max(0, scheduled - now))


start_pacer: StartPacer | None = None


def retry_delay(attempt: int) -> int:
    base = float(os.getenv("VIDEO_JOBS_RETRY_BASE_SECONDS", "5"))
    maximum = float(os.getenv("VIDEO_JOBS_RETRY_MAX_SECONDS", "120"))
    cap = min(base * (2 ** (attempt - 1)), maximum)
    return max(1, round(random.uniform(cap / 2, cap)))


def deliver_webhook(job: Job) -> None:
    if not job.request.webhook_url:
        return
    payload = {"job_id": job.id, "status": job.status, "result": job.result,
               "client_ref": job.request.metadata.get("client_ref"), "attempts": job.attempts}
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    secret = os.getenv("VIDEO_JOBS_WEBHOOK_SECRET")
    if secret:
        headers["x-video-jobs-signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with urlopen(Request(str(job.request.webhook_url), body, headers), timeout=10):
        pass


def process(job: Job) -> None:
    if start_pacer:
        start_pacer.wait()
    storage = (S3Storage(job.request.delivery.region, os.getenv("VIDEO_JOBS_S3_ENDPOINT_URL"))
               if job.request.delivery.type == "s3"
               else LocalStorage(os.getenv("VIDEO_JOBS_LOCAL_STORAGE", "artifacts")))
    proxy = massive_from_environment()
    player_clients = os.getenv("YTDLP_YOUTUBE_PLAYER_CLIENTS", "android_vr,web_embedded")
    temporary = Path(tempfile.mkdtemp(prefix=f"video-job-{job.id}-"))
    lease_seconds = int(os.getenv("VIDEO_JOBS_LEASE_SECONDS", "120"))
    heartbeat_interval = max(1, int(os.getenv("VIDEO_JOBS_HEARTBEAT_SECONDS", "30")))
    stopped = threading.Event()

    def renew_lease() -> None:
        while not stopped.wait(heartbeat_interval):
            store.heartbeat(job.id, lease_seconds)

    heartbeat = threading.Thread(target=renew_lease, name=f"lease-{job.id[:8]}", daemon=True)
    heartbeat.start()
    terminal.job_started(job.id, job.attempts, job.request.profile, job.request.delivery.type)
    route = proxy.route_for_attempt(job.id, job.attempts) if proxy else None
    exit_metadata = {}
    if proxy and route and os.getenv("VIDEO_JOBS_CAPTURE_EXIT_METADATA", "true").lower() == "true":
        try:
            exit_metadata = proxy.exit_metadata(route)
        except Exception:
            # Metadata collection must not block an otherwise valid download.
            pass
    attempt_id = store.start_attempt(job.id, job.attempts, route.session_hash if route else None,
                                     player_clients, exit_metadata)
    try:
        downloaded = YtDlpDownloader().download(
            str(job.request.url), job.request.profile, temporary, route.url if route else None,
            on_output=lambda output: terminal.downloader_output(job.id, output),
        )
        store.attempt_phase(attempt_id, "uploading", bytes_written=downloaded.path.stat().st_size)
        store.update(job.id, JobStatus.UPLOADING)
        key = f"{job.request.delivery.key_prefix.rstrip('/')}/{downloaded.path.name}".lstrip("/")
        result = storage.put(downloaded.path, key, job.request.delivery.bucket)
        result["video"] = {"id": downloaded.video_id, "bytes": downloaded.path.stat().st_size,
                           "duration_s": downloaded.duration_s, "resolution": downloaded.resolution}
        finished = store.update(job.id, JobStatus.COMPLETED, result=result)
        deliver_webhook(finished)
        store.finish_attempt(attempt_id, "completed", bytes_written=downloaded.path.stat().st_size)
        terminal.completed(job.id, key)
    except Exception as exc:
        classified = classify(exc)
        max_attempts = int(os.getenv("VIDEO_JOBS_MAX_ATTEMPTS", "3"))
        if classified.retryable and job.attempts < max_attempts:
            delay = retry_delay(job.attempts)
            store.finish_attempt(attempt_id, "retrying", classified.code)
            store.retry(job.id, classified.code, classified.message, delay)
            terminal.retrying(job.id, job.attempts, f"{classified.code}; next attempt in {delay}s")
            return
        store.finish_attempt(attempt_id, "failed", classified.code)
        failed = store.update(job.id, JobStatus.FAILED, error_code=classified.code, error_message=classified.message)
        terminal.failed(job.id, classified.message)
        try:
            deliver_webhook(failed)
        except Exception:
            pass
    finally:
        stopped.set()
        heartbeat.join(timeout=1)
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> None:
    global start_pacer
    load_dotenv()
    concurrency = max(1, int(os.getenv("VIDEO_JOBS_WORKER_CONCURRENCY", "4")))
    # No pacing by default: bounded concurrency is the normal throughput control.
    # Set a positive value only when a deployment deliberately needs start-rate limiting.
    starts_per_second = float(os.getenv("VIDEO_JOBS_STARTS_PER_SECOND", "0"))
    start_pacer = StartPacer(starts_per_second)
    terminal.banner(concurrency, massive_from_environment() is not None)
    recovered = store.recover_stale()
    if recovered:
        terminal.line(f"\033[33m  [recovered]\033[0m requeued {recovered} expired worker lease(s)")
    lease_seconds = int(os.getenv("VIDEO_JOBS_LEASE_SECONDS", "120"))
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        while True:
            jobs = [store.claim(lease_seconds) for _ in range(concurrency)]
            claimed = [job for job in jobs if job]
            if claimed:
                list(executor.map(process, claimed))
            else:
                time.sleep(1)
