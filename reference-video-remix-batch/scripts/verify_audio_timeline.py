#!/usr/bin/env python3
"""Verify an expected and an actual WAV against a confirmed audio timeline.

The verifier is deliberately local and uses only the Python standard library.
It decodes integer PCM WAV, converts both inputs to 48 kHz float PCM, and emits
one machine-readable PASS/FAIL/UNKNOWN report.  It does not fetch, upload, or
decode container media; callers must provide locally decoded WAV files.
"""

from __future__ import annotations

import argparse
from array import array
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import wave


SCHEMA_VERSION = "1.0"
TARGET_SAMPLE_RATE = 48_000
MAX_WAV_BYTES = 512 * 1024 * 1024
MAX_TIMELINE_BYTES = 5 * 1024 * 1024
MAX_PCM_SAMPLES = 60_000_000

RMS_TOLERANCE_DB = 1.5
PEAK_TOLERANCE_DB = 3.0
MAX_ALIGNMENT_SEARCH_SECONDS = 0.100
MAX_OFFSET_SECONDS = 0.050
ENVELOPE_WINDOW_SECONDS = 0.020
ENVELOPE_CORRELATION_MIN = 0.95
SEAM_HALF_WINDOW_SECONDS = 0.500
SEAM_CORRELATION_MIN = 0.90
RESTART_WINDOW_SECONDS = 1.0
RESTART_MARGIN = 0.05
REPETITIVE_CORRELATION_MIN = 0.90
SILENCE_THRESHOLD_DBFS = -60.0
SILENCE_THRESHOLD_LINEAR = 10.0 ** (SILENCE_THRESHOLD_DBFS / 20.0)
CLIP_THRESHOLD = 0.999


class VerificationInputError(ValueError):
    """Raised when an input cannot be safely or meaningfully verified."""


