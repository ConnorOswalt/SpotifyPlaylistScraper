#!/usr/bin/env python3
"""Sync a Spotify playlist to Jellyfin, acquiring missing tracks via slskd."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time

import yaml

from src.jellyfin import JellyfinClient
from src.library import LibraryManager
from src.slskd import SlskdClient
from src.spotify import SpotifyFetcher

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_playlist_id(jellyfin: JellyfinClient, name_or_id: str) -> str:
    """Return a Jellyfin playlist ID.

    If *name_or_id* looks like a UUID it is returned as-is; otherwise the
    playlist is looked up by name and created if it does not yet exist.
    """
    if _UUID_RE.match(name_or_id):
        return name_or_id
    return jellyfin.get_or_create_playlist(name_or_id)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace, cfg: dict) -> None:
    spotify = SpotifyFetcher(
        client_id=cfg["spotify"]["client_id"],
        client_secret=cfg["spotify"]["client_secret"],
        redirect_uri=cfg["spotify"]["redirect_uri"],
        cache_path=cfg["spotify"].get("cache_path", ".spotify_cache"),
    )
    jellyfin = JellyfinClient(
        url=cfg["jellyfin"]["url"],
        api_key=cfg["jellyfin"]["api_key"],
        user_id=cfg["jellyfin"]["user_id"],
    )
    slskd = SlskdClient(
        url=cfg["slskd"]["url"],
        api_key=cfg["slskd"]["api_key"],
        download_dir=cfg["slskd"]["download_dir"],
        search_timeout=cfg["slskd"].get("search_timeout", 30),
        result_limit=cfg["slskd"].get("result_limit", 100),
    )
    lib_mgr = LibraryManager(music_dir=cfg["library"]["music_dir"])
    lib_cfg: dict = cfg["library"]

    # ------------------------------------------------------------------
    # Stage 1 — Fetch playlist from Spotify
    # ------------------------------------------------------------------
    logger.info("Fetching Spotify playlist: %s", args.spotify_playlist)
    tracks = spotify.get_playlist_tracks(args.spotify_playlist)
    logger.info("Total tracks: %d", len(tracks))

    if args.dry_run:
        for t in tracks:
            isrc = t.isrc or "no ISRC"
            print(f"  {t.artist} – {t.title}  |  {t.album}  |  {isrc}")
        return

    playlist_id = resolve_playlist_id(jellyfin, args.jellyfin_playlist)
    logger.info("Target Jellyfin playlist: %s", playlist_id)

    matched = downloaded = failed = skipped = 0

    for track in tracks:
        label = f"{track.artist} – {track.title}"

        # ------------------------------------------------------------------
        # Stage 2 — Check Jellyfin local library
        # ------------------------------------------------------------------
        jf_id = jellyfin.search_track(track)
        if jf_id:
            logger.info("[✓ LIBRARY  ] %s", label)
            jellyfin.add_to_playlist(playlist_id, [jf_id])
            matched += 1
            continue

        if args.no_download:
            logger.info("[– SKIP     ] %s", label)
            skipped += 1
            continue

        # ------------------------------------------------------------------
        # Stage 3 — Search Soulseek via slskd
        # ------------------------------------------------------------------
        query = f"{track.artist} - {track.title}"
        logger.info("[? SEARCH   ] %s", label)

        raw_results = slskd.search(query)
        candidates = slskd.filter_candidates(
            raw_results,
            accepted_formats=lib_cfg.get("accepted_formats", ["flac", "mp3"]),
            min_mp3_bitrate=lib_cfg.get("min_mp3_bitrate", 320),
            prefer_flac=lib_cfg.get("prefer_flac", True),
            album_hint=track.album,
        )

        if not candidates:
            logger.warning("[✗ MISSING  ] %s — no suitable results on Soulseek", label)
            failed += 1
            continue

        best = candidates[0]
        fmt_str = best.extension.upper()
        if best.bitrate:
            fmt_str += f" {best.bitrate} kbps"
        logger.info(
            "[↓ DOWNLOAD ] %s  ←  %s  (%s)",
            best.filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
            best.username,
            fmt_str,
        )

        # ------------------------------------------------------------------
        # Stage 4 — Queue and await the download
        # ------------------------------------------------------------------
        slskd.queue_download(best)
        local_path = slskd.wait_for_download(
            username=best.username,
            filename=best.filename,
            poll_interval=lib_cfg.get("download_poll_interval", 5),
            timeout=lib_cfg.get("download_timeout", 600),
        )

        if local_path is None or not local_path.exists():
            logger.error("[✗ DL FAIL  ] %s", label)
            failed += 1
            continue

        # ------------------------------------------------------------------
        # Stage 5 — Move file into Jellyfin library structure
        # ------------------------------------------------------------------
        try:
            dest = lib_mgr.move_to_library(local_path, track.artist, track.album)
            logger.info("[→ MOVED    ] %s", dest)
        except OSError as exc:
            logger.error("[✗ MOVE FAIL] %s — %s", label, exc)
            failed += 1
            continue

        # ------------------------------------------------------------------
        # Stage 6 — Refresh Jellyfin, wait for scan, add to playlist
        # ------------------------------------------------------------------
        jellyfin.refresh_library()
        scan_wait: int = lib_cfg.get("scan_wait", 15)
        logger.info("[⟳ SCANNING ] Waiting %ds for Jellyfin to index new file...", scan_wait)
        time.sleep(scan_wait)

        jf_id = jellyfin.find_track_by_name_artist(track.title, track.artist)
        if jf_id:
            jellyfin.add_to_playlist(playlist_id, [jf_id])
            logger.info("[✓ ADDED    ] %s", label)
            downloaded += 1
        else:
            logger.warning(
                "[⚠ NO INDEX ] %s — file downloaded but not yet visible in Jellyfin", label
            )
            failed += 1

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total = matched + downloaded + failed + skipped
    print(f"\n{'=' * 52}")
    print(f"  Processed              : {total}")
    print(f"  Matched in library     : {matched}")
    print(f"  Downloaded & added     : {downloaded}")
    print(f"  Failed / not found     : {failed}")
    print(f"  Skipped (--no-download): {skipped}")
    print(f"{'=' * 52}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync a Spotify playlist to Jellyfin, acquiring missing tracks via slskd.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py \\\n"
            "      --spotify-playlist 'https://open.spotify.com/playlist/37i9dQZF1DX...' \\\n"
            "      --jellyfin-playlist 'My Mix'\n\n"
            "  python main.py --spotify-playlist <id> --jellyfin-playlist <name> --dry-run\n"
            "  python main.py --spotify-playlist <id> --jellyfin-playlist <name> --no-download\n"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--spotify-playlist",
        required=True,
        help="Spotify playlist URL, URI, or ID",
    )
    parser.add_argument(
        "--jellyfin-playlist",
        required=True,
        help="Target Jellyfin playlist name (created if absent) or UUID",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print track list only; make no changes to Jellyfin or slskd",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Only match tracks already in the library; skip slskd entirely",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)

    try:
        run(args, cfg)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
