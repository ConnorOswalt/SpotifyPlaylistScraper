"""Spotify playlist fetcher using spotipy."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import spotipy
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

        results = self._sp.playlist_items(
            playlist_id,
            fields=(
                "next,"
                "items(track(id,name,artists(name),album(name),"
                "external_ids(isrc),duration_ms))"
            ),
            additional_types=["track"],
        )

        while results:
            for item in results.get("items", []):
                raw = item.get("track")
                if not raw or not raw.get("id"):
                    # Local tracks or null entries have no Spotify ID — skip them.
                    continue

                artists = ", ".join(a["name"] for a in raw.get("artists", []))
                tracks.append(
                    SpotifyTrack(
                        title=raw["name"],
                        artist=artists,
                        album=raw["album"]["name"],
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
