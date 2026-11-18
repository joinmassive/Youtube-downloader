from __future__ import annotations

import base64
import hashlib
import http.client
import os
import shutil
import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote, unquote, urlsplit

from .errors import DownloadError, redact

DEFAULT_PLAYER_CLIENTS = "android_vr,web_embedded"


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    video_id: str
    duration_s: int | None = None
    resolution: str | None = None


class Downloader(Protocol):
    def download(self, url: str, profile: str, target_dir: Path, proxy: str | None,
                 on_output: Callable[[str], None] | None = None) -> DownloadedFile: ...


class Storage(Protocol):
    def put(self, source: Path, key: str, bucket: str | None = None) -> dict[str, str]: ...


class LocalStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def put(self, source: Path, key: str, bucket: str | None = None) -> dict[str, str]:
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return {"type": "local", "key": key, "path": str(destination)}


class S3Storage:
    """S3 and S3-compatible storage. boto3 is deliberately an optional extra."""
    def __init__(self, region: str | None = None, endpoint_url: str | None = None) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("Install video-jobs[s3] to use S3 delivery") from exc
        self.client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

    def put(self, source: Path, key: str, bucket: str | None = None) -> dict[str, str]:
        if not bucket:
            raise ValueError("S3 delivery requires a bucket")
        self.client.upload_file(str(source), bucket, key)
        return {"type": "s3", "bucket": bucket, "key": key}


class YtDlpDownloader:
    PROFILES = {
        "best_progressive": "best[ext=mp4][acodec!=none][vcodec!=none]/best",
        "max_360p": "best[height<=360][ext=mp4]/best[height<=360]",
        "strict_360p_mux": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    }

    def download(self, url: str, profile: str, target_dir: Path, proxy: str | None,
                 on_output: Callable[[str], None] | None = None) -> DownloadedFile:
        """Download in-process so proxy credentials never enter a subprocess argv."""
        selector = self.PROFILES.get(profile)
        if not selector:
            raise ValueError(f"Unknown download profile: {profile}")
        try:
            from yt_dlp import YoutubeDL
            from yt_dlp.utils import DownloadError as YtDlpError
        except ImportError as exc:
            raise RuntimeError("Install video-jobs[youtube] to download YouTube videos") from exc
        target_dir.mkdir(parents=True, exist_ok=True)
        template = str(target_dir / "%(id)s.%(ext)s")
        player_clients = os.getenv("YTDLP_YOUTUBE_PLAYER_CLIENTS", DEFAULT_PLAYER_CLIENTS)
        messages: list[str] = []

        def record(message: object) -> None:
            safe = redact(str(message))
            messages.append(safe)
            # Bound retained output; yt-dlp can be noisy for large downloads.
            del messages[:-200]
            if on_output:
                on_output(safe)

        class Logger:
            debug = staticmethod(record)
            warning = staticmethod(record)
            error = staticmethod(record)

        def progress(status: dict) -> None:
            if status.get("status") == "downloading":
                record(f"{status.get('_percent_str', '').strip()} at {status.get('_speed_str', '').strip()}")

        runtime_name, _, runtime_path = os.getenv("YTDLP_JS_RUNTIME", "deno").partition(":")
        options = {
            "noplaylist": True,
            "retries": 0,
            "fragment_retries": 0,
            "extractor_retries": 0,
            "format": selector,
            "outtmpl": template,
            "proxy": proxy,
            "socket_timeout": float(os.getenv("YTDLP_SOCKET_TIMEOUT", "45")),
            "js_runtimes": {runtime_name: {"path": runtime_path or None}} if runtime_name else {},
            "extractor_args": {"youtube": {"player_client": player_clients.split(",")}} if player_clients else {},
            "progress_hooks": [progress],
            "logger": Logger(),
            "quiet": True,
            "no_warnings": False,
        }
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
        except YtDlpError as exc:
            record(exc)
            raise DownloadError(1, "\n".join(messages)) from exc
        candidates = [Path(item["filepath"]) for item in info.get("requested_downloads", []) if item.get("filepath")]
        candidates.extend(path for path in target_dir.iterdir() if path.is_file() and not path.name.endswith(".part"))
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if not path:
            raise RuntimeError("yt-dlp completed without an output file")
        return DownloadedFile(path=path, video_id=str(info.get("id") or path.stem),
                              duration_s=info.get("duration"),
                              resolution=(f"{info.get('width')}x{info.get('height')}"
                                          if info.get("width") and info.get("height") else None))


@dataclass(frozen=True)
class ProxyRoute:
    url: str
    session_hash: str


class MassiveStrictProxy:
    """Build a Massive residential proxy URL for one download attempt."""
    def __init__(self, username: str, api_key: str, host: str = "network.joinmassive.com",
                 port: int = 65535, scheme: str = "https", country: str | None = None,
                 session_ttl: int | None = None) -> None:
        self.username, self.api_key, self.host, self.port, self.scheme = username, api_key, host, port, scheme
        self.country, self.session_ttl = country, session_ttl

    def route_for_attempt(self, job_id: str, attempt: int) -> ProxyRoute:
        session = f"{job_id[:12]}-{attempt}-{uuid.uuid4().hex[:10]}"
        parts = [self.username, "session", session, "sessionmode", "strict"]
        if self.session_ttl:
            parts.extend(["sessionttl", str(self.session_ttl)])
        if self.country:
            parts.extend(["country", self.country])
        user = quote("-".join(parts), safe="-")
        password = quote(self.api_key, safe="")
        return ProxyRoute(f"{self.scheme}://{user}:{password}@{self.host}:{self.port}",
                          hashlib.sha256(session.encode()).hexdigest()[:16])

    def for_attempt(self, job_id: str, attempt: int) -> str:
        return self.route_for_attempt(job_id, attempt).url

    def exit_metadata(self, route: ProxyRoute, target: str = "www.youtube.com") -> dict[str, str]:
        """Read Massive CONNECT metadata without persisting credentials or raw headers."""
        parsed = urlsplit(route.url)
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        connection = (http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=15, context=ssl.create_default_context())
                      if parsed.scheme == "https" else http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15))
        try:
            connection.request("CONNECT", f"{target}:443", headers={"Host": f"{target}:443", "Proxy-Authorization": f"Basic {auth}"})
            response = connection.getresponse()
            if response.status != 200:
                return {}
            return {
                "ip": response.getheader("X-Exit-IP") or "",
                "country": response.getheader("X-Exit-Country") or "",
                "timezone": response.getheader("X-Exit-Timezone") or "",
                "asn": response.getheader("X-Exit-ASN") or "",
            }
        finally:
            connection.close()



def massive_from_environment() -> MassiveStrictProxy | None:
    username = os.getenv("PROXY_USER") or os.getenv("MASSIVE_PROXY_USERNAME")
    api_key = os.getenv("PROXY_PASS") or os.getenv("MASSIVE_API_KEY")
    if not username or not api_key:
        return None
    port = int(os.getenv("PROXY_PORT", "65535"))
    # Massive documents 65534 as HTTP and 65535 as HTTPS.
    scheme = os.getenv("PROXY_SCHEME", "http" if port == 65534 else "https")
    ttl = os.getenv("PROXY_SESSION_TTL")
    return MassiveStrictProxy(
        username, api_key, os.getenv("PROXY_HOST", "network.joinmassive.com"), port, scheme,
        os.getenv("PROXY_COUNTRY"), int(ttl) if ttl else None,
    )