@dataclass
class AudioData:
    sample_rate: int
    channels: int
    sample_width_bytes: int
    original_frame_count: int
    samples: List[array]
    sha256: str

    @property
    def frame_count(self) -> int:
        return len(self.samples[0]) if self.samples else 0

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self.frame_count / self.sample_rate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_regular_file(path_text: str, max_bytes: int, label: str) -> Path:
    path = Path(path_text)
    try:
        if path.is_symlink():
            raise VerificationInputError(f"{label} must not be a symbolic link")
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except VerificationInputError:
        raise
    except (OSError, RuntimeError) as exc:
        raise VerificationInputError(f"cannot open {label}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise VerificationInputError(f"{label} must be a regular file")
    if info.st_size > max_bytes:
        raise VerificationInputError(f"{label} exceeds the {max_bytes}-byte limit")
    return resolved


def _decode_pcm(raw: bytes, sample_width: int, channels: int) -> List[array]:
    if channels < 1:
        raise VerificationInputError("WAV must contain at least one channel")
    if sample_width not in (1, 2, 3, 4):
        raise VerificationInputError(
            "only 8-, 16-, 24-, and 32-bit integer PCM WAV is supported"
        )
    frame_width = sample_width * channels
    if len(raw) % frame_width:
        raise VerificationInputError("WAV PCM payload is not frame-aligned")
    frame_count = len(raw) // frame_width
    outputs = [array("f") for _ in range(channels)]

    if sample_width == 1:
        for index, value in enumerate(raw):
            outputs[index % channels].append((value - 128) / 128.0)
        return outputs

    if sample_width in (2, 4):
        code = "h" if sample_width == 2 else "i"
        scale = float(1 << (8 * sample_width - 1))
        values = array(code)
        values.frombytes(raw)
        if sys.byteorder != "little":
            values.byteswap()
        for index, value in enumerate(values):
            outputs[index % channels].append(value / scale)
        return outputs

    scale = float(1 << 23)
    channel = 0
    for offset in range(0, len(raw), 3):
        value = raw[offset] | (raw[offset + 1] << 8) | (raw[offset + 2] << 16)
        if value & 0x800000:
            value -= 1 << 24
        outputs[channel].append(value / scale)
        channel += 1
        if channel == channels:
            channel = 0
    return outputs


def _read_wav(path_text: str, label: str) -> AudioData:
    path = _safe_regular_file(path_text, MAX_WAV_BYTES, label)
    file_hash = _sha256_file(path)
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise VerificationInputError(
                    f"{label} uses unsupported WAV compression {reader.getcomptype()!r}"
                )
            channels = reader.getnchannels()
            sample_rate = reader.getframerate()
            sample_width = reader.getsampwidth()
            frame_count = reader.getnframes()
            if sample_rate < 1 or sample_rate > 384_000:
                raise VerificationInputError(f"{label} has an invalid sample rate")
            if channels < 1 or channels > 32:
                raise VerificationInputError(f"{label} has an unsupported channel count")
            if frame_count * channels > MAX_PCM_SAMPLES:
                raise VerificationInputError(
                    f"{label} exceeds the decoded PCM sample safety limit"
                )
            projected_frames = int(
                math.ceil(frame_count * TARGET_SAMPLE_RATE / sample_rate)
            )
            if projected_frames * channels > MAX_PCM_SAMPLES:
                raise VerificationInputError(
                    f"{label} would exceed the PCM safety limit after 48 kHz conversion"
                )
            raw = reader.readframes(frame_count)
    except VerificationInputError:
        raise
    except (wave.Error, EOFError, OSError) as exc:
        raise VerificationInputError(f"cannot decode {label} as PCM WAV: {exc}") from exc

    decoded = _decode_pcm(raw, sample_width, channels)
    if not decoded or any(len(channel) != frame_count for channel in decoded):
        raise VerificationInputError(f"{label} decoded frame count is inconsistent")
    return AudioData(
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        original_frame_count=frame_count,
        samples=decoded,
        sha256=file_hash,
    )


def _linear_resample_channel(source: array, source_rate: int, target_rate: int) -> array:
    if source_rate == target_rate:
        return array("f", source)
    if len(source) == 0:
        return array("f")
    target_count = max(1, int(round(len(source) * target_rate / source_rate)))
    result = array("f")
    result_extend = result.append
    ratio = source_rate / target_rate
    last_index = len(source) - 1
    for out_index in range(target_count):
        source_position = out_index * ratio
        left = int(source_position)
        if left >= last_index:
            result_extend(source[last_index])
            continue
        fraction = source_position - left
        result_extend(source[left] + (source[left + 1] - source[left]) * fraction)
    return result


def _resample(audio: AudioData) -> AudioData:
    if audio.sample_rate == TARGET_SAMPLE_RATE:
        return AudioData(
            sample_rate=TARGET_SAMPLE_RATE,
            channels=audio.channels,
            sample_width_bytes=audio.sample_width_bytes,
            original_frame_count=audio.original_frame_count,
            samples=[array("f", item) for item in audio.samples],
            sha256=audio.sha256,
        )
    samples = [
        _linear_resample_channel(item, audio.sample_rate, TARGET_SAMPLE_RATE)
        for item in audio.samples
    ]
    return AudioData(
        sample_rate=TARGET_SAMPLE_RATE,
        channels=audio.channels,
        sample_width_bytes=audio.sample_width_bytes,
        original_frame_count=audio.original_frame_count,
        samples=samples,
        sha256=audio.sha256,
    )


def _read_json(path_text: str) -> Tuple[Any, str]:
    path = _safe_regular_file(path_text, MAX_TIMELINE_BYTES, "timeline map")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise VerificationInputError(f"cannot read timeline map: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise VerificationInputError(f"timeline map is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, (dict, list)):
        raise VerificationInputError("timeline map must be a JSON object or array")
    stack: List[Tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > 20:
            raise VerificationInputError("timeline map exceeds maximum nesting depth 20")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value, hashlib.sha256(raw).hexdigest()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerificationInputError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise VerificationInputError(f"{label} must be finite")
    return result


def _window_from_item(item: Any, label: str) -> Tuple[float, float]:
    if isinstance(item, (list, tuple)) and len(item) == 2:
        start = _number(item[0], f"{label}.start")
        end = _number(item[1], f"{label}.end")
    elif isinstance(item, dict):
        start_value = item.get("start", item.get("target_start"))
        end_value = item.get("end", item.get("target_end"))
        start = _number(start_value, f"{label}.start")
        end = _number(end_value, f"{label}.end")
    else:
        raise VerificationInputError(f"{label} must contain start and end")
    if start < 0 or end <= start:
        raise VerificationInputError(f"{label} must satisfy 0 <= start < end")
    return start, end


def _is_intentional_silence_operation(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        return normalized in {
            "silence",
            "intentional_silence",
            "mute",
            "muted",
        }
    if isinstance(value, dict):
        operation_type = value.get("type", value.get("operation", value.get("mode")))
        if not _is_intentional_silence_operation(operation_type):
            return False
        return value.get("intentional", True) is True
    return False


def _merge_windows(windows: Iterable[Tuple[float, float]]) -> List[Tuple[float, float]]:
    ordered = sorted(windows)
    merged: List[Tuple[float, float]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + 1e-9:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _parse_timestamp_correction(root: Mapping[str, Any]) -> Dict[str, Dict[str, float]]:
    raw = root.get("timestamp_correction", root.get("timestamp_corrections", {}))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise VerificationInputError("timestamp_correction must be an object")
    result: Dict[str, Dict[str, float]] = {}
    for side in ("expected", "actual"):
        item = raw.get(side, {})
        if item is None:
            item = {}
        if not isinstance(item, dict):
            raise VerificationInputError(f"timestamp_correction.{side} must be an object")
        start = _number(
            item.get("start_trim_seconds", item.get("priming_seconds", 0.0)),
            f"timestamp_correction.{side}.start_trim_seconds",
        )
        end = _number(
            item.get("end_trim_seconds", item.get("tail_padding_seconds", 0.0)),
            f"timestamp_correction.{side}.end_trim_seconds",
        )
        if start < 0 or end < 0 or start > 10 or end > 10:
            raise VerificationInputError(
                f"timestamp_correction.{side} trims must be between 0 and 10 seconds"
            )
        result[side] = {"start_trim_seconds": start, "end_trim_seconds": end}
    return result


def _parse_timeline(value: Any) -> Dict[str, Any]:
    if isinstance(value, list):
        root: Mapping[str, Any] = {"timeline_map": value}
    else:
        root = value
    timeline_items = root.get("timeline_map", [])
    if not isinstance(timeline_items, list):
        raise VerificationInputError("timeline_map must be a list")

    seams: List[float] = []
    explicit_seams = root.get("seams", root.get("seam_times", []))
    if explicit_seams is None:
        explicit_seams = []
    if not isinstance(explicit_seams, list):
        raise VerificationInputError("seams must be a list")
    for index, item in enumerate(explicit_seams):
        if isinstance(item, dict):
            item = item.get("time", item.get("target_time"))
        seam = _number(item, f"seams[{index}]")
        if seam <= 0:
            raise VerificationInputError("seam times must be greater than zero")
        seams.append(seam)

    silence: List[Tuple[float, float]] = []
    explicit_silence = root.get(
        "intentional_silence", root.get("intentional_silence_windows", [])
    )
    if explicit_silence is None:
        explicit_silence = []
    if not isinstance(explicit_silence, list):
        raise VerificationInputError("intentional_silence must be a list")
    for index, item in enumerate(explicit_silence):
        silence.append(_window_from_item(item, f"intentional_silence[{index}]"))

    entries: List[Dict[str, Any]] = []
    for index, item in enumerate(timeline_items):
        if not isinstance(item, dict):
            raise VerificationInputError(f"timeline_map[{index}] must be an object")
        if "target_start" not in item or "target_end" not in item:
            raise VerificationInputError(
                f"timeline_map[{index}] requires target_start and target_end"
            )
        start, end = _window_from_item(item, f"timeline_map[{index}]")
        entries.append({"start": start, "end": end})
        if _is_intentional_silence_operation(item.get("audio_operation")):
            silence.append((start, end))
        if item.get("seam_after") is True:
            seams.append(end)

    entries.sort(key=lambda item: (item["start"], item["end"]))
    for index in range(len(entries) - 1):
        current = entries[index]
        following = entries[index + 1]
        if following["start"] < current["end"] - 1e-9:
            raise VerificationInputError("timeline_map target ranges must not overlap")
        if abs(following["start"] - current["end"]) <= 1e-6:
            seams.append(current["end"])

    return {
        "seams": sorted(set(round(item, 9) for item in seams)),
        "intentional_silence": _merge_windows(silence),
        "timestamp_correction": _parse_timestamp_correction(root),
        "timeline_entry_count": len(entries),
    }


def _trim_audio(audio: AudioData, start_seconds: float, end_seconds: float) -> AudioData:
    start = int(round(start_seconds * audio.sample_rate))
    end_trim = int(round(end_seconds * audio.sample_rate))
    stop = audio.frame_count - end_trim
    if start < 0 or end_trim < 0 or stop <= start:
        raise VerificationInputError("timestamp correction removes the entire WAV")
    return AudioData(
        sample_rate=audio.sample_rate,
        channels=audio.channels,
        sample_width_bytes=audio.sample_width_bytes,
        original_frame_count=audio.original_frame_count,
        samples=[array("f", channel[start:stop]) for channel in audio.samples],
        sha256=audio.sha256,
    )


def _downmix(audio: AudioData) -> AudioData:
    if audio.channels == 1:
        return audio
    output = array("f")
    count = audio.frame_count
    scale = 1.0 / audio.channels
    append = output.append
    for index in range(count):
        append(sum(channel[index] for channel in audio.samples) * scale)
    return AudioData(
        sample_rate=audio.sample_rate,
        channels=1,
        sample_width_bytes=audio.sample_width_bytes,
        original_frame_count=audio.original_frame_count,
        samples=[output],
        sha256=audio.sha256,
    )


def _sample_ranges_excluding(
    frame_count: int, sample_rate: int, windows: Sequence[Tuple[float, float]]
) -> List[Tuple[int, int]]:
    excluded: List[Tuple[int, int]] = []
    for start_seconds, end_seconds in windows:
        start = max(0, min(frame_count, int(math.floor(start_seconds * sample_rate))))
        end = max(0, min(frame_count, int(math.ceil(end_seconds * sample_rate))))
        if end > start:
            excluded.append((start, end))
    excluded.sort()
    merged: List[Tuple[int, int]] = []
    for start, end in excluded:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    ranges: List[Tuple[int, int]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < frame_count:
        ranges.append((cursor, frame_count))
    return ranges


def _channel_stats(channel: array, ranges: Sequence[Tuple[int, int]]) -> Dict[str, Any]:
    count = 0
    total_sq = 0.0
    peak = 0.0
    clipped = 0
    for start, end in ranges:
        for value in channel[start:end]:
            absolute = abs(value)
            total_sq += value * value
            count += 1
            if absolute > peak:
                peak = absolute
            if absolute >= CLIP_THRESHOLD:
                clipped += 1
    rms = math.sqrt(total_sq / count) if count else 0.0
    return {
        "sample_count": count,
        "rms": rms,
        "peak": peak,
        "clipped_samples": clipped,
        "clipped_fraction": clipped / count if count else 0.0,
    }


def _db(value: float) -> Optional[float]:
    if value <= 0:
        return None
    return 20.0 * math.log10(value)


def _db_difference(actual: float, expected: float) -> Optional[float]:
    if actual <= 0 or expected <= 0:
        return None
    return 20.0 * math.log10(actual / expected)


def _pearson_vectors(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 2:
        return None
    count = len(left)
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    numerator = 0.0
    denom_left = 0.0
    denom_right = 0.0
    max_difference = 0.0
    for l_value, r_value in zip(left, right):
        l_centered = l_value - mean_left
        r_centered = r_value - mean_right
        numerator += l_centered * r_centered
        denom_left += l_centered * l_centered
        denom_right += r_centered * r_centered
        max_difference = max(max_difference, abs(l_value - r_value))
    denominator = math.sqrt(denom_left * denom_right)
    if denominator <= 1e-20:
        return 1.0 if max_difference <= 1e-7 else None
    return max(-1.0, min(1.0, numerator / denominator))


def _is_silence_time(seconds: float, windows: Sequence[Tuple[float, float]]) -> bool:
    for start, end in windows:
        if start <= seconds < end:
            return True
        if start > seconds:
            break
    return False


def _alignment_signal(
    channel: array,
    sample_rate: int,
    silence_windows: Sequence[Tuple[float, float]],
    block_seconds: float = 0.002,
) -> Tuple[List[float], List[bool], int]:
    block = max(1, int(round(block_seconds * sample_rate)))
    if not channel:
        return [], [], block
    ranges = _sample_ranges_excluding(len(channel), sample_rate, silence_windows)
    count = sum(end - start for start, end in ranges)
    global_mean = (
        sum(sum(channel[start:end]) for start, end in ranges) / count if count else 0.0
    )
    values: List[float] = []
    usable: List[bool] = []
    for start in range(0, len(channel), block):
        end = min(len(channel), start + block)
        center_time = (start + end) / (2.0 * sample_rate)
        values.append(sum(channel[start:end]) / (end - start) - global_mean)
        usable.append(not _is_silence_time(center_time, silence_windows))
    return values, usable, block


def _lag_correlation(
    expected: Sequence[float],
    actual: Sequence[float],
    usable: Sequence[bool],
    lag: int,
) -> Optional[float]:
    start_expected = max(0, -lag)
    end_expected = min(len(expected), len(actual) - lag)
    if end_expected - start_expected < 10:
        return None
    pairs_left: List[float] = []
    pairs_right: List[float] = []
    step = max(1, (end_expected - start_expected) // 25_000)
    for index in range(start_expected, end_expected, step):
        if index < len(usable) and usable[index]:
            pairs_left.append(expected[index])
            pairs_right.append(actual[index + lag])
    return _pearson_vectors(pairs_left, pairs_right)


def _find_offset(
    expected: array,
    actual: array,
    sample_rate: int,
    silence_windows: Sequence[Tuple[float, float]],
) -> Dict[str, Any]:
    expected_signal, usable, block = _alignment_signal(
        expected, sample_rate, silence_windows
    )
    actual_signal, _unused, _ = _alignment_signal(actual, sample_rate, [])
    max_lag = max(1, int(round(MAX_ALIGNMENT_SEARCH_SECONDS * sample_rate / block)))
    candidates: List[Tuple[float, int]] = []
    for lag in range(-max_lag, max_lag + 1):
        score = _lag_correlation(expected_signal, actual_signal, usable, lag)
        if score is not None:
            candidates.append((score, lag))
    if not candidates:
        return {
            "result": "UNKNOWN",
            "reason": "insufficient non-silent material for offset search",
            "offset_seconds": None,
            "correlation": None,
            "offset_samples": 0,
        }
    best_score = max(score for score, _lag in candidates)
    tied = [
        (score, lag)
        for score, lag in candidates
        if score >= best_score - 1e-7
    ]
    score, lag = min(tied, key=lambda item: (abs(item[1]), item[1]))
    offset_samples = lag * block
    offset_seconds = offset_samples / sample_rate
    if score < 0.20:
        result = "UNKNOWN"
        reason = "best offset correlation is too weak to establish alignment"
    elif abs(offset_seconds) > MAX_OFFSET_SECONDS + 1e-12:
        result = "FAIL"
        reason = "best offset exceeds 50 ms"
    else:
        result = "PASS"
        reason = "best offset is within 50 ms"
    return {
        "result": result,
        "reason": reason,
        "offset_seconds": offset_seconds,
        "correlation": score,
        "offset_samples": offset_samples,
        "search_range_seconds": MAX_ALIGNMENT_SEARCH_SECONDS,
    }


def _paired_samples(
    expected: array,
    actual: array,
    offset_samples: int,
    start: int,
    end: int,
    sample_rate: int,
    silence_windows: Sequence[Tuple[float, float]],
    max_points: int = 100_000,
) -> Tuple[List[float], List[float]]:
    left: List[float] = []
    right: List[float] = []
    clipped_start = max(0, start, -offset_samples)
    clipped_end = min(len(expected), end, len(actual) - offset_samples)
    if clipped_end <= clipped_start:
        return left, right
    stride = max(1, (clipped_end - clipped_start) // max_points)
    for index in range(clipped_start, clipped_end, stride):
        if _is_silence_time(index / sample_rate, silence_windows):
            continue
        left.append(expected[index])
        right.append(actual[index + offset_samples])
    return left, right


def _rms_envelope_pair(
    expected: array,
    actual: array,
    offset_samples: int,
    sample_rate: int,
    silence_windows: Sequence[Tuple[float, float]],
    actual_gain: float,
) -> Tuple[List[float], List[float]]:
    window = max(1, int(round(ENVELOPE_WINDOW_SECONDS * sample_rate)))
    expected_values: List[float] = []
    actual_values: List[float] = []
    end_limit = min(len(expected), len(actual) - offset_samples)
    start = max(0, -offset_samples)
    cursor = (start // window) * window
    if cursor < start:
        cursor += window
    while cursor + window <= end_limit:
        center_time = (cursor + window / 2.0) / sample_rate
        if not _is_silence_time(center_time, silence_windows):
            expected_sq = 0.0
            actual_sq = 0.0
            actual_start = cursor + offset_samples
            for position in range(window):
                e_value = expected[cursor + position]
                a_value = actual[actual_start + position] * actual_gain
                expected_sq += e_value * e_value
                actual_sq += a_value * a_value
            expected_rms = math.sqrt(expected_sq / window)
            actual_rms = math.sqrt(actual_sq / window)
            if expected_rms >= SILENCE_THRESHOLD_LINEAR:
                expected_values.append(expected_rms)
                actual_values.append(actual_rms)
        cursor += window
    return expected_values, actual_values


def _worst(results: Iterable[str]) -> str:
    values = list(results)
    if "FAIL" in values:
        return "FAIL"
    if "UNKNOWN" in values:
        return "UNKNOWN"
    return "PASS"


def _check(name: str, result: str, reason: str, **details: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"name": name, "result": result, "reason": reason}
    payload.update(details)
    return payload


def _level_check(
    expected: AudioData,
    actual: AudioData,
    silence_windows: Sequence[Tuple[float, float]],
) -> Tuple[Dict[str, Any], List[float], List[Dict[str, Any]], List[Dict[str, Any]]]:
    expected_ranges = _sample_ranges_excluding(
        expected.frame_count, expected.sample_rate, silence_windows
    )
    actual_ranges = _sample_ranges_excluding(
        actual.frame_count, actual.sample_rate, silence_windows
    )
    expected_stats = [_channel_stats(item, expected_ranges) for item in expected.samples]
    actual_stats = [_channel_stats(item, actual_ranges) for item in actual.samples]
    channel_details: List[Dict[str, Any]] = []
    gains: List[float] = []
    channel_results: List[str] = []
    for index, (expected_stat, actual_stat) in enumerate(
        zip(expected_stats, actual_stats), start=1
    ):
        expected_rms = expected_stat["rms"]
        actual_rms = actual_stat["rms"]
        expected_peak = expected_stat["peak"]
        actual_peak = actual_stat["peak"]
        rms_difference = _db_difference(actual_rms, expected_rms)
        peak_difference = _db_difference(actual_peak, expected_peak)
        reasons: List[str] = []
        result = "PASS"
        if expected_stat["sample_count"] == 0 or actual_stat["sample_count"] == 0:
            result = "UNKNOWN"
            reasons.append("no samples remain outside intentional silence")
        elif expected_rms < SILENCE_THRESHOLD_LINEAR:
            if actual_rms >= SILENCE_THRESHOLD_LINEAR:
                result = "FAIL"
                reasons.append("unexpected audio exists where expected channel is silent")
            else:
                reasons.append("both expected and actual channels are effectively silent")
        elif actual_rms < SILENCE_THRESHOLD_LINEAR:
            result = "FAIL"
            reasons.append("actual channel is missing or nearly silent")
        else:
            if rms_difference is None or abs(rms_difference) > RMS_TOLERANCE_DB:
                result = "FAIL"
                reasons.append("RMS level difference exceeds ±1.5 dB")
            if peak_difference is None or abs(peak_difference) > PEAK_TOLERANCE_DB:
                result = "FAIL"
                reasons.append("peak level difference exceeds ±3 dB")
            if (
                actual_stat["clipped_samples"] > expected_stat["clipped_samples"]
                and actual_peak >= CLIP_THRESHOLD
            ):
                result = "FAIL"
                reasons.append("actual channel introduces new clipping")
            if not reasons:
                reasons.append("absolute levels and clipping are within policy")
        gains.append(expected_rms / actual_rms if actual_rms > 1e-12 else 1.0)
        channel_results.append(result)
        channel_details.append(
            {
                "channel": index,
                "result": result,
                "reason": "; ".join(reasons),
                "expected_rms_dbfs": _db(expected_rms),
                "actual_rms_dbfs": _db(actual_rms),
                "rms_difference_db": rms_difference,
                "expected_peak_dbfs": _db(expected_peak),
                "actual_peak_dbfs": _db(actual_peak),
                "peak_difference_db": peak_difference,
                "expected_clipped_samples": expected_stat["clipped_samples"],
                "actual_clipped_samples": actual_stat["clipped_samples"],
            }
        )
    overall = _worst(channel_results)
    return (
        _check(
            "absolute_levels",
            overall,
            "per-channel RMS, peak, silence, and clipping evaluation completed",
            channels=channel_details,
        ),
        gains,
        expected_stats,
        actual_stats,
    )


def _silence_boundary_check(
    expected: AudioData,
    actual: AudioData,
    windows: Sequence[Tuple[float, float]],
) -> Dict[str, Any]:
    if not windows:
        return _check(
            "intentional_silence",
            "PASS",
            "no intentional silence windows were declared",
            applicable=False,
            windows=[],
        )
    frame_seconds = 0.010
    frame_samples = max(1, int(round(frame_seconds * actual.sample_rate)))
    details: List[Dict[str, Any]] = []
    window_results: List[str] = []

    def frame_rms(audio: AudioData, start: int, end: int) -> float:
        total_sq = 0.0
        count = 0
        for channel in audio.samples:
            for value in channel[max(0, start) : min(audio.frame_count, end)]:
                total_sq += value * value
                count += 1
        return math.sqrt(total_sq / count) if count else 0.0

    def find_transition(audio: AudioData, when: float, entering: bool) -> Optional[float]:
        center = int(round(when * audio.sample_rate))
        radius = int(round(MAX_ALIGNMENT_SEARCH_SECONDS * audio.sample_rate))
        first = max(frame_samples, center - radius)
        last = min(audio.frame_count - frame_samples, center + radius)
        candidates: List[float] = []
        for boundary in range(first, last + 1, frame_samples):
            before = frame_rms(audio, boundary - frame_samples, boundary)
            after = frame_rms(audio, boundary, boundary + frame_samples)
            matches = (
                before >= SILENCE_THRESHOLD_LINEAR and after < SILENCE_THRESHOLD_LINEAR
                if entering
                else before < SILENCE_THRESHOLD_LINEAR and after >= SILENCE_THRESHOLD_LINEAR
            )
            if matches:
                candidates.append(boundary / audio.sample_rate)
        if not candidates:
            return None
        return min(candidates, key=lambda value: abs(value - when))

    duration = min(expected.duration_seconds, actual.duration_seconds)
    for start, end in windows:
        clipped_start = max(0.0, min(duration, start))
        clipped_end = max(0.0, min(duration, end))
        reasons: List[str] = []
        result = "PASS"
        if clipped_end <= clipped_start:
            result = "UNKNOWN"
            reasons.append("window lies outside comparable audio duration")
            details.append(
                {
                    "start": start,
                    "end": end,
                    "result": result,
                    "reason": "; ".join(reasons),
                }
            )
            window_results.append(result)
            continue
        sample_start = int(round(clipped_start * actual.sample_rate))
        sample_end = int(round(clipped_end * actual.sample_rate))
        expected_rms = frame_rms(expected, sample_start, sample_end)
        actual_rms = frame_rms(actual, sample_start, sample_end)
        if expected_rms >= SILENCE_THRESHOLD_LINEAR:
            result = "UNKNOWN"
            reasons.append("expected master is not silent in the declared window")
        if actual_rms >= SILENCE_THRESHOLD_LINEAR:
            result = "FAIL"
            reasons.append("actual audio is not silent in the declared window")

        start_offset: Optional[float] = None
        end_offset: Optional[float] = None
        if start > 0:
            transition = find_transition(actual, start, True)
            if transition is None:
                if result != "FAIL":
                    result = "UNKNOWN"
                reasons.append("could not locate the silence start boundary")
            else:
                start_offset = transition - start
                if abs(start_offset) > MAX_OFFSET_SECONDS:
                    result = "FAIL"
                    reasons.append("silence start boundary is displaced by more than 50 ms")
        if end < duration:
            transition = find_transition(actual, end, False)
            if transition is None:
                if result != "FAIL":
                    result = "UNKNOWN"
                reasons.append("could not locate the silence end boundary")
            else:
                end_offset = transition - end
                if abs(end_offset) > MAX_OFFSET_SECONDS:
                    result = "FAIL"
                    reasons.append("silence end boundary is displaced by more than 50 ms")
        if not reasons:
            reasons.append("silence content and boundaries match the declared window")
        details.append(
            {
                "start": start,
                "end": end,
                "result": result,
                "reason": "; ".join(reasons),
                "expected_rms_dbfs": _db(expected_rms),
                "actual_rms_dbfs": _db(actual_rms),
                "start_boundary_offset_seconds": start_offset,
                "end_boundary_offset_seconds": end_offset,
            }
        )
        window_results.append(result)
    return _check(
        "intentional_silence",
        _worst(window_results),
        "declared silence content and boundary checks completed",
        applicable=True,
        windows=details,
    )


def _overall(checks: Sequence[Mapping[str, Any]]) -> str:
    return _worst(str(item.get("result", "UNKNOWN")) for item in checks)


def _base_report(timeline_hash: Optional[str], fps: float) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "audio-timeline-verification",
        "overall_result": "UNKNOWN",
        "inputs": {"timeline_map_sha256": timeline_hash},
        "normalization": {
            "target_sample_rate_hz": TARGET_SAMPLE_RATE,
            "gain_normalization_applied_only_after_absolute_level_check": True,
            "downmix_applied": False,
        },
        "policy": {
            "fps": fps,
            "duration_tolerance_seconds": max(2.0 / fps, 0.05),
            "rms_tolerance_db": RMS_TOLERANCE_DB,
            "peak_tolerance_db": PEAK_TOLERANCE_DB,
            "offset_search_seconds": MAX_ALIGNMENT_SEARCH_SECONDS,
            "max_offset_seconds": MAX_OFFSET_SECONDS,
            "envelope_window_seconds": ENVELOPE_WINDOW_SECONDS,
            "envelope_correlation_min": ENVELOPE_CORRELATION_MIN,
            "seam_half_window_seconds": SEAM_HALF_WINDOW_SECONDS,
            "seam_correlation_min": SEAM_CORRELATION_MIN,
            "restart_window_seconds": RESTART_WINDOW_SECONDS,
            "restart_margin": RESTART_MARGIN,
            "repetitive_correlation_min": REPETITIVE_CORRELATION_MIN,
            "silence_threshold_dbfs": SILENCE_THRESHOLD_DBFS,
        },
        "safety": {
            "network_used": False,
            "upload_performed": False,
            "external_process_used": False,
        },
        "timeline": {},
        "checks": [],
        "summary": {"pass": 0, "fail": 0, "unknown": 0},
    }


def verify(args: argparse.Namespace) -> Dict[str, Any]:
    timeline_value, timeline_hash = _read_json(args.timeline_map)
    timeline = _parse_timeline(timeline_value)
    report = _base_report(timeline_hash, args.fps)
    report["timeline"] = {
        "entry_count": timeline["timeline_entry_count"],
        "seams_seconds": timeline["seams"],
        "intentional_silence_seconds": [
            {"start": start, "end": end}
            for start, end in timeline["intentional_silence"]
        ],
        "timestamp_correction": timeline["timestamp_correction"],
    }

    expected_original = _read_wav(args.expected, "expected WAV")
    actual_original = _read_wav(args.actual, "actual WAV")
    report["inputs"].update(
        {
            "expected": {
                "sha256": expected_original.sha256,
                "sample_rate_hz": expected_original.sample_rate,
                "channels": expected_original.channels,
                "sample_width_bytes": expected_original.sample_width_bytes,
                "frame_count": expected_original.original_frame_count,
            },
            "actual": {
                "sha256": actual_original.sha256,
                "sample_rate_hz": actual_original.sample_rate,
                "channels": actual_original.channels,
                "sample_width_bytes": actual_original.sample_width_bytes,
                "frame_count": actual_original.original_frame_count,
            },
        }
    )

    expected = _resample(expected_original)
    actual = _resample(actual_original)
    correction = timeline["timestamp_correction"]
    expected = _trim_audio(
        expected,
        correction["expected"]["start_trim_seconds"],
        correction["expected"]["end_trim_seconds"],
    )
    actual = _trim_audio(
        actual,
        correction["actual"]["start_trim_seconds"],
        correction["actual"]["end_trim_seconds"],
    )
    checks: List[Dict[str, Any]] = []
    checks.append(
        _check(
            "decode_and_convert",
            "PASS",
            "both PCM WAV inputs decoded and were converted to 48 kHz float PCM",
            expected_effective_frames=expected.frame_count,
            actual_effective_frames=actual.frame_count,
            expected_effective_duration_seconds=expected.duration_seconds,
            actual_effective_duration_seconds=actual.duration_seconds,
        )
    )

    if expected.channels != actual.channels:
        if args.allow_downmix:
            expected = _downmix(expected)
            actual = _downmix(actual)
            report["normalization"]["downmix_applied"] = True
            checks.append(
                _check(
                    "channel_layout",
                    "PASS",
                    "channel counts differed and explicit downmix permission was applied",
                    expected_original_channels=expected_original.channels,
                    actual_original_channels=actual_original.channels,
                    compared_channels=1,
                )
            )
        else:
            checks.append(
                _check(
                    "channel_layout",
                    "FAIL",
                    "actual channel count does not match expected and downmix was not allowed",
                    expected_channels=expected.channels,
                    actual_channels=actual.channels,
                )
            )
    else:
        checks.append(
            _check(
                "channel_layout",
                "PASS",
                "actual and expected channel counts match",
                expected_channels=expected.channels,
                actual_channels=actual.channels,
            )
        )

    duration_difference = actual.duration_seconds - expected.duration_seconds
    duration_tolerance = max(2.0 / args.fps, 0.05)
    checks.append(
        _check(
            "effective_duration",
            "PASS" if abs(duration_difference) <= duration_tolerance else "FAIL",
            "effective duration difference is within policy"
            if abs(duration_difference) <= duration_tolerance
            else "effective duration difference exceeds policy",
            expected_seconds=expected.duration_seconds,
            actual_seconds=actual.duration_seconds,
            difference_seconds=duration_difference,
            tolerance_seconds=duration_tolerance,
        )
    )

    silence_windows: List[Tuple[float, float]] = timeline["intentional_silence"]
    checks.append(_silence_boundary_check(expected, actual, silence_windows))

    if expected.channels != actual.channels:
        for name in (
            "absolute_levels",
            "alignment_offset",
            "channel_identity",
            "envelope_correlation",
            "seam_correlation",
            "music_restart_detection",
        ):
            checks.append(
                _check(
                    name,
                    "UNKNOWN",
                    "check could not run because channel layouts are incompatible",
                )
            )
        report["checks"] = checks
        report["overall_result"] = _overall(checks)
        _finish_summary(report)
        return report

    level_check, gains, expected_stats, _actual_stats = _level_check(
        expected, actual, silence_windows
    )
    checks.append(level_check)

    alignments = [
        _find_offset(e_channel, a_channel, TARGET_SAMPLE_RATE, silence_windows)
        for e_channel, a_channel in zip(expected.samples, actual.samples)
    ]
    checks.append(
        _check(
            "alignment_offset",
            _worst(item["result"] for item in alignments),
            "per-channel offset search completed after DC removal",
            channels=[
                {
                    "channel": index,
                    "result": item["result"],
                    "reason": item["reason"],
                    "offset_seconds": item["offset_seconds"],
                    "correlation": item["correlation"],
                    "search_range_seconds": item.get("search_range_seconds"),
                }
                for index, item in enumerate(alignments, start=1)
            ],
        )
    )

    identity_details: List[Dict[str, Any]] = []
    identity_results: List[str] = []
    for index, alignment in enumerate(alignments):
        offset = int(alignment.get("offset_samples", 0))
        matching_left, matching_right = _paired_samples(
            expected.samples[index],
            actual.samples[index],
            offset,
            0,
            expected.frame_count,
            TARGET_SAMPLE_RATE,
            silence_windows,
            max_points=50_000,
        )
        matching = _pearson_vectors(matching_left, matching_right)
        alternatives: List[Tuple[int, float]] = []
        for other in range(actual.channels):
            if other == index:
                continue
            alternative_alignment = _find_offset(
                expected.samples[index],
                actual.samples[other],
                TARGET_SAMPLE_RATE,
                silence_windows,
            )
            alternative_offset = int(alternative_alignment.get("offset_samples", 0))
            alt_left, alt_right = _paired_samples(
                expected.samples[index],
                actual.samples[other],
                alternative_offset,
                0,
                expected.frame_count,
                TARGET_SAMPLE_RATE,
                silence_windows,
                max_points=50_000,
            )
            score = _pearson_vectors(alt_left, alt_right)
            if score is not None:
                alternatives.append((other, score))
        best_alternative = max(alternatives, key=lambda item: item[1]) if alternatives else None
        if matching is None:
            result = "UNKNOWN"
            reason = "insufficient waveform variance to assess channel identity"
        elif (
            best_alternative is not None
            and best_alternative[1] >= 0.90
            and best_alternative[1] >= matching + 0.05
        ):
            result = "FAIL"
            reason = "another actual channel matches better, indicating a possible swap"
        else:
            result = "PASS"
            reason = "no channel swap was detected"
        identity_results.append(result)
        identity_details.append(
            {
                "channel": index + 1,
                "result": result,
                "reason": reason,
                "matching_waveform_correlation": matching,
                "best_other_channel": best_alternative[0] + 1
                if best_alternative is not None
                else None,
                "best_other_correlation": best_alternative[1]
                if best_alternative is not None
                else None,
            }
        )
    checks.append(
        _check(
            "channel_identity",
            _worst(identity_results),
            "per-channel correspondence was compared after alignment",
            channels=identity_details,
        )
    )

    envelope_details: List[Dict[str, Any]] = []
    envelope_results: List[str] = []
    for index, alignment in enumerate(alignments):
        expected_envelope, actual_envelope = _rms_envelope_pair(
            expected.samples[index],
            actual.samples[index],
            int(alignment.get("offset_samples", 0)),
            TARGET_SAMPLE_RATE,
            silence_windows,
            gains[index],
        )
        if not expected_envelope:
            result = "PASS"
            reason = "no non-silent envelope windows were applicable"
            correlation = None
        else:
            correlation = _pearson_vectors(expected_envelope, actual_envelope)
            if correlation is None:
                result = "UNKNOWN"
                reason = "envelope correlation is undefined"
            elif correlation < ENVELOPE_CORRELATION_MIN:
                result = "FAIL"
                reason = "20 ms RMS envelope correlation is below 0.95"
            else:
                result = "PASS"
                reason = "20 ms RMS envelope correlation meets policy"
        envelope_results.append(result)
        envelope_details.append(
            {
                "channel": index + 1,
                "result": result,
                "reason": reason,
                "window_count": len(expected_envelope),
                "correlation": correlation,
            }
        )
    checks.append(
        _check(
            "envelope_correlation",
            _worst(envelope_results),
            "gain-normalized 20 ms RMS envelopes were compared per channel",
            channels=envelope_details,
        )
    )

    seam_details: List[Dict[str, Any]] = []
    seam_results: List[str] = []
    restart_details: List[Dict[str, Any]] = []
    restart_results: List[str] = []
    for seam in timeline["seams"]:
        if seam >= min(expected.duration_seconds, actual.duration_seconds):
            seam_details.append(
                {
                    "seam_seconds": seam,
                    "result": "UNKNOWN",
                    "reason": "seam lies outside comparable audio duration",
                    "channels": [],
                }
            )
            seam_results.append("UNKNOWN")
            restart_details.append(
                {
                    "seam_seconds": seam,
                    "result": "UNKNOWN",
                    "reason": "restart window lies outside comparable audio duration",
                    "channels": [],
                }
            )
            restart_results.append("UNKNOWN")
            continue

        per_seam_channels: List[Dict[str, Any]] = []
        per_seam_results: List[str] = []
        per_restart_channels: List[Dict[str, Any]] = []
        per_restart_results: List[str] = []
        for index, alignment in enumerate(alignments):
            offset = int(alignment.get("offset_samples", 0))
            seam_start = int(round((seam - SEAM_HALF_WINDOW_SECONDS) * TARGET_SAMPLE_RATE))
            seam_end = int(round((seam + SEAM_HALF_WINDOW_SECONDS) * TARGET_SAMPLE_RATE))
            left, right = _paired_samples(
                expected.samples[index],
                actual.samples[index],
                offset,
                seam_start,
                seam_end,
                TARGET_SAMPLE_RATE,
                silence_windows,
            )
            seam_score = _pearson_vectors(left, right)
            if seam_score is None:
                seam_result = "UNKNOWN"
                seam_reason = "seam correlation is undefined or has insufficient samples"
            elif seam_score < SEAM_CORRELATION_MIN:
                seam_result = "FAIL"
                seam_reason = "seam PCM correlation is below 0.90"
            else:
                seam_result = "PASS"
                seam_reason = "seam PCM correlation meets policy"
            per_seam_results.append(seam_result)
            per_seam_channels.append(
                {
                    "channel": index + 1,
                    "result": seam_result,
                    "reason": seam_reason,
                    "correlation": seam_score,
                    "compared_samples": len(left),
                }
            )

            restart_start = int(round(seam * TARGET_SAMPLE_RATE))
            restart_end = int(round((seam + RESTART_WINDOW_SECONDS) * TARGET_SAMPLE_RATE))
            correct_left, actual_after = _paired_samples(
                expected.samples[index],
                actual.samples[index],
                offset,
                restart_start,
                restart_end,
                TARGET_SAMPLE_RATE,
                silence_windows,
            )
            actual_start = max(0, restart_start + offset)
            available = min(
                len(correct_left),
                len(expected.samples[index]),
                max(0, len(actual.samples[index]) - actual_start),
            )
            expected_from_zero = list(expected.samples[index][:available])
            correct_left = correct_left[:available]
            actual_after = actual_after[:available]
            correct_score = _pearson_vectors(correct_left, actual_after)
            restart_score = _pearson_vectors(expected_from_zero, actual_after)
            repetition_score = _pearson_vectors(correct_left, expected_from_zero)
            if available < int(0.25 * TARGET_SAMPLE_RATE):
                restart_result = "UNKNOWN"
                restart_reason = "less than 250 ms is available after the seam"
            elif (
                correct_score is None
                or restart_score is None
                or repetition_score is None
            ):
                restart_result = "UNKNOWN"
                restart_reason = "restart comparison is undefined"
            elif restart_score - correct_score >= RESTART_MARGIN:
                restart_result = "FAIL"
                restart_reason = "audio after seam matches track start better by at least 0.05"
            elif (
                abs(repetition_score) >= REPETITIVE_CORRELATION_MIN
                and abs(correct_score - restart_score) < RESTART_MARGIN
            ):
                restart_result = "UNKNOWN"
                restart_reason = "source is highly repetitive and restart scores are inconclusive"
            else:
                restart_result = "PASS"
                restart_reason = "no erroneous music restart was detected"
            per_restart_results.append(restart_result)
            per_restart_channels.append(
                {
                    "channel": index + 1,
                    "result": restart_result,
                    "reason": restart_reason,
                    "correct_position_correlation": correct_score,
                    "track_start_correlation": restart_score,
                    "source_repetition_correlation": repetition_score,
                    "compared_samples": available,
                }
            )
        seam_result = _worst(per_seam_results)
        seam_results.append(seam_result)
        seam_details.append(
            {
                "seam_seconds": seam,
                "result": seam_result,
                "channels": per_seam_channels,
            }
        )
        restart_result = _worst(per_restart_results)
        restart_results.append(restart_result)
        restart_details.append(
            {
                "seam_seconds": seam,
                "result": restart_result,
                "channels": per_restart_channels,
            }
        )

    if not timeline["seams"]:
        checks.append(
            _check(
                "seam_correlation",
                "PASS",
                "no seams were declared or derived",
                applicable=False,
                seams=[],
            )
        )
        checks.append(
            _check(
                "music_restart_detection",
                "PASS",
                "no seams were declared or derived",
                applicable=False,
                seams=[],
            )
        )
    else:
        checks.append(
            _check(
                "seam_correlation",
                _worst(seam_results),
                "±500 ms seam PCM windows were compared per channel",
                applicable=True,
                seams=seam_details,
            )
        )
        checks.append(
            _check(
                "music_restart_detection",
                _worst(restart_results),
                "post-seam audio was compared with the correct position and track start",
                applicable=True,
                seams=restart_details,
            )
        )

    report["checks"] = checks
    report["overall_result"] = _overall(checks)
    _finish_summary(report)
    return report


def _finish_summary(report: Dict[str, Any]) -> None:
    counts = {"pass": 0, "fail": 0, "unknown": 0}
    for check in report.get("checks", []):
        key = str(check.get("result", "UNKNOWN")).lower()
        if key not in counts:
            key = "unknown"
        counts[key] += 1
    report["summary"] = counts


def _unknown_report(fps: float, reason: str) -> Dict[str, Any]:
    report = _base_report(None, fps)
    report["checks"] = [
        _check(
            "input_and_decode",
            "UNKNOWN",
            reason,
        )
    ]
    _finish_summary(report)
    return report


def _atomic_write_json(path_text: str, report: Mapping[str, Any]) -> None:
    target = Path(path_text)
    if target.exists() and target.is_symlink():
        raise VerificationInputError("output path must not be a symbolic link")
    try:
        parent = target.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise VerificationInputError(f"output parent directory does not exist: {exc}") from exc
    if not parent.is_dir():
        raise VerificationInputError("output parent must be a directory")
    encoded = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{target.name}.", suffix=".tmp", dir=parent, delete=False
        ) as handle:
            temp_name = handle.name
            os.chmod(temp_name, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    except OSError as exc:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise VerificationInputError(f"could not write output JSON: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare an expected PCM WAV and a final PCM WAV against timeline seams "
            "and intentional silence, emitting PASS/FAIL/UNKNOWN JSON."
        )
    )
    parser.add_argument("--expected", required=True, help="Expected master PCM WAV")
    parser.add_argument("--actual", required=True, help="Actual final PCM WAV")
    parser.add_argument(
        "--timeline-map",
        required=True,
        help="UTF-8 JSON timeline map containing seams and intentional silence",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=30.0,
        help="Video frame rate used for duration tolerance (default: 30)",
    )
    parser.add_argument(
        "--allow-downmix",
        action="store_true",
        help="Allow an explicit mono downmix when channel counts differ",
    )
    parser.add_argument(
        "--output",
        help="Optional JSON report path. The parent directory must already exist.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.fps) or args.fps <= 0 or args.fps > 240:
        report = _unknown_report(30.0, "fps must be finite and in the range (0, 240]")
    else:
        try:
            report = verify(args)
        except VerificationInputError as exc:
            report = _unknown_report(args.fps, str(exc))
        except (MemoryError, OverflowError) as exc:
            report = _unknown_report(args.fps, f"verification resource limit: {exc}")

    if args.output:
        try:
            _atomic_write_json(args.output, report)
        except VerificationInputError as exc:
            report.setdefault("checks", []).append(
                _check("output_write", "UNKNOWN", str(exc))
            )
            report["output_error"] = str(exc)
            report["overall_result"] = "UNKNOWN"
            _finish_summary(report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report.get("overall_result") == "PASS":
        return 0
    if report.get("overall_result") == "FAIL":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
