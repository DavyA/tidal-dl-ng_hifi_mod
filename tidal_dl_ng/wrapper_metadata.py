from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Iterable

import json
import requests
from tidalapi.artist import Role

from tidal_dl_ng.constants import CoverDimensions, REQUESTS_TIMEOUT_SEC
from tidal_dl_ng.constants import MediaType
from tidal_dl_ng.wrapper_api import BASE_URL, request_with_retry
from tidal_dl_ng.wrapper_api import WrapperApiError


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
    mix_name: str | None = None

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


@dataclass(slots=True)
class WrapperMix:
    id: str
    name: str


def _request(endpoint: str, params: dict[str, str | int]) -> dict | list:
    resp, error = request_with_retry(endpoint, params)
    if resp and resp.status_code == 200:
        return resp.json()

    if resp:
        msg = f"HTTP {resp.status_code}"
    else:
        msg = error or "network error"

    raise WrapperMetadataError(f"Failed to retrieve metadata from {endpoint}: {msg}")


def _get_json_response(endpoint: str, params: dict[str, str | int]) -> tuple[int, dict | list | None]:
    """Perform a GET request and return (status_code, payload)."""

    resp, _ = request_with_retry(endpoint, params)
    if resp:
        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, None
    
    # If generic retry failed completely (e.g. max retries exceeded for 500s),
    # we simulate a 503 or return 0 to indicate failure, but raising might be better.
    # For now, let's raise to be safe or return 503 so logic downstream handles it.
    raise WrapperMetadataError(f"Failed to retrieve track metadata: network error")


def _extract_track_id(payload: dict | list | None) -> int | None:
    """Try to extract a track identifier from various payload shapes."""
    if isinstance(payload, dict):
        data = payload.get("data") if "data" in payload else payload
        if isinstance(data, dict):
            return data.get("trackId") or data.get("id")

    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict):
                tid = entry.get("trackId") or entry.get("id")
                if tid:
                    return tid
    return None


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


