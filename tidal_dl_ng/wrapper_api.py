from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests
from requests import RequestException
from tidalapi import Quality

from tidal_dl_ng.constants import REQUESTS_TIMEOUT_SEC

BASE_URL = "https://hifi.401658.xyz"
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5


class WrapperApiError(Exception):
    """Raised when the wrapped TIDAL API cannot satisfy a stream request."""


@dataclass(slots=True)
class WrapperStreamManifest:
    """Minimal manifest compatible with tidalapi.StreamManifest usage."""

    urls: list[str]
    file_extension: str
    codecs: str
    is_encrypted: bool = False

    def get_urls(self) -> list[str]:
        return self.urls


@dataclass(slots=True)
class WrappedStream:
    """Subset of tidalapi.Stream attributes used by the downloader."""

    album_replay_gain: float
    album_peak_amplitude: float
    track_replay_gain: float
    track_peak_amplitude: float


QUALITY_STRING_MAP: dict[Quality, str] = {
    Quality.low_96k: "LOW",
    Quality.low_320k: "HIGH",
    Quality.high_lossless: "LOSSLESS",
    Quality.hi_res_lossless: "HI_RES_LOSSLESS",
}

QUALITY_RANK: dict[str, int] = {
    "LOW": 0,
    "HIGH": 1,
    "LOSSLESS": 2,
    "HI_RES_LOSSLESS": 3,
}


def _quality_candidates(quality: Quality) -> list[str | None]:
    """Return fallback chain for a requested quality."""
    candidates: dict[Quality, list[str | None]] = {
        Quality.hi_res_lossless: ["HI_RES_LOSSLESS", None, "LOSSLESS", "HIGH", "LOW"],
        Quality.high_lossless: ["LOSSLESS", None, "HIGH", "LOW"],
        Quality.low_320k: ["HIGH", None, "LOW"],
        Quality.low_96k: ["LOW", None],
    }

    return candidates.get(quality, ["LOSSLESS", None, "HIGH", "LOW"]).copy()


def _decode_manifest(manifest_payload: str) -> dict:
    decoded_bytes = base64.b64decode(manifest_payload)
    return json.loads(decoded_bytes)


def _derive_file_extension(urls: Iterable[str]) -> str:
    for url in urls:
        suffix = Path(urlparse(url).path).suffix
        if suffix:
            return suffix
    # Sensible default for AAC if nothing else is provided.
    return ".m4a"


def _request_with_retry(params: dict[str, str], *, attempts: int = MAX_RETRY_ATTEMPTS):
    """Perform a wrapper request with simple retry/backoff for transient errors."""

    last_error: str | None = None

    for attempt in range(attempts):
        try:
            response = requests.get(
                f"{BASE_URL}/track",
                params=params,
                timeout=REQUESTS_TIMEOUT_SEC,
            )
        except RequestException as exc:
            last_error = f"network error: {exc}"
        else:
            if response.status_code in RETRY_STATUS_CODES:
                last_error = f"HTTP {response.status_code}"
            else:
                return response, None

        if attempt < attempts - 1:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

    return None, last_error or "request failed"


