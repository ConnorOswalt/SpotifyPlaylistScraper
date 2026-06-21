"""slskd REST API client — Soulseek search, result filtering, and download management."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Search states that indicate the search has finished (successfully or not).
_TERMINAL_SEARCH_STATES = frozenset({"Completed", "Cancelled", "TimedOut"})

# Transfer states that mean no further progress will be made.
_TERMINAL_TRANSFER_STATES = frozenset(
    {"Completed", "Errored", "Cancelled", "Rejected", "TimedOut"}
)

# Path substrings that suggest a file is NOT the original studio/album version.
_SKIP_PATH_KEYWORDS = frozenset(
    [
        "youtube",
        "ytdl",
        "(rip)",
        " rip)",
        "webrip",
        "web rip",
        "live at",
        "live @",
        "(live)",
        "radio edit",
        "bonus disc",
        "bonus disk",
    ]
)


@dataclass
class SlskdFile:
    username: str
    filename: str   # Full remote path as reported by the Soulseek peer
    size: int
    bitrate: Optional[int]
    extension: str
    free_slots: int


class SlskdClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        download_dir: str,
        search_timeout: int = 30,
        result_limit: int = 100,
    ) -> None:
        self.url = url.rstrip("/")
        self.download_dir = Path(download_dir)
        self.search_timeout = search_timeout
        self.result_limit = result_limit

        self._session = requests.Session()
        self._session.headers.update(
            {
                "X-API-Key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[SlskdFile]:
        """Run a Soulseek search and return all candidate files."""
        search_id = str(uuid.uuid4())
        payload = {
            "id": search_id,
            "searchText": query,
            # slskd accepts timeout in milliseconds
            "timeout": self.search_timeout * 1000,
            # Only request peers that have at least one free upload slot
            "minimumPeerFreeUploadSlots": 0,
        }

        resp = self._session.post(f"{self.url}/api/v0/searches", json=payload)
        if resp.status_code == 401:
            raise RuntimeError(
                "slskd rejected the API key. Update slskd.api_key in config.yaml to match slskd-config/slskd.yml."
            )
        if resp.status_code == 409:
            raise RuntimeError(
                "slskd is not connected to Soulseek yet. Open http://localhost:5030, sign in with your Soulseek username/password, and connect the server before running imports."
            )
        resp.raise_for_status()

        # Poll until the search reaches a terminal state.
        deadline = time.monotonic() + self.search_timeout + 5
        data: dict = {}
        while time.monotonic() < deadline:
            time.sleep(2)
            r = self._session.get(f"{self.url}/api/v0/searches/{search_id}")
            r.raise_for_status()
            data = r.json()
            state: str = data.get("state", "")
            if any(s in state for s in _TERMINAL_SEARCH_STATES):
                break

        response_rows = data.get("responses") or []
        # Some slskd builds may omit embedded response rows from /searches/{id}
        # while exposing responseCount/fileCount. Try optional response endpoints.
        response_count = int(data.get("responseCount") or 0)
        if (not response_rows) and response_count > 0:
            candidate_paths = [
                f"{self.url}/api/v0/searches/{search_id}/responses",
            ]
            token = data.get("token")
            if token is not None:
                candidate_paths.append(f"{self.url}/api/v0/searches/{token}/responses")

            # slskd can report responseCount > 0 while /responses materializes shortly later.
            fetch_deadline = time.monotonic() + 20
            while time.monotonic() < fetch_deadline and not response_rows:
                for endpoint in candidate_paths:
                    try:
                        rr = self._session.get(endpoint)
                        if rr.status_code == 404:
                            continue
                        rr.raise_for_status()
                        response_rows = rr.json() or []
                        if response_rows:
                            break
                    except requests.RequestException:
                        continue
                if not response_rows:
                    time.sleep(2)

            if not response_rows:
                logger.warning(
                    "slskd search reported %s responses for '%s' but no rows were retrievable",
                    response_count,
                    query,
                )

        results: list[SlskdFile] = []
        try:
            for response in response_rows:
                username: str = response.get("username", "")
                has_free_slot = bool(response.get("hasFreeUploadSlot", False))
                free_slots: int = response.get("freeUploadSlots", 1 if has_free_slot else 0)
                for f in response.get("files", []):
                    raw_name: str = f.get("filename", "")
                    # Normalise Windows-style backslash separators for Path parsing.
                    ext = Path(raw_name.replace("\\", "/")).suffix.lstrip(".").lower()
                    results.append(
                        SlskdFile(
                            username=username,
                            filename=raw_name,
                            size=f.get("size", 0),
                            bitrate=f.get("bitRate"),
                            extension=ext,
                            free_slots=free_slots,
                        )
                    )
                    if len(results) >= self.result_limit:
                        break
                if len(results) >= self.result_limit:
                    break
            return results
        finally:
            # Remove the search from slskd to keep it tidy.
            try:
                self._session.delete(f"{self.url}/api/v0/searches/{search_id}")
            except requests.RequestException:
                pass

    # ------------------------------------------------------------------
    # Filtering & ranking
    # ------------------------------------------------------------------

    def filter_candidates(
        self,
        files: list[SlskdFile],
        accepted_formats: list[str],
        min_mp3_bitrate: int = 320,
        prefer_flac: bool = True,
        album_hint: str = "",
    ) -> list[SlskdFile]:
        """
        Narrow and rank *files* by format quality, bitrate, and peer availability.

        Ranking priority (ascending = better):
          1. Peer has a free upload slot
          2. Album name appears in the file path  (avoids compilation rips)
          3. FLAC before MP3 (when prefer_flac is True)
          4. Highest bitrate
        """
        accepted = set(accepted_formats)
        album_lower = album_hint.lower()

        candidates: list[SlskdFile] = []
        for f in files:
            if f.extension not in accepted:
                continue
            # Reject MP3s below the minimum acceptable quality.
            if f.extension == "mp3" and f.bitrate is not None and f.bitrate < min_mp3_bitrate:
                continue
            # Skip files whose paths hint at non-album sources.
            path_lower = f.filename.lower()
            if any(kw in path_lower for kw in _SKIP_PATH_KEYWORDS):
                continue
            candidates.append(f)

        def _sort_key(f: SlskdFile) -> tuple:
            slot_score = 0 if f.free_slots > 0 else 1
            album_score = 0 if (album_lower and album_lower in f.filename.lower()) else 1
            fmt_score = 0 if (f.extension == "flac" and prefer_flac) else 1
            # Negate bitrate so higher bitrate sorts first.
            bitrate_score = -(f.bitrate or 0)
            return (slot_score, album_score, fmt_score, bitrate_score)

        return sorted(candidates, key=_sort_key)

    # ------------------------------------------------------------------
    # Download management
    # ------------------------------------------------------------------

    def queue_download(self, f: SlskdFile) -> None:
        """Enqueue a single file download from a specific Soulseek user."""
        resp = self._session.post(
            f"{self.url}/api/v0/transfers/downloads/{f.username}",
            json=[{"filename": f.filename, "size": f.size}],
        )
        resp.raise_for_status()

    def wait_for_download(
        self,
        username: str,
        filename: str,
        poll_interval: int = 5,
        timeout: int = 600,
    ) -> Optional[Path]:
        """
        Poll slskd until *filename* from *username* reaches a terminal transfer state.

        Returns the local path of the completed file, or None on failure/timeout.
        """
        deadline = time.monotonic() + timeout

        def _iter_transfers(payload: object) -> list[dict]:
            # slskd may return either a flat transfer list or a grouped object
            # with directories[].files[] depending on version/endpoint behavior.
            if isinstance(payload, list):
                return [x for x in payload if isinstance(x, dict)]
            if isinstance(payload, dict):
                out: list[dict] = []
                for d in payload.get("directories", []) or []:
                    if not isinstance(d, dict):
                        continue
                    for f in d.get("files", []) or []:
                        if isinstance(f, dict):
                            out.append(f)
                return out
            return []

        while time.monotonic() < deadline:
            time.sleep(poll_interval)

            try:
                resp = self._session.get(
                    f"{self.url}/api/v0/transfers/downloads/{username}"
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.warning("Transfer status check failed: %s", exc)
                continue

            for xfer in _iter_transfers(resp.json()):
                if xfer.get("filename") != filename:
                    continue
                state: str = xfer.get("state", "")
                logger.debug("Transfer state for '%s': %s", Path(filename).name, state)

                if "Completed" in state:
                    target_name = Path(filename.replace("\\", "/")).name
                    return self.find_in_download_dir(target_name)

                if any(s in state for s in ("Errored", "Cancelled", "Rejected", "TimedOut")):
                    logger.error("Download entered failed state [%s]: %s", state, filename)
                    return None
                # Still in progress — keep polling.
                break

        logger.warning("Download timed out after %ds: %s", timeout, filename)
        return None

    def find_in_download_dir(self, filename: str) -> Optional[Path]:
        """Scan the download directory recursively for a file by name."""
        matches = sorted(
            self.download_dir.rglob(filename),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None
