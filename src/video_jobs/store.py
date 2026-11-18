from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .models import Job, JobStatus, SubmitJob


class SqliteJobStore:
    """Small local store; replace this boundary with Postgres/queue infrastructure."""

    def __init__(self, database: str) -> None:
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, status TEXT NOT NULL,
            request TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            error_code TEXT, error_message TEXT, result TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS job_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL,
            session_hash TEXT,
            player_clients TEXT,
            phase TEXT NOT NULL,
            error_code TEXT,
            elapsed_ms INTEGER,
            bytes INTEGER,
            exit_ip TEXT,
            exit_country TEXT,
            exit_timezone TEXT,
            exit_asn TEXT,
            UNIQUE(job_id, attempt))""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "available_at" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN available_at REAL NOT NULL DEFAULT 0")
        if "lease_expires_at" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN lease_expires_at REAL")
        self.db.commit()

    def create(self, request: SubmitJob, idempotency_key: str | None = None) -> Job:
        with self.lock:
            if idempotency_key:
                existing = self.db.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
                if existing:
                    return self._job(existing)
            job_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO jobs(id,idempotency_key,status,request) VALUES(?,?,?,?)",
                            (job_id, idempotency_key, JobStatus.QUEUED, request.model_dump_json()))
            self.db.commit()
            return self.get(job_id)

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._job(row) if row else None

    def claim(self, lease_seconds: int = 120) -> Job | None:
        with self.lock:
            now = time.time()
            row = self.db.execute("SELECT * FROM jobs WHERE status = ? AND available_at <= ? ORDER BY rowid LIMIT 1",
                                  (JobStatus.QUEUED, now)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE jobs SET status=?, attempts=attempts+1, lease_expires_at=? WHERE id=? AND status=?",
                            (JobStatus.RUNNING, now + lease_seconds, row["id"], JobStatus.QUEUED))
            self.db.commit()
            return self.get(row["id"])

    def heartbeat(self, job_id: str, lease_seconds: int) -> None:
        with self.lock:
            self.db.execute("UPDATE jobs SET lease_expires_at=? WHERE id=? AND status=?",
                            (time.time() + lease_seconds, job_id, JobStatus.RUNNING))
            self.db.commit()

    def recover_stale(self) -> int:
        """Return interrupted jobs to the queue after their lease expires."""
        with self.lock:
            cursor = self.db.execute("""UPDATE jobs
                SET status=?, available_at=?, lease_expires_at=NULL,
                    error_code='worker_lease_expired', error_message='Worker lease expired; requeued'
                WHERE status=? AND (lease_expires_at IS NULL OR lease_expires_at < ?)""",
                (JobStatus.QUEUED, time.time(), JobStatus.RUNNING, time.time()))
            self.db.commit()
            return cursor.rowcount

    def retry(self, job_id: str, error_code: str, message: str, delay_seconds: int) -> Job:
        with self.lock:
            self.db.execute("""UPDATE jobs
                SET status=?, available_at=?, lease_expires_at=NULL,
                    error_code=?, error_message=? WHERE id=?""",
                (JobStatus.QUEUED, time.time() + delay_seconds, error_code, message, job_id))
            self.db.commit()
            return self.get(job_id)

    def retry_terminal(self, job_id: str) -> Job | None:
        """Operator-triggered retry with a fresh attempt budget."""
        with self.lock:
            cursor = self.db.execute("""UPDATE jobs
                SET status=?, attempts=0, available_at=?, lease_expires_at=NULL,
                    error_code=NULL, error_message=NULL
                WHERE id=? AND status=?""",
                (JobStatus.QUEUED, time.time(), job_id, JobStatus.FAILED))
            self.db.commit()
            return self.get(job_id) if cursor.rowcount else None

    def start_attempt(self, job_id: str, attempt: int, session_hash: str | None,
                      player_clients: str, exit: dict[str, str] | None = None) -> int:
        exit = exit or {}
        with self.lock:
            cursor = self.db.execute("""INSERT OR REPLACE INTO job_attempts(
                job_id,attempt,started_at,session_hash,player_clients,phase,
                exit_ip,exit_country,exit_timezone,exit_asn)
                VALUES(?,?,?,?,?,'extracting',?,?,?,?)""",
                (job_id, attempt, time.time(), session_hash, player_clients,
                 exit.get("ip"), exit.get("country"), exit.get("timezone"), exit.get("asn")))
            self.db.commit()
            return cursor.lastrowid

    def attempt_phase(self, attempt_id: int, phase: str, *, bytes_written: int | None = None) -> None:
        with self.lock:
            self.db.execute("UPDATE job_attempts SET phase=?, bytes=COALESCE(?, bytes) WHERE id=?",
                            (phase, bytes_written, attempt_id))
            self.db.commit()

    def finish_attempt(self, attempt_id: int, phase: str, error_code: str | None = None,
                       bytes_written: int | None = None) -> None:
        with self.lock:
            now = time.time()
            row = self.db.execute("SELECT started_at FROM job_attempts WHERE id=?", (attempt_id,)).fetchone()
            elapsed = round((now - row["started_at"]) * 1000) if row else None
            self.db.execute("""UPDATE job_attempts
                SET phase=?, error_code=?, finished_at=?, elapsed_ms=?, bytes=COALESCE(?, bytes)
                WHERE id=?""", (phase, error_code, now, elapsed, bytes_written, attempt_id))
            self.db.commit()

    def attempts_for(self, job_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt", (job_id,)).fetchall()
            return [dict(row) for row in rows]

    def update(self, job_id: str, status: JobStatus, *, result: dict | None = None,
               error_code: str | None = None, error_message: str | None = None) -> Job:
        with self.lock:
            self.db.execute("UPDATE jobs SET status=?, result=?, error_code=?, error_message=?, lease_expires_at=NULL WHERE id=?",
                            (status, json.dumps(result) if result else None, error_code, error_message, job_id))
            self.db.commit()
            return self.get(job_id)

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(id=row["id"], status=row["status"], request=SubmitJob.model_validate_json(row["request"]),
                   attempts=row["attempts"], error_code=row["error_code"], error_message=row["error_message"],
                   result=json.loads(row["result"]) if row["result"] else None)
