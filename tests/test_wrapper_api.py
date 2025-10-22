import base64
import json
from collections.abc import Iterator

from tidalapi import Quality

from tidal_dl_ng.wrapper_api import fetch_track_stream


class StubResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _encode_manifest(
    url: str,
    audio_quality: str | None,
    audio_mode: str | None = "STEREO",
    audio_modes: list[str] | None = None,
) -> list:
    manifest_payload = {
        "urls": [url],
        "encryptionType": "NONE",
        "codecs": "AAC",
    }
    encoded_manifest = base64.b64encode(json.dumps(manifest_payload).encode()).decode()

    track_info = {
        "audioQuality": audio_quality,
        "audioMode": audio_mode,
        "albumReplayGain": 0.0,
        "albumPeakAmplitude": 0.0,
        "trackReplayGain": 0.0,
        "trackPeakAmplitude": 0.0,
    }

    if audio_modes is not None:
        track_info["audioModes"] = audio_modes

    headline = {"audioQuality": audio_quality, "audioMode": audio_mode}
    if audio_modes is not None:
        headline["audioModes"] = audio_modes

    track_info["manifest"] = encoded_manifest

    return [headline, track_info]


def _iter_responses() -> Iterator[StubResponse]:
    # LOSSLESS request -> "quality not found"
    yield StubResponse(404, {"detail": "Quality not found"})
    # Default request succeeds but responds with LOW quality.
    yield StubResponse(200, _encode_manifest("https://example.com/low.m4a", "LOW"))
    # Explicit HIGH request should be attempted and preferred.
    yield StubResponse(200, _encode_manifest("https://example.com/high.m4a", "HIGH"))


def test_fetch_track_stream_prefers_high_when_available(monkeypatch):
    response_iter = _iter_responses()
    seen_requests: list[dict] = []

    def fake_get(url, params, timeout):
        del url, timeout  # Unused in stub.
        seen_requests.append(dict(params))
        try:
            return next(response_iter)
        except StopIteration as exc:  # pragma: no cover - indicates logic regression.
            raise AssertionError("Unexpected additional request") from exc

    monkeypatch.setattr("tidal_dl_ng.wrapper_api.requests.get", fake_get)

    manifest, stream, quality = fetch_track_stream(286295085, Quality.high_lossless)

    assert quality == "HIGH"
    assert manifest.urls == ["https://example.com/high.m4a"]
    assert stream.track_peak_amplitude == 0.0
    assert [req.get("quality") for req in seen_requests] == ["LOSSLESS", None, "HIGH"]


def test_fetch_track_stream_maps_atmos_low_to_lossless(monkeypatch):
    response = StubResponse(200, _encode_manifest("https://example.com/atmos.m4a", "LOW", "DOLBY_ATMOS"))
    call_count = 0

    def fake_get(url, params, timeout):
        nonlocal call_count
        del url, timeout
        call_count += 1
        return response

    monkeypatch.setattr("tidal_dl_ng.wrapper_api.requests.get", fake_get)

    manifest, stream, quality = fetch_track_stream(987654321, Quality.high_lossless)

    assert call_count == 1
    assert quality == "LOSSLESS"
    assert manifest.urls == ["https://example.com/atmos.m4a"]
    assert stream.album_replay_gain == 0.0


def test_fetch_track_stream_maps_atmos_list_to_lossless(monkeypatch):
    response = StubResponse(
        200,
        _encode_manifest("https://example.com/atmos_list.m4a", None, None, ["DOLBY_ATMOS", "STEREO"]),
    )

    def fake_get(url, params, timeout):
        del url, params, timeout
        return response

    monkeypatch.setattr("tidal_dl_ng.wrapper_api.requests.get", fake_get)

    manifest, stream, quality = fetch_track_stream(192837465, Quality.high_lossless)

    assert quality == "LOSSLESS"
    assert manifest.urls == ["https://example.com/atmos_list.m4a"]
    assert stream.track_replay_gain == 0.0


def test_fetch_track_stream_normalizes_quality_alias(monkeypatch):
    response = StubResponse(200, _encode_manifest("https://example.com/hires.flac", "hi_res"))

    def fake_get(url, params, timeout):
        del url, params, timeout
        return response

    monkeypatch.setattr("tidal_dl_ng.wrapper_api.requests.get", fake_get)

    manifest, stream, quality = fetch_track_stream(555123, Quality.hi_res_lossless)

    assert quality == "HI_RES_LOSSLESS"
    assert manifest.urls == ["https://example.com/hires.flac"]
    assert stream.album_peak_amplitude == 0.0
