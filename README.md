# Tutorial: Reliable YouTube downloads with Massive Residential

This tutorial shows how to run asynchronous YouTube downloads through Massive
Residential Proxies. The service wraps yt-dlp with the operating rules that
matter at volume: bounded concurrency, one strict sticky session per download
attempt, fresh routing on retry, and visible attempt telemetry.

The goal is to avoid the common direct-script failure modes: unbounded
connections, `sessionmode=flex` timeouts, retries that reuse a failing route,
and proxy errors that are invisible after the job fails.

> Status: alpha / v0.1. SQLite is suitable for local development and a single
> worker host, not a multi-node production deployment.

## What this tutorial builds

- Async API: queue a job with `POST /video`; poll it with `GET /video/{id}`.
- Explicit states: `queued`, `running`, `uploading`, `completed`, `failed`.
- Bounded per-worker concurrency and worker leases; stale `running` jobs are
  recovered when a worker restarts.
- Massive Residential strict sessions with a fresh route for every retry.
- yt-dlp **Python API** integration: proxy credentials are not passed through a
  child process command line.
- YouTube player-client configuration and Deno JavaScript-runtime support.
- Local filesystem and optional S3/S3-compatible output storage.
- Per-attempt telemetry, including phase, timing, bytes, player clients, and
  Massive exit metadata when enabled.
- Optional completion webhook with HMAC signature.

