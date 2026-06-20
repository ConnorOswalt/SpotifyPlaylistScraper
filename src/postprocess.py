"""Post-download metadata standardization and verification."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from pathlib import Path

from mutagen import File as MutagenFile

from .spotify import SpotifyTrack

logger = logging.getLogger(__name__)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class PicardPostProcessor:
    def __init__(
        self,
        enabled: bool,
        command_template: str,
        timeout: int = 180,
        verify_artist_title: bool = True,
        require_success: bool = False,
    ) -> None:
        self.enabled = enabled
        self.command_template = command_template.strip()
        self.timeout = timeout
        self.verify_artist_title = verify_artist_title
        self.require_success = require_success

    def process(self, file_path: Path, track: SpotifyTrack) -> bool:
        """
        Run external Picard command and optionally verify resulting tags.

        Returns True when processing is successful enough to continue.
        """
        if not self.enabled:
            return True

        if not self.command_template:
            logger.warning("Picard enabled but no command_template is set; skipping.")
            return not self.require_success

        command = self.command_template.format(
            file=str(file_path),
            artist=track.artist,
            title=track.title,
            album=track.album,
            isrc=track.isrc or "",
        )

        try:
            args = shlex.split(command, posix=False)
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.error("Picard command failed to start for %s: %s", file_path.name, exc)
            return not self.require_success

        if completed.returncode != 0:
            logger.error(
                "Picard command failed for %s (exit %s): %s",
                file_path.name,
                completed.returncode,
                (completed.stderr or completed.stdout or "").strip(),
            )
            return not self.require_success

        if self.verify_artist_title:
            verified = self._verify_tags(file_path, track)
            if not verified:
                logger.warning(
                    "Tag verification mismatch for %s (%s - %s)",
                    file_path.name,
                    track.artist,
                    track.title,
                )
                return not self.require_success

        return True

    def _verify_tags(self, file_path: Path, track: SpotifyTrack) -> bool:
        audio = MutagenFile(file_path, easy=True)
        if not audio:
            return False

        tagged_title = " ".join(audio.get("title", [])).strip()
        tagged_artist = " ".join(audio.get("artist", [])).strip()
        if not tagged_title or not tagged_artist:
            return False

        expected_title = _norm(track.title)
        expected_artist = _norm(track.artist)
        got_title = _norm(tagged_title)
        got_artist = _norm(tagged_artist)

        # Allow partial containment for featured-artist and punctuation differences.
        title_ok = expected_title in got_title or got_title in expected_title
        artist_ok = expected_artist in got_artist or got_artist in expected_artist
        return title_ok and artist_ok
