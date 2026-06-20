"""Local filesystem operations for moving downloaded tracks into the Jellyfin library."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Characters that are illegal in Windows or Linux path components.
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize(name: str) -> str:
    """Strip characters that are illegal in file/directory names."""
    safe = _UNSAFE_CHARS.sub("_", name).strip(". ")
    return safe[:200]  # cap at 200 chars to stay well within OS limits


class LibraryManager:
    def __init__(self, music_dir: str) -> None:
        self.music_dir = Path(music_dir)

    def move_to_library(self, src: Path, artist: str, album: str) -> Path:
        """
        Move *src* into ``<music_dir>/<artist>/<album>/`` and return the new path.

        Follows the standard Jellyfin/Plex library layout so the metadata scanner
        can pair each file with the correct artist and album automatically.
        """
        dest_dir = self.music_dir / _sanitize(artist) / _sanitize(album)
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest = dest_dir / src.name
        if dest.exists():
            logger.warning("Destination already exists — overwriting: %s", dest)
            dest.unlink()

        shutil.move(str(src), str(dest))
        logger.info("Moved: %s  →  %s", src, dest)
        return dest
