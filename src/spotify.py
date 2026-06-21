"""Spotify playlist fetcher using spotipy."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

logger = logging.getLogger(__name__)

_SCOPE = "playlist-read-private playlist-read-collaborative"


@dataclass
class SpotifyTrack:
    title: str
    artist: str
    album: str
    isrc: Optional[str]
    duration_ms: int
    spotify_id: str


@dataclass
class SpotifyPlaylist:
    spotify_id: str
    name: str
    owner_id: str
    total_tracks: int


class SpotifyFetcher:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        cache_path: str = ".spotify_cache",
    ) -> None:
        self._sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scope=_SCOPE,
                cache_path=cache_path,
                open_browser=True,
            )
        )

    def get_playlist_tracks(self, playlist_id: str) -> list[SpotifyTrack]:
        """Fetch all tracks from a playlist, including ISRC and album metadata."""
        tracks: list[SpotifyTrack] = []

        try:
            results = self._sp.playlist_items(
                playlist_id,
                additional_types=["track"],
            )
        except SpotifyException as exc:
            if exc.http_status == 404:
                raise RuntimeError(
                    f"Spotify playlist not found or inaccessible: {playlist_id}. "
                    "Verify the playlist URL/ID and ensure it is visible to the authenticated account."
                ) from exc
            raise

        while results:
            for item in results.get("items", []):
                # Spotify has returned both `track` (legacy) and `item` (newer)
                # playlist entry shapes over time; support both.
                raw = item.get("track") or item.get("item")
                if not raw or not raw.get("id"):
                    # Local tracks or null entries have no Spotify ID — skip them.
                    continue
                if raw.get("type") and raw.get("type") != "track":
                    # Ignore podcast episodes and any non-track item types.
                    continue

                artists = ", ".join(a["name"] for a in raw.get("artists", []))
                album = (raw.get("album") or {}).get("name", "Unknown Album")
                tracks.append(
                    SpotifyTrack(
                        title=raw["name"],
                        artist=artists,
                        album=album,
                        isrc=raw.get("external_ids", {}).get("isrc"),
                        duration_ms=raw.get("duration_ms", 0),
                        spotify_id=raw["id"],
                    )
                )

            results = self._sp.next(results) if results.get("next") else None

        logger.info("Fetched %d tracks from Spotify playlist", len(tracks))
        return tracks

    def get_current_user_playlists(self) -> list[SpotifyPlaylist]:
        """Return all playlists visible to the authenticated Spotify user."""
        playlists: list[SpotifyPlaylist] = []

        results = self._sp.current_user_playlists(limit=50)
        while results:
            for item in results.get("items", []):
                if not item or not item.get("id"):
                    continue
                playlists.append(
                    SpotifyPlaylist(
                        spotify_id=item["id"],
                        name=item.get("name", "Untitled Playlist"),
                        owner_id=(item.get("owner") or {}).get("id", ""),
                        total_tracks=(item.get("tracks") or {}).get("total", 0),
                    )
                )

            results = self._sp.next(results) if results.get("next") else None

        logger.info("Fetched %d playlists from Spotify account", len(playlists))
        return playlists
