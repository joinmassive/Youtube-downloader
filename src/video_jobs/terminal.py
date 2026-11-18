"""Small dependency-free terminal display for the local worker."""
from __future__ import annotations

import threading

from .errors import redact

_lock = threading.Lock()
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"


def line(message: str) -> None:
    with _lock:
        print(message, flush=True)


def _short(job_id: str) -> str:
    return job_id.split("-")[0]


def banner(concurrency: int, proxy_enabled: bool) -> None:
    route = "Massive strict sessions" if proxy_enabled else "direct connection"
    line(f"{RED}{BOLD}"
         "╔════════════════════════════════════════════════════════════╗\n"
         "║                 VIDEO JOBS  •  WORKER                      ║\n"
         "╚════════════════════════════════════════════════════════════╝"
         f"{RESET}")
    line(f"{CYAN}  [ready]{RESET} workers: {BOLD}{concurrency}{RESET}  route: {BOLD}{route}{RESET}")
    line(f"{DIM}  waiting for queued jobs… (Ctrl-C stops after the active batch){RESET}")


def job_started(job_id: str, attempt: int, profile: str, delivery: str) -> None:
    line(f"\n{MAGENTA}{BOLD}┌─ job {_short(job_id)}  •  attempt {attempt}{RESET}")
    line(f"{MAGENTA}├─ profile  {RESET}{profile}")
    line(f"{MAGENTA}├─ route    {RESET}starting downloader")
    line(f"{MAGENTA}└─ delivery {RESET}{delivery}")


def downloader_output(job_id: str, message: str) -> None:
    cleaned = message.strip()
    if cleaned:
        line(f"{CYAN}  │ [{_short(job_id)}] yt-dlp {RESET}{cleaned}")


def completed(job_id: str, key: str) -> None:
    line(f"{GREEN}{BOLD}  ✓ [{_short(job_id)}] completed{RESET} {key}")


def retrying(job_id: str, attempt: int, message: str) -> None:
    line(f"{YELLOW}{BOLD}  ↻ [{_short(job_id)}] retrying{RESET} after attempt {attempt}: {redact(message)}")


def failed(job_id: str, message: str) -> None:
    line(f"{RED}{BOLD}  ✗ [{_short(job_id)}] failed{RESET} {redact(message)}")


def recovered(count: int) -> None:
    line(f"{YELLOW}  [recovered]{RESET} requeued {count} expired worker lease(s)")


def webhook_failed(job_id: str, message: str) -> None:
    line(f"{YELLOW}  ! [{_short(job_id)}] webhook delivery failed{RESET} {redact(message)}")
