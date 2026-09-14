#!/usr/bin/env python3
"""Safely probe media capabilities for reference-video-remix-batch.

This module is intentionally a capability probe, not a downloader or a media
service client.  It never opens a network connection and never uploads data.
Cloud use is represented only by a fail-closed consent decision that a caller
may use before invoking a separately configured cloud adapter.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = "1.0"
MAX_CONFIRMATION_BYTES = 1 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 8
SUPPORTED_CLOUD_PROVIDER = "mediakit_cloud"
HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
SAFE_EXECUTABLE_PATH = os.pathsep.join(
    ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
)


class InputError(ValueError):
    """Raised for malformed or unsafe local inputs."""


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _first_nonempty_line(text: str) -> Optional[str]:
    for line in text.splitlines():
        cleaned = " ".join(line.strip().split())
        if cleaned:
            return cleaned[:500]
    return None


def _safe_executable(name: str) -> Tuple[Optional[Path], Optional[str]]:
    # Do not search the caller's PATH: a batch workspace may contain an
    # attacker-controlled executable with a familiar filename.
    candidate = shutil.which(name, path=SAFE_EXECUTABLE_PATH)
    if not candidate:
        return None, f"{name} was not found on PATH"
    try:
        unresolved = Path(candidate)
        resolved = unresolved.resolve(strict=True)
        mode = resolved.stat().st_mode
    except (OSError, RuntimeError) as exc:
        return None, f"unable to resolve {name}: {exc}"
    if not stat.S_ISREG(mode):
        return None, f"resolved {name} is not a regular file"
    if not os.access(resolved, os.X_OK):
        return None, f"resolved {name} is not executable"
    if mode & stat.S_IWOTH:
        return None, f"resolved {name} is world-writable and was rejected"
    return resolved, None


def _run_probe(executable: Path, args: Sequence[str]) -> Dict[str, Any]:
    """Run a fixed, read-only probe without a shell."""

    env = {
        "PATH": os.pathsep.join(
            dict.fromkeys([str(executable.parent), *SAFE_EXECUTABLE_PATH.split(os.pathsep)])
        ),
        "LANG": "C",
        "LC_ALL": "C",
    }
    try:
        completed = subprocess.run(
            [str(executable), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "probe timed out"}
    except OSError as exc:
        return {"ok": False, "reason": f"probe could not start: {exc}"}

    combined = completed.stdout + "\n" + completed.stderr
    if completed.returncode != 0:
        return {
            "ok": False,
            "reason": f"probe exited with status {completed.returncode}",
            "summary": _first_nonempty_line(combined),
        }
    return {
        "ok": True,
        "summary": _first_nonempty_line(combined),
        "output": combined,
    }


def _probe_tool(name: str) -> Tuple[Dict[str, Any], Optional[Path]]:
    executable, error = _safe_executable(name)
    if executable is None:
        return (
            {
                "availability": "UNAVAILABLE",
                "verified": False,
                "reason": error,
            },
            None,
        )
    result = _run_probe(executable, ["-version"])
    if not result["ok"]:
        return (
            {
                "availability": "UNKNOWN",
                "verified": False,
                "reason": result["reason"],
                "summary": result.get("summary"),
            },
            None,
        )
    return (
        {
            "availability": "AVAILABLE",
            "verified": True,
            "executable": executable.name,
            "version_summary": result.get("summary"),
            "probe_method": "fixed local '-version' invocation without shell",
        },
        executable,
    )


def _probe_ffmpeg_features(ffmpeg: Optional[Path]) -> Dict[str, Any]:
    if ffmpeg is None:
        return {
            "probe_status": "UNAVAILABLE",
            "filters": [],
            "encoders": [],
        }

    filters_probe = _run_probe(ffmpeg, ["-hide_banner", "-filters"])
    encoders_probe = _run_probe(ffmpeg, ["-hide_banner", "-encoders"])

    filters: List[str] = []
    if filters_probe.get("ok"):
        for line in filters_probe.get("output", "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and re.fullmatch(r"[.A-Z|]{3,8}", parts[0]):
                filters.append(parts[1])

    encoders: List[str] = []
    if encoders_probe.get("ok"):
        for line in encoders_probe.get("output", "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and re.fullmatch(r"[.A-Z|]{6}", parts[0]):
                encoders.append(parts[1])

    status = "AVAILABLE" if filters_probe.get("ok") and encoders_probe.get("ok") else "UNKNOWN"
    reasons = [
        probe.get("reason")
        for probe in (filters_probe, encoders_probe)
        if not probe.get("ok") and probe.get("reason")
    ]
    return {
        "probe_status": status,
        "filters": sorted(set(filters)),
        "encoders": sorted(set(encoders)),
        "reason": "; ".join(reasons) if reasons else None,
    }


def _capability(
    available: bool,
    provider: Optional[str],
    reason: str,
    *,
    evidence: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    return {
        "availability": "AVAILABLE" if available else "UNAVAILABLE",
        "provider": provider if available else None,
        "processing_destination": "local" if available else None,
        "upload_required": False,
        "evidence": sorted(set(evidence or [])),
        "reason": reason,
    }


def _local_capabilities(
    ffmpeg_info: Mapping[str, Any],
    ffprobe_info: Mapping[str, Any],
    ffmpeg_features: Mapping[str, Any],
    manual_visual_check: bool,
) -> Dict[str, Any]:
    ffmpeg_ok = ffmpeg_info.get("availability") == "AVAILABLE"
    ffprobe_ok = ffprobe_info.get("availability") == "AVAILABLE"
    filters = set(ffmpeg_features.get("filters", []))
    encoders = set(ffmpeg_features.get("encoders", []))

    concat_ok = ffmpeg_ok and "concat" in filters
    audio_filters = {"amix", "atrim", "asetpts", "aresample"}
    audio_ok = ffmpeg_ok and audio_filters.issubset(filters)
    drawtext_ok = ffmpeg_ok and "drawtext" in filters
    wav_ok = ffmpeg_ok and bool({"pcm_s16le", "pcm_s24le", "pcm_f32le"} & encoders)

    return {
        "probe_media": _capability(
            ffprobe_ok,
            "local_ffprobe",
            "ffprobe version probe succeeded"
            if ffprobe_ok
            else "a verified local ffprobe executable is required",
            evidence=["ffprobe-version"] if ffprobe_ok else [],
        ),
        "extract_frames": _capability(
            ffmpeg_ok,
            "local_ffmpeg",
            "ffmpeg version probe succeeded"
            if ffmpeg_ok
            else "a verified local ffmpeg executable is required",
            evidence=["ffmpeg-version"] if ffmpeg_ok else [],
        ),
        "concat_video": _capability(
            concat_ok,
            "local_ffmpeg",
            "ffmpeg concat filter is available"
            if concat_ok
            else "the local ffmpeg concat filter was not verified",
            evidence=["filter:concat"] if concat_ok else [],
        ),
        "replace_or_mix_audio": _capability(
            audio_ok,
            "local_ffmpeg",
            "required audio filters are available"
            if audio_ok
            else "required local filters were not all verified: "
            + ", ".join(sorted(audio_filters - filters)),
            evidence=[f"filter:{item}" for item in audio_filters]
            if audio_ok
            else [],
        ),
        "overlay_text": _capability(
            drawtext_ok,
            "local_ffmpeg",
            "ffmpeg drawtext filter is available"
            if drawtext_ok
            else "the local ffmpeg drawtext filter was not verified",
            evidence=["filter:drawtext"] if drawtext_ok else [],
        ),
        "decode_audio_wav": _capability(
            wav_ok,
            "local_ffmpeg",
            "a PCM WAV encoder is available"
            if wav_ok
            else "no supported local PCM WAV encoder was verified",
            evidence=[f"encoder:{item}" for item in sorted({"pcm_s16le", "pcm_s24le", "pcm_f32le"} & encoders)]
            if wav_ok
            else [],
        ),
        "visual_check": {
            "availability": "AVAILABLE" if manual_visual_check else "UNAVAILABLE",
            "provider": "manual_full_playback" if manual_visual_check else None,
            "processing_destination": "local" if manual_visual_check else None,
            "upload_required": False,
            "evidence": ["operator-declared-manual-review-entrypoint"]
            if manual_visual_check
            else [],
            "reason": (
                "a manual full-playback review entry point was explicitly declared"
                if manual_visual_check
                else "no local semantic visual checker or manual review entry point was declared"
            ),
        },
    }


def _safe_read_json(path_text: str) -> Tuple[Dict[str, Any], str]:
    path = Path(path_text)
    try:
        if path.is_symlink():
            raise InputError("cloud confirmation file must not be a symbolic link")
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except InputError:
        raise
    except (OSError, RuntimeError) as exc:
        raise InputError(f"cannot open cloud confirmation file: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise InputError("cloud confirmation path must name a regular file")
    if info.st_size > MAX_CONFIRMATION_BYTES:
        raise InputError(
            f"cloud confirmation file exceeds {MAX_CONFIRMATION_BYTES} bytes"
        )
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read cloud confirmation file: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InputError(f"cloud confirmation is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InputError("cloud confirmation JSON must be an object")
    stack: List[Tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > 20:
            raise InputError("cloud confirmation exceeds maximum nesting depth 20")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value, _sha256_bytes(raw)


def _confirmation_section(data: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("cloud_upload_confirmation", "rights_confirmation"):
        candidate = data.get(key)
        if isinstance(candidate, dict):
            return candidate
    return data


def _evaluate_cloud_gate(
    provider: Optional[str],
    confirmation_file: Optional[str],
    required_asset_ids: Sequence[str],
) -> Tuple[Dict[str, Any], bool]:
    base: Dict[str, Any] = {
        "provider": provider,
        "decision": "NOT_REQUESTED" if provider is None else "DENIED",
        "upload_performed": False,
        "network_used": False,
        "required_asset_ids": sorted(set(required_asset_ids)),
        "confirmation_file_sha256": None,
        "validated_assets": [],
        "reasons": [],
        "note": "This probe never invokes a cloud service; ALLOWED only authorizes a separately configured adapter.",
    }
    if provider is None:
        if confirmation_file or required_asset_ids:
            base["decision"] = "DENIED"
            base["reasons"].append(
                "cloud confirmation arguments require --cloud-provider"
            )
            return base, False
        return base, True

    if provider != SUPPORTED_CLOUD_PROVIDER:
        base["reasons"].append("unsupported cloud provider")
        return base, False
    if not confirmation_file:
        base["reasons"].append("no explicit cloud confirmation JSON was provided")
        return base, False
    if not required_asset_ids:
        base["reasons"].append(
            "at least one --cloud-asset-id is required; blanket upload consent is not accepted"
        )
        return base, False
    if len(set(required_asset_ids)) != len(required_asset_ids):
        base["reasons"].append("duplicate --cloud-asset-id values are not allowed")
        return base, False

    try:
        data, file_hash = _safe_read_json(confirmation_file)
    except InputError as exc:
        base["reasons"].append(str(exc))
        return base, False
    base["confirmation_file_sha256"] = file_hash
    section = _confirmation_section(data)

    if section.get("confirmed") is not True:
        base["reasons"].append("confirmation is not explicitly marked confirmed=true")
    if section.get("scope") != "current_batch":
        base["reasons"].append("confirmation scope must be current_batch")
    if not isinstance(section.get("confirmed_at"), str) or not section.get(
        "confirmed_at", ""
    ).strip():
        base["reasons"].append("confirmation is missing confirmed_at")

    assets = section.get("assets")
    if not isinstance(assets, list):
        base["reasons"].append("confirmation assets must be a list")
        assets = []

    by_id: Dict[str, Mapping[str, Any]] = {}
    duplicates = set()
    for item in assets:
        if not isinstance(item, dict) or not isinstance(item.get("asset_id"), str):
            continue
        asset_id = item["asset_id"]
        if asset_id in by_id:
            duplicates.add(asset_id)
        by_id[asset_id] = item
    if duplicates:
        base["reasons"].append(
            "duplicate asset records: " + ", ".join(sorted(duplicates))
        )

    for asset_id in required_asset_ids:
        asset = by_id.get(asset_id)
        if asset is None:
            base["reasons"].append(f"asset {asset_id!r} has no confirmation record")
            continue
        asset_errors: List[str] = []
        content_hash = asset.get("content_hash")
        if not isinstance(content_hash, str) or not HASH_RE.fullmatch(content_hash):
            asset_errors.append("missing or invalid SHA-256 content_hash")
        if asset.get("rights_confirmed") is not True:
            asset_errors.append("rights_confirmed is not true")
        if asset.get("confirmation_scope") != "current_batch":
            asset_errors.append("confirmation_scope is not current_batch")
        if asset.get("processing_destination") != provider:
            asset_errors.append("processing_destination does not match provider")
        if asset.get("upload_required") is not True:
            asset_errors.append("upload_required is not true")
        if asset.get("upload_confirmed") is not True:
            asset_errors.append("upload_confirmed is not true")
        purpose = asset.get("processing_purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            asset_errors.append("processing_purpose is missing")
        retention = asset.get("retention_status")
        if not isinstance(retention, str) or not retention.strip():
            asset_errors.append("retention_status is missing")
        if asset_errors:
            base["reasons"].append(
                f"asset {asset_id!r}: " + "; ".join(asset_errors)
            )
        else:
            base["validated_assets"].append(asset_id)

    if not base["reasons"]:
        base["decision"] = "ALLOWED"
        return base, True
    return base, False


def _atomic_write_json(path_text: str, payload: Mapping[str, Any]) -> None:
    target = Path(path_text)
    if target.exists() and target.is_symlink():
        raise InputError("output path must not be a symbolic link")
    try:
        parent = target.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InputError(f"output parent directory does not exist: {exc}") from exc
    if not parent.is_dir():
        raise InputError("output parent must be a directory")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
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
        raise InputError(f"could not write output JSON: {exc}") from exc


def build_report(args: argparse.Namespace) -> Tuple[Dict[str, Any], bool]:
    ffmpeg_info, ffmpeg = _probe_tool("ffmpeg")
    ffprobe_info, _ffprobe = _probe_tool("ffprobe")
    features = _probe_ffmpeg_features(ffmpeg)
    capabilities = _local_capabilities(
        ffmpeg_info,
        ffprobe_info,
        features,
        args.manual_visual_check,
    )
    cloud_gate, gate_ok = _evaluate_cloud_gate(
        args.cloud_provider,
        args.cloud_confirmation,
        args.cloud_asset_id,
    )
    missing = [
        name
        for name, details in capabilities.items()
        if details.get("availability") != "AVAILABLE"
    ]
    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report_type": "media-capabilities",
        "generated_at": _utc_now(),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.system().lower(),
        },
        "safety": {
            "network_used": False,
            "upload_performed": False,
            "fetching_supported": False,
            "private_api_supported": False,
            "shell_used": False,
        },
        "selection_policy": {
            "mode": "local_first",
            "cloud_is_fallback_only": True,
            "cloud_requires_per_asset_confirmation": True,
        },
        "local_tools": {
            "ffmpeg": ffmpeg_info,
            "ffprobe": ffprobe_info,
            "ffmpeg_feature_probe": {
                "availability": features.get("probe_status"),
                "verified_filters": features.get("filters", []),
                "verified_encoders": features.get("encoders", []),
                "reason": features.get("reason"),
            },
        },
        "capabilities": capabilities,
        "cloud_upload_gate": cloud_gate,
        "overall_readiness": "READY" if not missing else "PARTIAL",
        "missing_required_capabilities": missing,
    }
    return report, gate_ok


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe local media capabilities and evaluate, without using the network, "
            "an optional per-asset Cloud upload confirmation gate."
        )
    )
    parser.add_argument(
        "--output",
        help="Optional path for media-capabilities JSON. The parent directory must exist.",
    )
    parser.add_argument(
        "--manual-visual-check",
        action="store_true",
        help="Declare that a local manual full-playback review entry point is available.",
    )
    parser.add_argument(
        "--cloud-provider",
        choices=[SUPPORTED_CLOUD_PROVIDER],
        help="Evaluate a consent gate for this separately configured Cloud provider.",
    )
    parser.add_argument(
        "--cloud-confirmation",
        help=(
            "Local UTF-8 JSON containing confirmed=true, scope=current_batch, "
            "confirmed_at, and per-asset upload fields. It is read only."
        ),
    )
    parser.add_argument(
        "--cloud-asset-id",
        action="append",
        default=[],
        help="Asset ID that the Cloud operation would upload; repeat for every asset.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    report, gate_ok = build_report(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        try:
            _atomic_write_json(args.output, report)
        except InputError as exc:
            report["output_error"] = str(exc)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 2
    print(rendered)
    if args.cloud_provider is not None and not gate_ok:
        return 3
    if args.cloud_provider is None and not gate_ok:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
