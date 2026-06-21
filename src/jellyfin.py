"""Jellyfin API client for library search and playlist management."""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from .spotify import SpotifyTrack

logger = logging.getLogger(__name__)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class JellyfinClient:
    def __init__(self, url: str, api_key: str, user_id: str) -> None:
        self.url = url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "X-Emby-Token": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self.user_id = self._resolve_user_id(user_id)

    def _resolve_user_id(self, user_id_or_name: str) -> str:
        """Accept either a Jellyfin user ID or a username and return the canonical ID."""
        try:
            resp = self._session.get(f"{self.url}/Users")
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Failed to resolve Jellyfin user '%s': %s", user_id_or_name, exc)
            return user_id_or_name

        wanted = user_id_or_name.strip().lower()
        for user in resp.json():
            user_name = str(user.get("Name", "")).strip().lower()
            user_id = str(user.get("Id", "")).strip().lower()
            if wanted == user_name or wanted == user_id:
                resolved = str(user.get("Id", user_id_or_name))
                if resolved.lower() != wanted:
                    logger.info("Resolved Jellyfin user '%s' to id '%s'", user_id_or_name, resolved)
                return resolved

        logger.warning("Jellyfin user '%s' was not found; using it as-is", user_id_or_name)
        return user_id_or_name

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

        sp_title = _norm(track.title)
        sp_album = _norm(track.album)
        sp_artists = {a.strip().lower() for a in track.artist.split(",")}

        # First pass: strongest match with title + artist + album.
        for item in resp.json().get("Items", []):
            item_name = _norm(item.get("Name", ""))
            item_artists = {a.lower() for a in item.get("Artists", [])}
            item_album = _norm(item.get("Album", ""))
            if item_name == sp_title and (sp_artists & item_artists) and item_album == sp_album:
                return item["Id"]

        # Second pass: title + artist when album metadata differs.
        for item in resp.json().get("Items", []):
            item_name = _norm(item.get("Name", ""))
            item_artists = {a.lower() for a in item.get("Artists", [])}
            if item_name == sp_title and (sp_artists & item_artists):
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

    def get_playlist_item_ids(self, playlist_id: str) -> set[str]:
        """Return the Jellyfin item IDs currently present in a playlist."""
        return {
            str(item.get("Id"))
            for item in self.get_playlist_items(playlist_id)
            if item.get("Id")
        }

    def get_playlist_items(self, playlist_id: str) -> list[dict]:
        """Return the current items in a Jellyfin playlist."""
        try:
            resp = self._session.get(
                f"{self.url}/Playlists/{playlist_id}/Items",
                params={"UserId": self.user_id},
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Failed to read Jellyfin playlist '%s': %s", playlist_id, exc)
            return []

        return resp.json().get("Items", [])

    def add_to_playlist(
        self,
        playlist_id: str,
        item_ids: list[str],
        playlist_name: Optional[str] = None,
    ) -> str:
        if not item_ids:
            return playlist_id

        def _post_add(target_playlist_id: str) -> requests.Response:
            return self._session.post(
                f"{self.url}/Playlists/{target_playlist_id}/Items",
                params={"Ids": ",".join(item_ids), "UserId": self.user_id},
            )

        resp = _post_add(playlist_id)
        if resp.status_code != 404:
            resp.raise_for_status()
            return playlist_id

        if not playlist_name:
            resp.raise_for_status()

        logger.warning(
            "Playlist id '%s' was not found while adding items; recreating '%s' and retrying once",
            playlist_id,
            playlist_name,
        )
        recovered_id = self.get_or_create_playlist(playlist_name)
        retry = _post_add(recovered_id)
        retry.raise_for_status()
        return recovered_id

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
