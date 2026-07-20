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
    short = job_id.split("-")[0]
    line(f"\n{MAGENTA}{BOLD}┌─ job {short}  •  attempt {attempt}{RESET}")
    line(f"{MAGENTA}├─ profile  {RESET}{profile}")
    line(f"{MAGENTA}├─ route    {RESET}starting downloader")
    line(f"{MAGENTA}└─ delivery {RESET}{delivery}")


def downloader_output(job_id: str, message: str) -> None:
    short = job_id.split("-")[0]
    cleaned = message.strip()
    if cleaned:
        line(f"{CYAN}  │ [{short}] yt-dlp {RESET}{cleaned}")


def completed(job_id: str, key: str) -> None:
    line(f"{GREEN}{BOLD}  ✓ [{job_id.split('-')[0]}] completed{RESET} {key}")


def retrying(job_id: str, attempt: int, message: str) -> None:
    line(f"{YELLOW}{BOLD}  ↻ [{job_id.split('-')[0]}] retrying{RESET} after attempt {attempt}: {redact(message)}")


def failed(job_id: str, message: str) -> None:
    line(f"{RED}{BOLD}  ✗ [{job_id.split('-')[0]}] failed{RESET} {redact(message)}")
