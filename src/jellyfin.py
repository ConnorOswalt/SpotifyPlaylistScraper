"""Jellyfin API client for library search and playlist management."""

from __future__ import annotations

import logging
from typing import Optional

import requests

from .spotify import SpotifyTrack

logger = logging.getLogger(__name__)


class JellyfinClient:
    def __init__(self, url: str, api_key: str, user_id: str) -> None:
        self.url = url.rstrip("/")
        self.user_id = user_id
        self._session = requests.Session()
        self._session.headers.update(
            {
                "X-Emby-Token": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Library search
    # ------------------------------------------------------------------

    def search_track(self, track: SpotifyTrack) -> Optional[str]:
        """Return a Jellyfin item ID if the track exists in the local library."""
        try:
            resp = self._session.get(
                f"{self.url}/Items",
                params={
                    "searchTerm": track.title,
                    "IncludeItemTypes": "Audio",
                    "Recursive": "true",
                    "UserId": self.user_id,
                    "Fields": "Artists,AlbumArtist",
                    "Limit": 20,
                },
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Jellyfin search failed for '%s': %s", track.title, exc)
            return None

        # Match when any Spotify artist intersects the Jellyfin item's artist list.
        sp_artists = {a.strip().lower() for a in track.artist.split(",")}
        for item in resp.json().get("Items", []):
            item_artists = {a.lower() for a in item.get("Artists", [])}
            if sp_artists & item_artists:
                return item["Id"]

        return None

    def find_track_by_name_artist(self, title: str, artist: str) -> Optional[str]:
        """Post-download search — used after a library refresh to locate a new item."""
        try:
            resp = self._session.get(
                f"{self.url}/Items",
                params={
                    "searchTerm": title,
                    "IncludeItemTypes": "Audio",
                    "Recursive": "true",
                    "UserId": self.user_id,
                    "Fields": "Artists",
                    "Limit": 20,
                },
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Post-download Jellyfin search failed: %s", exc)
            return None

        sp_artists = {a.strip().lower() for a in artist.split(",")}
        for item in resp.json().get("Items", []):
            item_artists = {a.lower() for a in item.get("Artists", [])}
            if sp_artists & item_artists:
                return item["Id"]

        return None

    # ------------------------------------------------------------------
    # Playlist management
    # ------------------------------------------------------------------

    def get_or_create_playlist(self, name: str) -> str:
        """Return the ID of a named playlist, creating it if it does not exist."""
        resp = self._session.get(
            f"{self.url}/Items",
            params={
                "searchTerm": name,
                "IncludeItemTypes": "Playlist",
                "Recursive": "true",
                "UserId": self.user_id,
                "Limit": 20,
            },
        )
        resp.raise_for_status()

        for item in resp.json().get("Items", []):
            if item["Name"].lower() == name.lower():
                logger.info("Using existing playlist: %s (%s)", name, item["Id"])
                return item["Id"]

        # Playlist not found — create it.
        resp = self._session.post(
            f"{self.url}/Playlists",
            params={
                "Name": name,
                "UserId": self.user_id,
                "MediaType": "Audio",
            },
        )
        resp.raise_for_status()
        playlist_id: str = resp.json()["Id"]
        logger.info("Created new playlist: %s (%s)", name, playlist_id)
        return playlist_id

    def add_to_playlist(self, playlist_id: str, item_ids: list[str]) -> None:
        if not item_ids:
            return
        resp = self._session.post(
            f"{self.url}/Playlists/{playlist_id}/Items",
            params={"Ids": ",".join(item_ids), "UserId": self.user_id},
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Library refresh
    # ------------------------------------------------------------------

    def refresh_library(self) -> None:
        """Trigger an incremental Jellyfin library scan."""
        try:
            resp = self._session.post(f"{self.url}/Library/Refresh")
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Library refresh request failed: %s", exc)
