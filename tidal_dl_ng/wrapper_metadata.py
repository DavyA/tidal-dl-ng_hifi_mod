from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Iterable

import requests
from tidalapi.artist import Role

from tidal_dl_ng.constants import CoverDimensions, REQUESTS_TIMEOUT_SEC
from tidal_dl_ng.constants import MediaType
from tidal_dl_ng.wrapper_api import BASE_URL


class WrapperMetadataError(Exception):
    """Raised when metadata cannot be retrieved from the wrapper API."""


@dataclass(slots=True)
class WrapperLyrics:
    subtitles: list[str] | None = None
    text: str = ""


@dataclass(slots=True)
class WrapperArtist:
    id: int
    name: str
    roles: list[Role]


@dataclass(slots=True)
class WrapperAlbum:
    id: int
    name: str
    cover: str
    num_tracks: int
    num_volumes: int
    upc: str | None
    release_date: datetime.datetime | None
    available_release_date: datetime.datetime | None
    artists: list[WrapperArtist]
    explicit: bool
    allow_streaming: bool

    def image(self, size: int | CoverDimensions) -> str:
        if isinstance(size, CoverDimensions):
            size = int(size)

        cover_id = self.cover.replace("-", "/")
        return f"https://resources.tidal.com/images/{cover_id}/{size}x{size}.jpg"

    @property
    def year(self) -> int | None:
        return self.release_date.year if self.release_date else None


@dataclass(slots=True)
class WrapperTrack:
    id: int
    title: str
    duration: int
    track_num: int
    volume_num: int
    copyright: str | None
    isrc: str | None
    album: WrapperAlbum
    artists: list[WrapperArtist]
    media_metadata_tags: list[str]
    share_url: str
    audio_quality: str
    explicit: bool
    available: bool
    playlist_name: str | None = None

    @property
    def name(self) -> str:
        return self.title

    @property
    def full_name(self) -> str:
        return self.title

    def lyrics(self) -> WrapperLyrics:
        return WrapperLyrics()


@dataclass(slots=True)
class WrapperPlaylist:
    uuid: str
    name: str
    description: str | None = None

    @property
    def id(self) -> str:
        return self.uuid

    @property
    def title(self) -> str:
        return self.name


def _request(endpoint: str, params: dict[str, str | int]) -> dict | list:
    response = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=REQUESTS_TIMEOUT_SEC)
    response.raise_for_status()
    return response.json()


def _parse_date(date_str: str | None, date_format: str) -> datetime.datetime | None:
    if not date_str:
        return None

    try:
        return datetime.datetime.strptime(date_str, date_format)
    except ValueError:
        return None


def _map_artists(artists: Iterable[dict]) -> list[WrapperArtist]:
    result: list[WrapperArtist] = []

    for artist in artists or []:
        artist_type = (artist.get("type") or "MAIN").lower()
        if artist_type == "featured":
            role = Role.featured
        elif artist_type == "composer":
            role = Role.composer
        else:
            role = Role.main

        result.append(
            WrapperArtist(
                id=artist.get("id", 0),
                name=artist.get("name", "Unknown Artist"),
                roles=[role],
            )
        )

    return result


def _build_album(album_data: dict) -> WrapperAlbum:
    artists = _map_artists(album_data.get("artists") or [])

    release_date = _parse_date(album_data.get("releaseDate"), "%Y-%m-%d")
    available_release_date = _parse_date(album_data.get("streamStartDate"), "%Y-%m-%dT%H:%M:%S.%f%z")

    return WrapperAlbum(
        id=album_data.get("id"),
        name=album_data.get("title", "Unknown Album"),
        cover=album_data.get("cover", ""),
        num_tracks=album_data.get("numberOfTracks") or 0,
        num_volumes=album_data.get("numberOfVolumes") or 1,
        upc=album_data.get("upc"),
        release_date=release_date,
        available_release_date=available_release_date or release_date,
        artists=artists,
        explicit=album_data.get("explicit", False),
        allow_streaming=album_data.get("allowStreaming", True),
    )


