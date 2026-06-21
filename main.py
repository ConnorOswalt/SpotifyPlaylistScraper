#!/usr/bin/env python3
"""Sync a Spotify playlist to Jellyfin, acquiring missing tracks via slskd."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import re
import sys
import time

import yaml

from src.jellyfin import JellyfinClient
from src.library import LibraryManager
from src.postprocess import PicardPostProcessor
from src.slskd import SlskdClient
from src.spotify import SpotifyFetcher, SpotifyTrack

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


def _looks_like_placeholder(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    return (
        lowered.startswith("your_")
        or "youridhere" in lowered
        or text.startswith("#")
        or text.endswith("#")
    )


def validate_config(cfg: dict) -> None:
    spotify_cfg = cfg.get("spotify", {})
    if _looks_like_placeholder(spotify_cfg.get("client_id")):
        raise ValueError("spotify.client_id is not set to a real Spotify app client ID.")
    if _looks_like_placeholder(spotify_cfg.get("client_secret")):
        raise ValueError("spotify.client_secret is not set to a real Spotify app client secret.")


def resolve_slskd_api_key(cfg: dict) -> str:
    """Use config.yaml by default, but fall back to local slskd.yml for placeholder values."""
    configured = str(cfg["slskd"].get("api_key", "")).strip()
    if configured and not configured.startswith("YOUR_"):
        return configured

    slskd_config_path = Path("slskd-config") / "slskd.yml"
    if not slskd_config_path.exists():
        return configured

    try:
        with open(slskd_config_path, encoding="utf-8") as fh:
            slskd_cfg = yaml.safe_load(fh) or {}
    except OSError:
        return configured

    api_keys = (
        slskd_cfg.get("web", {})
        .get("authentication", {})
        .get("api_keys", {})
    )
    for entry in api_keys.values():
        key = str((entry or {}).get("key", "")).strip()
        if key:
            logger.info("Using API key from local slskd-config/slskd.yml")
            return key

    return configured


def resolve_playlist_id(jellyfin: JellyfinClient, name_or_id: str) -> str:
    """Return a Jellyfin playlist ID.

    If *name_or_id* looks like a UUID it is returned as-is; otherwise the
    playlist is looked up by name and created if it does not yet exist.
    """
    if _UUID_RE.match(name_or_id):
        return name_or_id
    return jellyfin.get_or_create_playlist(name_or_id)


def process_tracks_for_playlist(
    jellyfin: JellyfinClient,
    slskd: SlskdClient,
    lib_mgr: LibraryManager,
    postprocessor: PicardPostProcessor,
    lib_cfg: dict,
    tracks: list[SpotifyTrack],
    jellyfin_playlist_id: str,
    no_download: bool,
) -> tuple[int, int, int, int]:
    matched = downloaded = failed = skipped = 0

    for track in tracks:
        label = f"{track.artist} - {track.title}"

        jf_id = jellyfin.search_track(track)
        if jf_id:
            logger.info("[LIBRARY   ] %s", label)
            jellyfin.add_to_playlist(jellyfin_playlist_id, [jf_id])
            matched += 1
            continue

        if no_download:
            logger.info("[SKIP      ] %s", label)
            skipped += 1
            continue

        query = f"{track.artist} - {track.title}"
        logger.info("[SEARCH    ] %s", label)

        try:
            raw_results = slskd.search(query)
        except RuntimeError as exc:
            logger.error("[SEARCH FAIL] %s - %s", label, exc)
            failed += 1
            continue

        candidates = slskd.filter_candidates(
            raw_results,
            accepted_formats=lib_cfg.get("accepted_formats", ["flac", "mp3"]),
            min_mp3_bitrate=lib_cfg.get("min_mp3_bitrate", 320),
            prefer_flac=lib_cfg.get("prefer_flac", True),
            album_hint=track.album,
        )

        if not candidates:
            fallback_min_bitrate = lib_cfg.get("fallback_min_mp3_bitrate", 192)
            strict_min_bitrate = lib_cfg.get("min_mp3_bitrate", 320)
            if fallback_min_bitrate < strict_min_bitrate:
                logger.info(
                    "[FALLBACK  ] %s - relaxing MP3 bitrate floor from %s to %s",
                    label,
                    strict_min_bitrate,
                    fallback_min_bitrate,
                )
                candidates = slskd.filter_candidates(
                    raw_results,
                    accepted_formats=lib_cfg.get("accepted_formats", ["flac", "mp3"]),
                    min_mp3_bitrate=fallback_min_bitrate,
                    prefer_flac=lib_cfg.get("prefer_flac", True),
                    album_hint=track.album,
                )

        if not candidates:
            logger.warning("[MISSING   ] %s - no suitable results on Soulseek", label)
            failed += 1
            continue

        best = candidates[0]
        fmt_str = best.extension.upper()
        if best.bitrate:
            fmt_str += f" {best.bitrate} kbps"
        logger.info(
            "[DOWNLOAD  ] %s  <-  %s  (%s)",
            best.filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
            best.username,
            fmt_str,
        )

        slskd.queue_download(best)
        local_path = slskd.wait_for_download(
            username=best.username,
            filename=best.filename,
            poll_interval=lib_cfg.get("download_poll_interval", 5),
            timeout=lib_cfg.get("download_timeout", 600),
        )

        if local_path is None or not local_path.exists():
            logger.error("[DL FAIL   ] %s", label)
            failed += 1
            continue

        try:
            dest = lib_mgr.move_to_library(local_path, track.artist, track.album)
            logger.info("[MOVED     ] %s", dest)
        except OSError as exc:
            logger.error("[MOVE FAIL ] %s - %s", label, exc)
            failed += 1
            continue

        post_ok = postprocessor.process(dest, track)
        if not post_ok:
            logger.error("[VERIFY FAIL] %s - metadata verification/standardization failed", label)
            failed += 1
            continue

        jellyfin.refresh_library()
        scan_wait: int = lib_cfg.get("scan_wait", 15)
        logger.info("[SCANNING  ] Waiting %ds for Jellyfin to index new file...", scan_wait)
        time.sleep(scan_wait)

        jf_id = jellyfin.find_track_by_name_artist(track.title, track.artist)
        if jf_id:
            jellyfin.add_to_playlist(jellyfin_playlist_id, [jf_id])
            logger.info("[ADDED     ] %s", label)
            downloaded += 1
        else:
            logger.warning("[NO INDEX  ] %s - downloaded but not visible in Jellyfin yet", label)
            failed += 1

    return matched, downloaded, failed, skipped


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
        api_key=resolve_slskd_api_key(cfg),
        download_dir=cfg["slskd"]["download_dir"],
        search_timeout=cfg["slskd"].get("search_timeout", 30),
        result_limit=cfg["slskd"].get("result_limit", 100),
    )
    lib_mgr = LibraryManager(music_dir=cfg["library"]["music_dir"])
    lib_cfg: dict = cfg["library"]
    picard_cfg: dict = cfg.get("picard", {})
    postprocessor = PicardPostProcessor(
        enabled=picard_cfg.get("enabled", False),
        command_template=picard_cfg.get("command_template", ""),
        timeout=picard_cfg.get("timeout", 180),
        verify_artist_title=picard_cfg.get("verify_artist_title", True),
        require_success=picard_cfg.get("require_success", False),
    )

    if args.sync_all_playlists:
        source_playlists = spotify.get_current_user_playlists()
        if args.playlist_name_contains:
            needle = args.playlist_name_contains.lower()
            source_playlists = [
                p for p in source_playlists if needle in p.name.lower()
            ]

        logger.info("Discovered %d Spotify playlists for sync", len(source_playlists))

        grand_matched = grand_downloaded = grand_failed = grand_skipped = 0
        for source in source_playlists:
            logger.info("Syncing playlist: %s", source.name)
            tracks = spotify.get_playlist_tracks(source.spotify_id)

            if args.dry_run:
                print(f"\nPlaylist: {source.name} ({len(tracks)} tracks)")
                for t in tracks:
                    isrc = t.isrc or "no ISRC"
                    print(f"  {t.artist} - {t.title}  |  {t.album}  |  {isrc}")
                continue

            playlist_id = resolve_playlist_id(jellyfin, source.name)
            matched, downloaded, failed, skipped = process_tracks_for_playlist(
                jellyfin=jellyfin,
                slskd=slskd,
                lib_mgr=lib_mgr,
                postprocessor=postprocessor,
                lib_cfg=lib_cfg,
                tracks=tracks,
                jellyfin_playlist_id=playlist_id,
                no_download=args.no_download,
            )
            grand_matched += matched
            grand_downloaded += downloaded
            grand_failed += failed
            grand_skipped += skipped

        if args.dry_run:
            return

        total = grand_matched + grand_downloaded + grand_failed + grand_skipped
        print(f"\n{'=' * 52}")
        print(f"  Processed              : {total}")
        print(f"  Matched in library     : {grand_matched}")
        print(f"  Downloaded & added     : {grand_downloaded}")
        print(f"  Failed / not found     : {grand_failed}")
        print(f"  Skipped (--no-download): {grand_skipped}")
        print(f"{'=' * 52}")
        return

    logger.info("Fetching Spotify playlist: %s", args.spotify_playlist)
    tracks = spotify.get_playlist_tracks(args.spotify_playlist)
    logger.info("Total tracks: %d", len(tracks))

    if args.dry_run:
        preview_tracks = tracks[:10]
        print(f"Showing first {len(preview_tracks)} of {len(tracks)} tracks:")
        for t in preview_tracks:
            isrc = t.isrc or "no ISRC"
            print(f"  {t.artist} - {t.title}  |  {t.album}  |  {isrc}")
        return

    playlist_id = resolve_playlist_id(jellyfin, args.jellyfin_playlist)
    logger.info("Target Jellyfin playlist: %s", playlist_id)

    matched, downloaded, failed, skipped = process_tracks_for_playlist(
        jellyfin=jellyfin,
        slskd=slskd,
        lib_mgr=lib_mgr,
        postprocessor=postprocessor,
        lib_cfg=lib_cfg,
        tracks=tracks,
        jellyfin_playlist_id=playlist_id,
        no_download=args.no_download,
    )

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
            "  python main.py --spotify-playlist <id> --dry-run\n"
            "  python main.py --spotify-playlist <id> --jellyfin-playlist <name> --no-download\n"
            "  python main.py --sync-all-playlists\n"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--spotify-playlist",
        help="Spotify playlist URL, URI, or ID",
    )
    parser.add_argument(
        "--jellyfin-playlist",
        help="Target Jellyfin playlist name (created if absent) or UUID",
    )
    parser.add_argument(
        "--sync-all-playlists",
        action="store_true",
        help="Read all playlists from the authenticated Spotify account and mirror each by name to Jellyfin",
    )
    parser.add_argument(
        "--playlist-name-contains",
        help="Optional substring filter when using --sync-all-playlists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For a single Spotify playlist, print only the first 10 tracks and make no changes",
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

    if not args.sync_all_playlists:
        if not args.spotify_playlist:
            parser.error(
                "Pass --spotify-playlist for single-playlist mode, or use --sync-all-playlists."
            )
        if not args.dry_run and not args.jellyfin_playlist:
            parser.error(
                "--jellyfin-playlist is required unless using --dry-run."
            )

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    validate_config(cfg)

    try:
        run(args, cfg)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