## Before you start

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- [Deno](https://deno.com/) for reliable modern YouTube player extraction
- `ffmpeg` only for the future muxed-format profile; it is not required for
  progressive downloads
- A Massive Residential account and credentials

## The operating model

```text
POST /video
    ↓
worker claims one job (bounded concurrency, e.g. 30)
    ↓
create a unique Massive session ID with sessionmode=strict
    ↓
capture Massive exit metadata → yt-dlp extracts and downloads
    ↓
upload result → mark completed → optional webhook
```

If a connection, timeout, 5xx, or proxy-capacity error occurs, the worker
delays the job and retries it with a new Massive session ID. It does not ask
yt-dlp to retry internally, because that would reuse the same proxy route.

## Step 1: Install the service

```bash
git clone https://github.com/joinmassive/Youtube-downloader.git
cd Youtube-downloader

uv venv
uv pip install -e '.[youtube,dev,s3]'
brew install deno

cp .env.example .env
```

## Step 2: Configure Massive

Edit `.env` and add the Massive credentials supplied by your account; never
commit this file. Start with this configuration:

```dotenv
PROXY_HOST=network.joinmassive.com
PROXY_PORT=65534
PROXY_USER=replace-me
PROXY_PASS=replace-me
PROXY_SESSION_TTL=30

VIDEO_JOBS_WORKER_CONCURRENCY=30
VIDEO_JOBS_MAX_ATTEMPTS=3
VIDEO_JOBS_CAPTURE_EXIT_METADATA=true
```

Why these settings:

- `sessionmode=strict` is added by the worker to every session. A route error
  invalidates the session rather than persisting it through failures.
- The worker generates a new session ID per attempt, so retries do not reuse a
  failed route.
- `VIDEO_JOBS_WORKER_CONCURRENCY` is a per-worker cap. Choose it from observed
  proxy and YouTube success rates; 30 is a useful load-test setting.
- `PROXY_SESSION_TTL=30` gives a long video a stable route for its attempt.

## Step 3: Start the API and worker

Start the API in one terminal:

```bash
uv run video-jobs-api
```

Start the worker in another:

```bash
uv run video-jobs-worker
```

The API listens on `http://127.0.0.1:8000`.

## Configuration reference

`.env` is loaded automatically by both the API and worker. The most important
settings are:

```dotenv
VIDEO_JOBS_DATABASE=./video-jobs.db
VIDEO_JOBS_LOCAL_STORAGE=./artifacts
VIDEO_JOBS_WORKER_CONCURRENCY=20
VIDEO_JOBS_MAX_ATTEMPTS=3
VIDEO_JOBS_RETRY_BASE_SECONDS=5
VIDEO_JOBS_RETRY_MAX_SECONDS=120

# Set to 0 for no start-rate limiting.
VIDEO_JOBS_STARTS_PER_SECOND=0

# Modern YouTube extraction.
YTDLP_JS_RUNTIME=deno
YTDLP_SOCKET_TIMEOUT=45
YTDLP_YOUTUBE_PLAYER_CLIENTS=android_vr,web_embedded
```

### Massive routing details

Massive is the core routing layer. Every worker attempt has a unique sticky
session ID and `sessionmode=strict`. A retry therefore starts a fresh Massive
route rather than repeating the same failed session.

```dotenv
PROXY_HOST=network.joinmassive.com
PROXY_PORT=65534
PROXY_USER=replace-me
PROXY_PASS=replace-me
PROXY_SESSION_TTL=30

# Capture X-Exit-IP, country, timezone, and ASN once per attempt.
VIDEO_JOBS_CAPTURE_EXIT_METADATA=true
```

Use the HTTPS Massive endpoint when your environment supports it. Do not put
proxy credentials in a request body, source file, terminal history, or issue.

## Step 4: Submit and monitor a video

The response is immediate: downloading happens in the worker, not in the HTTP
request.

### Queue a video job

```bash
curl -X POST http://127.0.0.1:8000/video \
  -H 'content-type: application/json' \
  -H 'idempotency-key: example-video-1' \
  -d '{
    "url": "https://www.youtube.com/watch?v=VIDEO_ID",
    "profile": "best_progressive",
    "delivery": {
      "type": "local",
      "key_prefix": "youtube/"
    },
    "metadata": {
      "client_ref": "example-123"
    }
  }'
```

The current alpha response returns a full job object:

```json
{
  "id": "d13ba53f-937d-4a26-8a11-68cdc3a57891",
  "status": "queued",
  "attempts": 0,
  "error_code": null,
  "error_message": null,
  "result": null,
  "request": { "...": "original request" }
}
```

### Poll a job

```bash
curl http://127.0.0.1:8000/video/JOB_ID
```

### Inspect every Massive-backed attempt

```bash
curl http://127.0.0.1:8000/video/JOB_ID/attempts
```

Each record contains the session hash, selected player clients, extraction or
upload phase, elapsed time, byte count, error code, and optional Massive exit
metadata. Credentials are never included.

### Retry a terminal failure

```bash
curl -X POST http://127.0.0.1:8000/video/JOB_ID/retry
```

This resets the attempt budget and queues a fresh download.

## Download profiles

| Profile | Current behavior |
| --- | --- |
| `best_progressive` | Best available single-file video+audio format, preferring MP4. |
| `max_360p` | Best available format at or below 360p. |
| `strict_360p_mux` | Muxed best video/audio at or below 360p; requires ffmpeg in a full production implementation. |

`strict_360p_mux` currently remains a policy placeholder: it does not yet
enforce an exact 360p source. See the roadmap before using it as a contractual
format guarantee.

## How retries work

yt-dlp internal retries are disabled. The worker owns retry decisions so it can
apply a backoff and construct a fresh Massive session for the next attempt.

- Permanent errors, such as private/removed/unavailable videos and known bot
  walls, fail fast.
- Transient connection, timeout, 5xx, and proxy-capacity errors retry up to
  `VIDEO_JOBS_MAX_ATTEMPTS`.
- A worker crash leaves a lease behind; an expired lease is recovered and
  requeued when a worker starts.

## Storage and webhooks

Use local delivery for development:

```json
{ "type": "local", "key_prefix": "youtube/" }
```

Use S3 when the optional `s3` dependency and AWS-compatible credentials are
available:

```json
{
  "type": "s3",
  "bucket": "customer-bucket",
  "key_prefix": "youtube/",
  "region": "us-east-1"
}
```

Set `webhook_url` on a job to receive terminal notifications. If
`VIDEO_JOBS_WEBHOOK_SECRET` is set, the delivery includes an
`x-video-jobs-signature` HMAC-SHA256 header.

## Development

```bash
uv run pytest -q
python -m compileall -q src tests
```

The test suite uses mocked yt-dlp/Massive behavior and never downloads YouTube
videos or requires proxy credentials. It covers API submission/polling, error
classification, session construction, leases, retry state, telemetry, and a
100-job queue simulation bounded to 30 claims.

## Security and responsible use

- Keep `.env`, databases, downloaded artifacts, and test videos out of Git.
- Validate and allowlist webhook targets before using this in an untrusted,
  multi-tenant deployment.
- You are responsible for ensuring that requested downloads comply with content
  rights, platform terms, and applicable law.