def _build_track(track_data: dict, album: WrapperAlbum) -> WrapperTrack:
    artists = _map_artists(track_data.get("artists") or [])

    media_metadata_tags = []
    if track_metadata := track_data.get("mediaMetadata"):
        media_metadata_tags = track_metadata.get("tags") or []

    return WrapperTrack(
        id=track_data.get("id"),
        title=track_data.get("title", "Unknown Track"),
        duration=track_data.get("duration") or 0,
        track_num=track_data.get("trackNumber") or 0,
        volume_num=track_data.get("volumeNumber") or 1,
        copyright=track_data.get("copyright"),
        isrc=track_data.get("isrc"),
        album=album,
        artists=artists or album.artists,
        media_metadata_tags=media_metadata_tags,
        share_url=track_data.get("url", ""),
        audio_quality=track_data.get("audioQuality", ""),
        explicit=track_data.get("explicit", False),
        available=track_data.get("allowStreaming", True),
    )


def fetch_track_metadata(track_id: str | int) -> WrapperTrack:
    """Retrieve track metadata using the wrapper API."""
    try:
        payload = _request("track", {"id": track_id})
    except requests.RequestException as exc:
        raise WrapperMetadataError(f"Failed to retrieve track metadata: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise WrapperMetadataError("Unexpected track payload structure.")

    track_data: dict = payload[0]
    album_ref = track_data.get("album") or {}
    album_id = album_ref.get("id")

    if album_id is None:
        raise WrapperMetadataError("Track payload is missing album information.")

    try:
        album_payload = _request("album", {"id": album_id})
    except requests.RequestException as exc:
        raise WrapperMetadataError(f"Failed to retrieve album metadata: {exc}") from exc

    if not isinstance(album_payload, list) or not album_payload:
        raise WrapperMetadataError("Unexpected album payload structure.")

    album_data = album_payload[0]
    album = _build_album(album_data)

    return _build_track(track_data, album)


def _build_album_from_fragment(fragment: dict, fallback_artists: Iterable[WrapperArtist]) -> WrapperAlbum:
    artists = _map_artists(fragment.get("artists") or []) or list(fallback_artists)
    release_date = _parse_date(fragment.get("releaseDate"), "%Y-%m-%d")

    return WrapperAlbum(
        id=fragment.get("id") or 0,
        name=fragment.get("title", "Unknown Album"),
        cover=fragment.get("cover", ""),
        num_tracks=fragment.get("numberOfTracks") or 0,
        num_volumes=fragment.get("numberOfVolumes") or 1,
        upc=fragment.get("upc"),
        release_date=release_date,
        available_release_date=release_date,
        artists=artists,
        explicit=fragment.get("explicit", False),
        allow_streaming=fragment.get("allowStreaming", True),
    )


def fetch_album_with_tracks(album_id: str | int) -> tuple[WrapperAlbum, list[WrapperTrack]]:
    try:
        payload = _request("album", {"id": album_id})
    except requests.RequestException as exc:
        raise WrapperMetadataError(f"Failed to retrieve album metadata: {exc}") from exc

    if not isinstance(payload, list) or len(payload) < 2:
        raise WrapperMetadataError("Unexpected album payload structure.")

    album_data = payload[0] or {}
    items_section = payload[1] or {}
    album = _build_album(album_data)

    tracks: list[WrapperTrack] = []

    for entry in items_section.get("items", []):
        track_data = entry.get("item") or entry
        if not isinstance(track_data, dict):
            continue

        track = _build_track(track_data, album)
        tracks.append(track)

    return album, tracks


def fetch_playlist_with_tracks(playlist_uuid: str) -> tuple[WrapperPlaylist, list[WrapperTrack]]:
    try:
        payload = _request("playlist", {"id": playlist_uuid})
    except requests.RequestException as exc:
        raise WrapperMetadataError(f"Failed to retrieve playlist metadata: {exc}") from exc

    if not isinstance(payload, list) or len(payload) < 2:
        raise WrapperMetadataError("Unexpected playlist payload structure.")

    playlist_info = payload[0] or {}
    items_section = payload[1] or {}

    playlist = WrapperPlaylist(
        uuid=playlist_info.get("uuid") or str(playlist_uuid),
        name=playlist_info.get("title", "Unknown Playlist"),
        description=playlist_info.get("description"),
    )

    tracks: list[WrapperTrack] = []

    for entry in items_section.get("items", []):
        track_data = entry.get("item") or entry
        if not isinstance(track_data, dict):
            continue

        track_artists = _map_artists(track_data.get("artists") or [])
        album_fragment = track_data.get("album") or {}
        album = _build_album_from_fragment(album_fragment, track_artists)

        track = _build_track(track_data, album)
        tracks.append(track)

    return playlist, tracks


def instantiate_media_wrapper(media_type: MediaType, media_id: str) -> WrapperTrack:
    if media_type == MediaType.TRACK:
        return fetch_track_metadata(media_id)

    raise NotImplementedError
