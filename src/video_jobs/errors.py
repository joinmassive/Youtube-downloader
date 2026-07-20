"""Classify sanitized downloader failures into retryable and terminal outcomes."""
from __future__ import annotations

import re
from dataclasses import dataclass


_CREDENTIALS = re.compile(r"(https?://[^:\s]+:)[^@\s]+@")

_PERMANENT = {
    "private video": "video_private",
    "sign in to confirm your age": "age_restricted",
    "sign in to confirm you're not a bot": "bot_wall",
    "confirm you're not a bot": "bot_wall",
    "video unavailable": "video_unavailable",
    "removed by the uploader": "video_removed",
    "http error 404": "not_found",
    "is not available in your country": "geo_blocked",
    "members-only": "members_only",
    "requested format is not available": "format_unavailable",
}

_TRANSIENT = (
    "timed out", "timeout", "connection reset", "connection aborted",
    "connection refused", "temporarily", "unable to connect", "handshake",
    "network is unreachable", "remote end closed", "http error 5", "http error 429",
    "no upstream proxy available", "bad gateway", "service unavailable",
)


@dataclass(frozen=True)
class ClassifiedError:
    code: str
    message: str
    retryable: bool


class DownloadError(RuntimeError):
    """Downloader output, retained without embedding its proxy command."""
    def __init__(self, returncode: int, output: str) -> None:
        self.returncode = returncode
        self.output = output
        super().__init__(output)


def redact(message: str) -> str:
    return _CREDENTIALS.sub(r"\1***@", message)


def classify(exc: Exception) -> ClassifiedError:
    """Permanent yt-dlp outcomes fail fast; unknown failures retry conservatively."""
    if isinstance(exc, ValueError):
        return ClassifiedError("invalid_configuration", redact(str(exc)), False)
    text = redact(exc.output if isinstance(exc, DownloadError) else str(exc))
    # yt-dlp messages may use typographic quotes; classification must not depend
    # on the typography of a YouTube error message.
    lowered = text.lower().replace("’", "'").replace("‘", "'")
    for marker, code in _PERMANENT.items():
        if marker in lowered:
            return ClassifiedError(code, text, False)
    for marker in _TRANSIENT:
        if marker in lowered:
            code = "proxy_capacity" if "tunnel connection failed: 503" in lowered else "transient_download_error"
            return ClassifiedError(code, text, True)
    return ClassifiedError("unknown_download_error", text, True)