def _normalize_album_fragment(
    fragment: dict | None, track_data: dict | None = None, total_tracks: int | None = None
) -> dict:
    """Fill missing album fields using track-level data or collection counts."""

    normalized = dict(fragment or {})

    if track_data:
        stream_start = track_data.get("streamStartDate")
        if stream_start and isinstance(stream_start, str):
            normalized.setdefault("streamStartDate", stream_start)
            normalized.setdefault("releaseDate", stream_start.split("T", 1)[0])

        if (allow_streaming := track_data.get("allowStreaming")) is not None:
            normalized.setdefault("allowStreaming", allow_streaming)

        if (explicit := track_data.get("explicit")) is not None:
            normalized.setdefault("explicit", explicit)

        if (volume_number := track_data.get("volumeNumber")):
            normalized.setdefault("numberOfVolumes", volume_number)

    if total_tracks is not None and not normalized.get("numberOfTracks"):
        normalized["numberOfTracks"] = total_tracks

    return normalized


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
    status, payload = _get_json_response("info", {"id": track_id})

    # Wrapper sometimes knows a different trackId; try to resolve via /track when /info says 404.
    if status == 404:
        track_status, track_payload = _get_json_response("track", {"id": track_id})
        mapped_track_id = _extract_track_id(track_payload) if track_status < 400 else None
        if mapped_track_id and str(mapped_track_id) != str(track_id):
            status, payload = _get_json_response("info", {"id": mapped_track_id})
            track_id = mapped_track_id

    if status >= 400 or payload is None:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or "")
        raise WrapperMetadataError(f"Failed to retrieve track metadata: {detail or f'HTTP {status}'}")

    if isinstance(payload, dict) and payload.get("detail"):
        raise WrapperMetadataError(str(payload.get("detail")))

    if isinstance(payload, dict):
        track_data = payload.get("data") or {}
    elif isinstance(payload, list) and payload:
        track_data = payload[0]
    else:
        track_data = {}

    if not isinstance(track_data, dict) or not track_data:
        raise WrapperMetadataError("Unexpected track payload structure.")

    album_ref = track_data.get("album") or {}
    album_id = album_ref.get("id")

    if album_id is None:
        raise WrapperMetadataError("Track payload is missing album information.")

    album_fragment = _normalize_album_fragment(album_ref, track_data)
    album_fragment.setdefault("id", album_id)
    album = _build_album(album_fragment)

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

    album_data: dict | None = None
    items: list = []

    if isinstance(payload, dict):
        if payload.get("detail"):
            raise WrapperMetadataError(str(payload.get("detail")))

        data_section = payload.get("data") or {}
        raw_items = data_section.get("items") or []
        track_entries: list[dict] = []

        for entry in raw_items:
            track_data = entry.get("item") if isinstance(entry, dict) else entry
            if isinstance(track_data, dict):
                track_entries.append(track_data)

        if track_entries:
            total_tracks = data_section.get("totalNumberOfItems")
            album_fragment = track_entries[0].get("album") or {}
            album_data = _normalize_album_fragment(album_fragment, track_entries[0], total_tracks)
            album_data.setdefault("id", album_id)
            if total_tracks:
                album_data.setdefault("numberOfTracks", total_tracks)
            album_data.setdefault("numberOfVolumes", max((track.get("volumeNumber") or 1) for track in track_entries))
            if not album_data.get("explicit"):
                album_data["explicit"] = any(bool(track.get("explicit")) for track in track_entries)

        items = track_entries

    elif isinstance(payload, list) and len(payload) >= 2:
        album_data = payload[0] or {}
        items_section = payload[1] or {}
        raw_items = items_section.get("items") or []
        items = []
        for entry in raw_items:
            track_data = entry.get("item") if isinstance(entry, dict) else entry
            if isinstance(track_data, dict):
                items.append(track_data)

    if album_data is None:
        raise WrapperMetadataError("Unexpected album payload structure.")

    album = _build_album(album_data)

    tracks: list[WrapperTrack] = []

    for entry in items:
        track_data = entry.get("item") if isinstance(entry, dict) else entry
        if track_data is None and isinstance(entry, dict):
            track_data = entry
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

    if isinstance(payload, dict) and payload.get("detail"):
        raise WrapperMetadataError(str(payload.get("detail")))

    if isinstance(payload, dict):
        playlist_info = payload.get("playlist") or {}
        items = payload.get("items") or []
    elif isinstance(payload, list) and len(payload) >= 2:
        playlist_info = payload[0] or {}
        items_section = payload[1] or {}
        items = items_section.get("items") or []
    else:
        raise WrapperMetadataError("Unexpected playlist payload structure.")

    playlist = WrapperPlaylist(
        uuid=playlist_info.get("uuid") or str(playlist_uuid),
        name=playlist_info.get("title", "Unknown Playlist"),
        description=playlist_info.get("description"),
    )

    tracks: list[WrapperTrack] = []

    for entry in items:
        track_data = entry.get("item") or entry
        if not isinstance(track_data, dict):
            continue

        track_artists = _map_artists(track_data.get("artists") or [])
        album_fragment = _normalize_album_fragment(track_data.get("album"), track_data)
        album = _build_album_from_fragment(album_fragment, track_artists)

        track = _build_track(track_data, album)
        tracks.append(track)

    return playlist, tracks


def _mix_name_from_html(mix_id: str) -> str:
    try:
        resp = requests.get(f"https://tidal.com/mix/{mix_id}", timeout=REQUESTS_TIMEOUT_SEC)
        resp.raise_for_status()
        html = resp.text
        start = html.find("<title>")
        end = html.find("</title>", start + 7)
        if start != -1 and end != -1:
            return html[start + 7 : end].strip() or f"Mix {mix_id}"
    except Exception:
        pass

    return f"Mix {mix_id}"


def fetch_mix_with_tracks(mix_id: str) -> tuple[WrapperMix, list[WrapperTrack]]:
    try:
        payload = _request("mix", {"id": mix_id})
    except requests.RequestException as exc:
        raise WrapperMetadataError(f"Failed to retrieve mix data: {exc}") from exc

    if not isinstance(payload, dict):
        raise WrapperMetadataError("Unexpected mix payload structure.")

    mix = WrapperMix(id=mix_id, name=_mix_name_from_html(mix_id))
    tracks: list[WrapperTrack] = []

    for entry in payload.get("items", []):
        track_data = entry.get("item") or entry
        if not isinstance(track_data, dict):
            continue

        track_artists = _map_artists(track_data.get("artists") or [])
        album_fragment = track_data.get("album") or {}
        album = _build_album_from_fragment(album_fragment, track_artists)

        track = _build_track(track_data, album)
        track.mix_name = mix.name
        tracks.append(track)

    return mix, tracks


def instantiate_media_wrapper(media_type: MediaType, media_id: str) -> WrapperTrack:
    if media_type == MediaType.TRACK:
        return fetch_track_metadata(media_id)

    raise NotImplementedError