def fetch_track_stream(
    track_id: str | int,
    quality: Quality,
    *,
    logger: Callable[[str], None] | None = None,
) -> tuple[WrapperStreamManifest, WrappedStream, str]:
    """Fetch manifest and stream metadata for a track via the wrapped API.

    Args:
        track_id: TIDAL track identifier.
        quality: Desired audio quality.
        logger: Optional callable accepting a debug/info string.

    Returns:
        Tuple containing the manifest, stream metadata and the quality that was delivered.

    Raises:
        WrapperApiError: If no usable manifest could be retrieved.
    """
    errors: list[str] = []
    params_base = {"id": str(track_id)}
    high_quality_attempted_error = False
    high_quality_not_available = False
    high_quality_candidates = {None, "LOSSLESS"}
    candidates = _quality_candidates(quality)
    best_result: tuple[WrapperStreamManifest, WrappedStream, str] | None = None
    best_rank = -1

    for idx, candidate in enumerate(candidates):
        params = params_base.copy()
        if candidate:
            params["quality"] = candidate

        response, retry_error = _request_with_retry(params)

        if response is None:
            if candidate in high_quality_candidates:
                high_quality_attempted_error = True
            errors.append(f"{candidate or 'DEFAULT'}: {retry_error}")
            continue

        if response.status_code != 200:
            detail_msg: str = ""

            try:
                payload_error = response.json()
            except json.JSONDecodeError:
                payload_error = None

            if isinstance(payload_error, dict):
                detail_msg = str(payload_error.get("detail") or "")

            if response.status_code in (400, 404) and detail_msg and "quality not found" in detail_msg.lower():
                if candidate in high_quality_candidates:
                    high_quality_not_available = True
                errors.append(f"{candidate or 'DEFAULT'}: {detail_msg}")
                continue

            if candidate in high_quality_candidates:
                high_quality_attempted_error = True

            errors.append(
                f"{candidate or 'DEFAULT'}: HTTP {response.status_code}{' ' + detail_msg if detail_msg else ''}"
            )
            continue

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            errors.append(f"{candidate or 'DEFAULT'}: invalid JSON ({exc})")
            if candidate in high_quality_candidates:
                high_quality_attempted_error = True
            continue

        if isinstance(payload, dict):
            detail = payload.get("detail")
            if detail:
                errors.append(f"{candidate or 'DEFAULT'}: {detail}")
                if candidate in high_quality_candidates and "quality not found" in detail.lower():
                    high_quality_not_available = True
                elif candidate in high_quality_candidates:
                    high_quality_attempted_error = True
            continue

        if not isinstance(payload, list) or len(payload) < 2:
            errors.append(f"{candidate or 'DEFAULT'}: unexpected payload")
            if candidate in high_quality_candidates:
                high_quality_attempted_error = True
            continue

        manifest_info = payload[1] or {}
        manifest_encoded = manifest_info.get("manifest")

        if not manifest_encoded:
            errors.append(f"{candidate or 'DEFAULT'}: missing manifest")
            if candidate in high_quality_candidates:
                high_quality_attempted_error = True
            continue

        try:
            manifest_decoded = _decode_manifest(manifest_encoded)
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{candidate or 'DEFAULT'}: manifest decode failed ({exc})")
            if candidate in high_quality_candidates:
                high_quality_attempted_error = True
            continue

        urls: list[str] = manifest_decoded.get("urls", [])
        if not urls:
            errors.append(f"{candidate or 'DEFAULT'}: manifest without URLs")
            if candidate in high_quality_candidates:
                high_quality_attempted_error = True
            continue

        file_extension = _derive_file_extension(urls)
        is_encrypted = manifest_decoded.get("encryptionType", "NONE").upper() != "NONE"
        codecs = manifest_decoded.get("codecs", "")

        manifest = WrapperStreamManifest(
            urls=urls,
            file_extension=file_extension,
            codecs=codecs,
            is_encrypted=is_encrypted,
        )

        stream = WrappedStream(
            album_replay_gain=manifest_info.get("albumReplayGain", 0.0),
            album_peak_amplitude=manifest_info.get("albumPeakAmplitude", 0.0),
            track_replay_gain=manifest_info.get("trackReplayGain", 0.0),
            track_peak_amplitude=manifest_info.get("trackPeakAmplitude", 0.0),
        )

        quality_reported = manifest_info.get("audioQuality")
        if not quality_reported and payload and isinstance(payload[0], dict):
            quality_reported = payload[0].get("audioQuality", "")

        quality_label = (quality_reported or candidate or "UNKNOWN").upper()

        delivered_rank = QUALITY_RANK.get(quality_label, -1)
        if delivered_rank > best_rank or best_result is None:
            best_result = (manifest, stream, quality_label)
            best_rank = delivered_rank

        remaining_candidates = [c for c in candidates[idx + 1 :] if c]
        max_remaining_rank = max((QUALITY_RANK.get(c, -1) for c in remaining_candidates), default=-1)
        should_continue = delivered_rank < max_remaining_rank or (delivered_rank == -1 and max_remaining_rank >= 0)

        if should_continue:
            continue

        break

    if best_result:
        manifest, stream, quality_label = best_result

        if quality_label in {"HIGH", "LOW"} and high_quality_attempted_error and not high_quality_not_available:
            errors.append("High-quality stream temporarily unavailable; received lower quality response.")
            raise WrapperApiError("Lossless stream could not be retrieved due to wrapper errors. Try again.")

        return manifest, stream, quality_label

    if logger and errors:
        logger(f"Wrapper API attempts failed: {'; '.join(errors)}")

    raise WrapperApiError("Unable to retrieve track manifest from wrapped API.")
