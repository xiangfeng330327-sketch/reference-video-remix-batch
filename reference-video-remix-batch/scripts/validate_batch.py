#!/usr/bin/env python3
"""Validate and normalize reference-video-remix batch manifests.

This module is deliberately offline and standard-library only.  It is shared by
``batch_state.py`` and may also be used as a command line validator for JSON or
CSV imports.  Validation never submits work, uploads media, or probes URLs.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence, Union
from urllib.parse import urlsplit


SCHEMA_VERSION = "1.0"
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_JSON_DEPTH = 20
MAX_TEXT_BYTES = 64 * 1024
MAX_BATCH_JOBS = 50
MIN_BATCH_JOBS = 10
MAX_ASSETS = 500
ALLOWED_ASPECT_RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9"}
ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

BATCH_STATUSES = {
    "DRAFT",
    "VALIDATED",
    "AWAITING_CONFIRMATION",
    "RUNNING",
    "COMPLETED_WITH_SUMMARY",
    "PAUSED_FOR_RIGHTS",
    "PAUSED_FOR_INPUT",
    "PAUSED_FOR_AUTH",
    "PARTIALLY_COMPLETED",
}
JOB_STATUSES = {
    "DRAFT",
    "VALIDATED",
    "CONFIRMED",
    "QUEUED",
    "SUBMITTED",
    "GENERATING",
    "COMPOSING",
    "QC_FAST",
    "QC_DEEP",
    "SUCCEEDED",
    "RETRY_QUEUED",
    "AWAITING_REVISION_CONFIRMATION",
    "FAILED_FINAL",
    "PAUSED_FOR_AUTH",
    "PAUSED_FOR_INPUT",
}
SEGMENT_STATUSES = JOB_STATUSES | {"SUBMITTING", "NEEDS_RECONCILIATION"}
MEDIA_STATUSES = {
    "PLANNED",
    "QUEUED",
    "SUBMITTING",
    "SUBMITTED",
    "RUNNING",
    "SUCCEEDED",
    "NEEDS_RECONCILIATION",
    "FAILED_FINAL",
    "PAUSED_FOR_AUTH",
    "PAUSED_FOR_INPUT",
}
CONFIRMATION_STATUSES = {"DRAFT", "AWAITING_CONFIRMATION", "CONFIRMED", "SUPERSEDED"}
QC_RESULTS = {"PASS", "FAIL", "UNKNOWN"}
REQUIRED_QC_CHECK_IDS = {
    "media_integrity",
    "duration",
    "audio_timeline",
    "visual_integrity",
    "identity",
    "text_stability",
    "rights",
}
MODES = {
    "one_reference_many_subjects",
    "multiple_references_mapped_subjects",
    "mapped_references",
    "cartesian_product",
}

CSV_LIST_FIELDS = {
    "subject_assets",
    "preserve",
    "variation",
    "text_overlays",
    "negative_constraints",
}
INPUT_PATH_KEYS = {
    "input_path",
    "local_path",
    "source_path",
    "reference_path",
    "audio_path",
    "subject_path",
    "media_path",
}
OUTPUT_PATH_KEYS = {
    "output_path",
    "output_dir",
    "output_directory",
    "output_filename",
    "result_path",
    "temporary_path",
    "temp_path",
}


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical UTF-8 JSON representation used for hashes."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def atomic_write_json(path: Union[os.PathLike, str], value: Any, mode: int = 0o600) -> None:
    """Atomically write JSON in the destination directory and fsync it."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        try:
            os.fchmod(fd, mode)
            payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
            os.chmod(destination, mode)
            directory_fd = os.open(str(destination.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def _json_depth(value: Any) -> int:
    maximum = 1
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        maximum = max(maximum, depth)
        if maximum > MAX_JSON_DEPTH:
            return maximum
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return maximum


def _object_without_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"DUPLICATE_JSON_KEY: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"NONFINITE_JSON_NUMBER: {value}")


def _walk(value: Any, path: str = "$") -> Iterable[tuple[str, Optional[str], Any]]:
    """Yield ``(path, key, value)`` for all descendants."""

    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, str(key), child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield child_path, None, child
            yield from _walk(child, child_path)


def _has_control(value: str) -> bool:
    return bool(CONTROL_RE.search(value))


def _validate_id(value: Any, path: str, errors: list[dict[str, str]]) -> bool:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        errors.append(
            _issue(
                "INVALID_ID",
                path,
                "must contain only lowercase letters, digits, '-' or '_' and be 1-64 characters",
            )
        )
        return False
    return True


def _is_public_ip_literal(hostname: str) -> bool:
    try:
        parsed = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return True
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def validate_public_url(value: Any) -> Optional[str]:
    """Do syntax/literal-host checks only; DNS and redirects remain runtime work."""

    if not isinstance(value, str) or _has_control(value):
        return "URL must be a control-character-free string"
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return "URL is malformed"
    if parsed.scheme.lower() not in {"http", "https"}:
        return "only http:// and https:// public URLs are accepted"
    if parsed.username is not None or parsed.password is not None:
        return "embedded URL credentials are not accepted"
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return "URL must include a host"
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return "localhost and local-network hosts are not accepted"
    if not _is_public_ip_literal(host):
        return "private, loopback, link-local, reserved, or metadata addresses are not accepted"
    if host in {"169.254.169.254", "metadata.google.internal"}:
        return "cloud metadata addresses are not accepted"
    if port is not None and not (1 <= port <= 65535):
        return "URL port is invalid"
    return None


def _is_within(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _validate_path(
    raw: Any,
    path: str,
    *,
    base_dir: Path,
    allowed_roots: Sequence[Path],
    require_exists: bool,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(raw, str) or not raw:
        errors.append(_issue("INVALID_INPUT_PATH", path, "input path must be a non-empty string"))
        return
    if _has_control(raw):
        errors.append(_issue("UNSAFE_INPUT_PATH", path, "input path contains control characters"))
        return
    candidate = Path(raw)
    if not candidate.is_absolute():
        if any(part == ".." for part in candidate.parts):
            errors.append(_issue("PATH_TRAVERSAL", path, "relative input path may not contain '..'"))
            return
        candidate = base_dir / candidate
    try:
        resolved = candidate.resolve(strict=require_exists)
    except (OSError, RuntimeError) as exc:
        errors.append(_issue("INPUT_PATH_UNAVAILABLE", path, f"cannot resolve input path: {exc}"))
        return
    effective_roots = list(allowed_roots) or [base_dir.resolve()]
    if not _is_within(resolved, effective_roots):
        errors.append(
            _issue(
                "INPUT_PATH_OUTSIDE_ALLOWED_ROOT",
                path,
                "resolved input path is outside explicitly allowed input roots",
            )
        )
        return
    if require_exists and not resolved.is_file():
        errors.append(_issue("INPUT_FILE_NOT_FOUND", path, "input path is not an existing file"))


def _parse_csv_cell(value: str, path: str, errors: list[dict[str, str]]) -> Any:
    if value == "":
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        errors.append(
            _issue("INVALID_CSV_JSON_CELL", path, f"list-valued CSV cell must be JSON: {exc.msg}")
        )
        return []
    if not isinstance(parsed, list):
        errors.append(_issue("INVALID_CSV_LIST_CELL", path, "CSV list-valued cell must decode to an array"))
        return []
    return parsed


def load_manifest(
    path: Union[os.PathLike, str], format_name: str = "auto"
) -> tuple[dict[str, Any], str]:
    """Load a bounded JSON/CSV manifest and return ``(manifest, format)``."""

    source = Path(path)
    size = source.stat().st_size
    if size > MAX_IMPORT_BYTES:
        raise ValueError(f"IMPORT_TOO_LARGE: file is {size} bytes; maximum is {MAX_IMPORT_BYTES}")
    selected = format_name
    if selected == "auto":
        suffix = source.suffix.lower()
        if suffix == ".json":
            selected = "json"
        elif suffix == ".csv":
            selected = "csv"
        else:
            raise ValueError("UNKNOWN_FORMAT: use --format json or --format csv")
    if selected == "json":
        with source.open("r", encoding="utf-8-sig") as handle:
            value = json.load(
                handle,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_nonfinite,
            )
        if not isinstance(value, dict):
            raise ValueError("INVALID_ROOT: JSON root must be an object")
        return value, selected
    if selected != "csv":
        raise ValueError(f"UNKNOWN_FORMAT: unsupported format {selected!r}")

    parse_errors: list[dict[str, str]] = []
    jobs: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required_columns = {"job_id", "reference", "subject_assets"}
        columns = set(reader.fieldnames or [])
        missing = sorted(required_columns - columns)
        if missing:
            raise ValueError(f"MISSING_CSV_COLUMNS: {', '.join(missing)}")
        for row_index, raw_row in enumerate(reader, 2):
            row: dict[str, Any] = {}
            for key, raw_value in raw_row.items():
                if key is None:
                    continue
                value = raw_value or ""
                if key in CSV_LIST_FIELDS:
                    row[key] = _parse_csv_cell(value, f"$.rows[{row_index - 2}].{key}", parse_errors)
                elif key == "target_duration_seconds" and value:
                    try:
                        row[key] = int(value)
                    except ValueError:
                        row[key] = value
                elif value != "":
                    row[key] = value
            jobs.append(row)
    if parse_errors:
        message = "; ".join(f"{item['path']}: {item['message']}" for item in parse_errors)
        raise ValueError(f"INVALID_CSV: {message}")
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": source.stem.lower().replace(" ", "-")[:64] or "imported-batch",
        "status": "DRAFT",
        "mode": "multiple_references_mapped_subjects",
        "target_duration_seconds": 30,
        "jobs": jobs,
        "confirmation_bundles": [],
        "media_operations": [],
    }, selected


def _coerce_subject_assets(job: MutableMapping[str, Any]) -> list[Any]:
    value = job.get("subject_assets")
    if value is None:
        value = job.get("subject_asset_ids")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value else []
        return parsed if isinstance(parsed, list) else []
    return value if isinstance(value, list) else []


def _subject_identifier(subject: Any, index: int) -> str:
    if isinstance(subject, str) and ID_RE.fullmatch(subject):
        return subject
    if isinstance(subject, dict):
        for key in ("asset_id", "subject_id", "id"):
            value = subject.get(key)
            if isinstance(value, str) and ID_RE.fullmatch(value):
                return value
    return f"subject-{index + 1:02d}"


def normalize_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-round-tripped, predictably shaped manifest.

    ``expand_per_subject`` rows are expanded deterministically.  Expansion never
    truncates at 50; callers receive a split plan from :func:`validate_manifest`.
    """

    normalized = json.loads(json.dumps(manifest, ensure_ascii=False, allow_nan=False))
    normalized.setdefault("schema_version", SCHEMA_VERSION)
    normalized.setdefault("status", "DRAFT")
    normalized.setdefault("target_duration_seconds", 30)
    normalized.setdefault("confirmation_bundles", [])
    normalized.setdefault("media_operations", [])
    source_jobs = normalized.get("jobs", normalized.get("rows", []))
    if not isinstance(source_jobs, list):
        normalized["jobs"] = source_jobs
        return normalized

    expanded: list[Any] = []
    for row in source_jobs:
        if not isinstance(row, dict):
            expanded.append(row)
            continue
        job = dict(row)
        subjects = _coerce_subject_assets(job)
        if subjects:
            job["subject_assets"] = subjects
        mapping_mode = job.get("subject_mapping_mode")
        if mapping_mode == "expand_per_subject" and len(subjects) > 1:
            base_id = job.get("job_id")
            for subject_index, subject in enumerate(subjects):
                item = dict(job)
                item["subject_assets"] = [subject]
                item["subject_mapping_mode"] = "subjects_in_same_output"
                if isinstance(base_id, str) and base_id:
                    suffix = _subject_identifier(subject, subject_index)
                    maximum_base = max(1, 64 - len(suffix) - 2)
                    item["job_id"] = f"{base_id[:maximum_base]}--{suffix}"
                expanded.append(item)
        else:
            expanded.append(job)
    normalized["jobs"] = expanded
    normalized.pop("rows", None)
    return normalized


def _validate_hash_pair(
    container: Mapping[str, Any],
    text_key: str,
    hash_key: str,
    path: str,
    errors: list[dict[str, str]],
) -> None:
    text = container.get(text_key)
    digest = container.get(hash_key)
    if digest is None:
        return
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        errors.append(_issue("INVALID_HASH", f"{path}.{hash_key}", "must be a lowercase SHA-256 hex digest"))
        return
    if isinstance(text, str) and sha256_text(text) != digest:
        errors.append(_issue("HASH_MISMATCH", f"{path}.{hash_key}", f"does not match {text_key}"))


def _validate_invocation_hash(
    container: Mapping[str, Any], path: str, errors: list[dict[str, str]]
) -> None:
    spec = container.get("invocation_spec")
    digest = container.get("invocation_hash")
    if digest is None:
        return
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        errors.append(_issue("INVALID_HASH", f"{path}.invocation_hash", "must be a lowercase SHA-256 hex digest"))
    elif isinstance(spec, dict) and sha256_json(spec) != digest:
        errors.append(_issue("HASH_MISMATCH", f"{path}.invocation_hash", "does not match invocation_spec"))


def _validate_time_range(value: Any, path: str, errors: list[dict[str, str]]) -> None:
    if isinstance(value, dict):
        start, end = value.get("start"), value.get("end")
    elif isinstance(value, list) and len(value) == 2:
        start, end = value
    else:
        errors.append(_issue("INVALID_TIME_RANGE", path, "must be {start,end} or a two-number array"))
        return
    if not all(isinstance(item, (int, float)) and math.isfinite(float(item)) for item in (start, end)):
        errors.append(_issue("INVALID_TIME_RANGE", path, "start/end must be finite numbers"))
    elif float(start) < 0 or float(end) <= float(start) or float(end) > 30.000001:
        errors.append(_issue("INVALID_TIME_RANGE", path, "range must satisfy 0 <= start < end <= 30"))


def _validate_assets(
    manifest: Mapping[str, Any], errors: list[dict[str, str]], warnings: list[dict[str, str]]
) -> set[str]:
    rights = manifest.get("rights_confirmation", {})
    if not isinstance(rights, dict):
        errors.append(_issue("INVALID_RIGHTS_CONFIRMATION", "$.rights_confirmation", "must be an object"))
        return set()
    if "confirmed" in rights and not isinstance(rights.get("confirmed"), bool):
        errors.append(_issue("INVALID_RIGHTS_FLAG", "$.rights_confirmation.confirmed", "must be boolean"))
    if rights.get("scope") not in (None, "current_batch"):
        errors.append(_issue("INVALID_CONFIRMATION_SCOPE", "$.rights_confirmation.scope", "must be current_batch"))
    if rights.get("confirmed") is True:
        confirmed_at = rights.get("confirmed_at")
        valid_time = False
        if isinstance(confirmed_at, str) and confirmed_at:
            try:
                parsed = dt.datetime.fromisoformat(
                    confirmed_at[:-1] + "+00:00" if confirmed_at.endswith("Z") else confirmed_at
                )
                valid_time = parsed.tzinfo is not None
            except ValueError:
                valid_time = False
        if rights.get("scope") != "current_batch":
            errors.append(_issue("INVALID_CONFIRMATION_SCOPE", "$.rights_confirmation.scope", "confirmed rights must be scoped to current_batch"))
        if not valid_time:
            errors.append(_issue("INVALID_CONFIRMED_AT", "$.rights_confirmation.confirmed_at", "confirmed rights need a timezone-aware ISO-8601 timestamp"))
    assets = rights.get("assets", []) if isinstance(rights, dict) else []
    if assets is None:
        assets = []
    if not isinstance(assets, list):
        errors.append(_issue("INVALID_ASSETS", "$.rights_confirmation.assets", "must be an array"))
        return set()
    if len(assets) > MAX_ASSETS:
        errors.append(
            _issue("TOO_MANY_ASSETS", "$.rights_confirmation.assets", f"maximum is {MAX_ASSETS}")
        )
    identifiers: set[str] = set()
    for index, asset in enumerate(assets):
        path = f"$.rights_confirmation.assets[{index}]"
        if not isinstance(asset, dict):
            errors.append(_issue("INVALID_ASSET", path, "asset record must be an object"))
            continue
        asset_id = asset.get("asset_id")
        if _validate_id(asset_id, f"{path}.asset_id", errors):
            if asset_id in identifiers:
                errors.append(_issue("DUPLICATE_ASSET_ID", f"{path}.asset_id", "asset_id must be unique"))
            identifiers.add(asset_id)
        required_fields = {
            "asset_type",
            "rights_confirmed",
            "confirmation_scope",
            "processing_destination",
            "upload_required",
            "processing_purpose",
            "retention_status",
            "upload_confirmed",
        }
        for missing_key in sorted(required_fields - set(asset)):
            errors.append(_issue("MISSING_ASSET_FIELD", f"{path}.{missing_key}", "required rights-ledger field is missing"))
        if "rights_confirmed" in asset and not isinstance(asset.get("rights_confirmed"), bool):
            errors.append(_issue("INVALID_RIGHTS_FLAG", f"{path}.rights_confirmed", "must be boolean"))
        if "upload_required" in asset and not isinstance(asset.get("upload_required"), bool):
            errors.append(_issue("INVALID_UPLOAD_FLAG", f"{path}.upload_required", "must be boolean"))
        if "upload_confirmed" in asset and not isinstance(asset.get("upload_confirmed"), bool):
            errors.append(_issue("INVALID_UPLOAD_FLAG", f"{path}.upload_confirmed", "must be boolean"))
        if asset.get("confirmation_scope") != "current_batch":
            errors.append(_issue("INVALID_CONFIRMATION_SCOPE", f"{path}.confirmation_scope", "must be current_batch"))
        content_hash = asset.get("content_hash")
        source_fingerprint = asset.get("source_fingerprint")
        if content_hash is not None and (
            not isinstance(content_hash, str) or not SHA256_RE.fullmatch(content_hash)
        ):
            errors.append(_issue("INVALID_CONTENT_HASH", f"{path}.content_hash", "must be SHA-256 hex"))
        if not content_hash and not source_fingerprint:
            warnings.append(
                _issue("ASSET_WITHOUT_REPRODUCIBLE_HASH", path, "asset has neither content_hash nor source_fingerprint")
            )
        destination = asset.get("processing_destination")
        if not isinstance(destination, str) or not destination.strip():
            errors.append(_issue("INVALID_PROCESSING_DESTINATION", f"{path}.processing_destination", "must be a non-empty string"))
        purpose = asset.get("processing_purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            errors.append(_issue("INVALID_PROCESSING_PURPOSE", f"{path}.processing_purpose", "must be a non-empty string"))
        retention = asset.get("retention_status")
        if not isinstance(retention, str) or not retention.strip():
            errors.append(_issue("INVALID_RETENTION_STATUS", f"{path}.retention_status", "must be a non-empty string; use 'unknown' when not known"))
        if asset.get("upload_required") is True and asset.get("upload_confirmed") is not True:
            warnings.append(
                _issue("UPLOAD_NOT_CONFIRMED", path, "asset requires upload but current-batch upload is not confirmed")
            )
    return identifiers


def _validate_segment(
    segment: Any,
    path: str,
    errors: list[dict[str, str]],
    segment_ids: set[str],
) -> None:
    if not isinstance(segment, dict):
        errors.append(_issue("INVALID_SEGMENT", path, "segment must be an object"))
        return
    segment_id = segment.get("segment_id")
    if _validate_id(segment_id, f"{path}.segment_id", errors):
        if segment_id in segment_ids:
            errors.append(_issue("DUPLICATE_SEGMENT_ID", f"{path}.segment_id", "segment_id must be unique within job"))
        segment_ids.add(segment_id)
    status = segment.get("status", "DRAFT")
    if status not in SEGMENT_STATUSES:
        errors.append(_issue("INVALID_SEGMENT_STATUS", f"{path}.status", f"unknown status {status!r}"))
    time_range = segment.get("target_time_range", segment.get("target_range"))
    if time_range is not None:
        _validate_time_range(time_range, f"{path}.target_time_range", errors)
    _validate_hash_pair(segment, "prompt_text", "prompt_hash", path, errors)
    _validate_invocation_hash(segment, path, errors)
    invocation = segment.get("invocation_spec")
    if invocation is not None and not isinstance(invocation, dict):
        errors.append(_issue("INVALID_INVOCATION_SPEC", f"{path}.invocation_spec", "must be an object"))
    if isinstance(invocation, dict):
        model = invocation.get("model")
        if model is not None and model != "seedance_2.5":
            errors.append(_issue("INVALID_MODEL", f"{path}.invocation_spec.model", "must be seedance_2.5"))
        duration = invocation.get("duration_seconds", invocation.get("duration"))
        if duration is not None and (
            not isinstance(duration, int) or isinstance(duration, bool) or not 5 <= duration <= 30
        ):
            errors.append(_issue("INVALID_GENERATION_DURATION", f"{path}.invocation_spec.duration_seconds", "must be an integer from 5 to 30"))
        ratio = invocation.get("aspect_ratio")
        if ratio is not None and ratio not in ALLOWED_ASPECT_RATIOS:
            errors.append(_issue("INVALID_ASPECT_RATIO", f"{path}.invocation_spec.aspect_ratio", "unsupported aspect ratio"))
    for integer_key in ("technical_retry_count", "quality_revision"):
        value = segment.get(integer_key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(_issue("INVALID_COUNTER", f"{path}.{integer_key}", "must be a non-negative integer"))
    client_id = segment.get("client_submission_id")
    if client_id is not None and (not isinstance(client_id, str) or not client_id or _has_control(client_id)):
        errors.append(_issue("INVALID_CLIENT_SUBMISSION_ID", f"{path}.client_submission_id", "must be a non-empty safe string"))
    if status in {"SUBMITTED", "GENERATING", "SUCCEEDED"} and not segment.get("tool_task_id"):
        errors.append(_issue("MISSING_TOOL_TASK_ID", f"{path}.tool_task_id", f"required for {status}"))
    if status == "SUCCEEDED":
        if not segment.get("output_hash"):
            errors.append(_issue("MISSING_OUTPUT_HASH", f"{path}.output_hash", "required for SUCCEEDED"))
        if not segment.get("output_path"):
            errors.append(_issue("MISSING_OUTPUT_PATH", f"{path}.output_path", "required for SUCCEEDED"))


def _validate_qc_result(
    value: Any,
    path: str,
    errors: list[dict[str, str]],
    *,
    require_complete_pass: bool,
) -> None:
    if not isinstance(value, dict):
        errors.append(_issue("INVALID_QC_RESULT", path, "qc_result must be an object"))
        return
    overall = value.get("overall_result")
    if overall not in QC_RESULTS:
        errors.append(_issue("INVALID_QC_RESULT", f"{path}.overall_result", "must be PASS, FAIL, or UNKNOWN"))
    checks = value.get("checks")
    if not isinstance(checks, list):
        errors.append(_issue("INVALID_QC_CHECKS", f"{path}.checks", "must be an array"))
        return
    seen: set[str] = set()
    observed: dict[str, str] = {}
    for index, check in enumerate(checks):
        check_path = f"{path}.checks[{index}]"
        if not isinstance(check, dict):
            errors.append(_issue("INVALID_QC_CHECK", check_path, "check must be an object"))
            continue
        check_id = check.get("check_id")
        if not isinstance(check_id, str) or not ID_RE.fullmatch(check_id):
            errors.append(_issue("INVALID_QC_CHECK_ID", f"{check_path}.check_id", "must be a safe opaque ID"))
            continue
        if check_id in seen:
            errors.append(_issue("DUPLICATE_QC_CHECK", f"{check_path}.check_id", "check_id must be unique"))
        seen.add(check_id)
        result = check.get("result")
        if result not in QC_RESULTS:
            errors.append(_issue("INVALID_QC_CHECK_RESULT", f"{check_path}.result", "must be PASS, FAIL, or UNKNOWN"))
        else:
            observed[check_id] = result
        method = check.get("method")
        if not isinstance(method, str) or not method.strip():
            errors.append(_issue("MISSING_QC_METHOD", f"{check_path}.method", "a non-empty method is required"))
        for numeric_key in ("coverage", "confidence"):
            number = check.get(numeric_key)
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(float(number))
                or not 0.0 <= float(number) <= 1.0
            ):
                errors.append(_issue("INVALID_QC_RATIO", f"{check_path}.{numeric_key}", "must be a finite number from 0 to 1"))
    if require_complete_pass:
        if overall != "PASS":
            errors.append(_issue("QC_NOT_PASSED", f"{path}.overall_result", "SUCCEEDED requires overall_result PASS"))
        missing = sorted(REQUIRED_QC_CHECK_IDS - set(observed))
        if missing:
            errors.append(_issue("MISSING_REQUIRED_QC_CHECK", f"{path}.checks", "missing: " + ", ".join(missing)))
        failed = sorted(check_id for check_id, result in observed.items() if result != "PASS")
        if failed:
            errors.append(_issue("QC_CHECK_NOT_PASSED", f"{path}.checks", "non-PASS checks: " + ", ".join(failed)))


def _validate_job(
    job: Any,
    index: int,
    *,
    is_batch: bool,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
    known_asset_ids: set[str],
) -> Optional[str]:
    path = f"$.jobs[{index}]"
    if not isinstance(job, dict):
        errors.append(_issue("INVALID_JOB", path, "job must be an object"))
        return None
    job_id = job.get("job_id")
    valid_id = _validate_id(job_id, f"{path}.job_id", errors)
    status = job.get("status", "DRAFT")
    if status not in JOB_STATUSES:
        errors.append(_issue("INVALID_JOB_STATUS", f"{path}.status", f"unknown status {status!r}"))
    duration = job.get("target_duration_seconds", 30)
    if is_batch and duration != 30:
        errors.append(_issue("INVALID_BATCH_DURATION", f"{path}.target_duration_seconds", "batch outputs must be exactly 30 seconds"))
    elif not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
        errors.append(_issue("INVALID_DURATION", f"{path}.target_duration_seconds", "must be a positive number"))
    ratio = job.get("aspect_ratio", "9:16")
    if ratio not in ALLOWED_ASPECT_RATIOS:
        errors.append(_issue("INVALID_ASPECT_RATIO", f"{path}.aspect_ratio", "unsupported batch aspect ratio"))
    reference = job.get("reference_id", job.get("reference"))
    if not isinstance(reference, (str, dict)) or not reference:
        errors.append(_issue("MISSING_REFERENCE", f"{path}.reference", "each job needs a reference"))
    subjects = _coerce_subject_assets(job)
    if not subjects:
        errors.append(_issue("MISSING_SUBJECT", f"{path}.subject_assets", "each job needs at least one subject asset"))
    mapping_mode = job.get("subject_mapping_mode")
    if len(subjects) > 1 and mapping_mode not in {"subjects_in_same_output", "expand_per_subject"}:
        errors.append(_issue("AMBIGUOUS_SUBJECT_MAPPING", f"{path}.subject_mapping_mode", "multiple subjects require an explicit mapping mode"))
    if mapping_mode is not None and mapping_mode not in {"subjects_in_same_output", "expand_per_subject"}:
        errors.append(_issue("INVALID_SUBJECT_MAPPING", f"{path}.subject_mapping_mode", "unknown mapping mode"))
    for subject_index, subject in enumerate(subjects):
        asset_id: Optional[str] = None
        if isinstance(subject, str) and ID_RE.fullmatch(subject):
            asset_id = subject
        elif isinstance(subject, dict):
            possible = subject.get("asset_id")
            if isinstance(possible, str):
                asset_id = possible
        if asset_id and known_asset_ids and asset_id not in known_asset_ids:
            warnings.append(_issue("SUBJECT_ASSET_NOT_IN_RIGHTS_LEDGER", f"{path}.subject_assets[{subject_index}]", "subject asset is not present in rights_confirmation.assets"))
    _validate_hash_pair(job, "prompt_text", "prompt_hash", path, errors)
    _validate_invocation_hash(job, path, errors)
    segments = job.get("segments", [])
    if segments is None:
        segments = []
    if not isinstance(segments, list):
        errors.append(_issue("INVALID_SEGMENTS", f"{path}.segments", "must be an array"))
    else:
        segment_ids: set[str] = set()
        for segment_index, segment in enumerate(segments):
            _validate_segment(segment, f"{path}.segments[{segment_index}]", errors, segment_ids)
    qc_result = job.get("qc_result")
    if qc_result is not None or status == "SUCCEEDED":
        _validate_qc_result(
            qc_result,
            f"{path}.qc_result",
            errors,
            require_complete_pass=status == "SUCCEEDED",
        )
    if status == "SUCCEEDED":
        if not isinstance(segments, list) or not segments:
            errors.append(_issue("MISSING_SUCCESS_SEGMENTS", f"{path}.segments", "SUCCEEDED requires at least one persisted segment"))
        elif any(not isinstance(segment, dict) or segment.get("status") != "SUCCEEDED" for segment in segments):
            errors.append(_issue("INCOMPLETE_SUCCESS_SEGMENTS", f"{path}.segments", "every segment must be SUCCEEDED"))
        output_hash = job.get("output_hash")
        if not isinstance(output_hash, str) or not SHA256_RE.fullmatch(output_hash):
            errors.append(_issue("MISSING_FINAL_OUTPUT_HASH", f"{path}.output_hash", "SUCCEEDED requires a SHA-256 final output_hash"))
        if not isinstance(job.get("output_path"), str) or not job.get("output_path"):
            errors.append(_issue("MISSING_FINAL_OUTPUT_PATH", f"{path}.output_path", "SUCCEEDED requires a final output_path"))
    timeline = job.get("timeline_map")
    if timeline is not None:
        if not isinstance(timeline, list):
            errors.append(_issue("INVALID_TIMELINE_MAP", f"{path}.timeline_map", "must be an array"))
        else:
            for timeline_index, entry in enumerate(timeline):
                entry_path = f"{path}.timeline_map[{timeline_index}]"
                if not isinstance(entry, dict):
                    errors.append(_issue("INVALID_TIMELINE_ENTRY", entry_path, "must be an object"))
                    continue
                if "target_start" in entry or "target_end" in entry:
                    _validate_time_range(
                        {"start": entry.get("target_start"), "end": entry.get("target_end")},
                        entry_path,
                        errors,
                    )
    return job_id if valid_id else None


def _validate_confirmations(
    bundles: Any,
    jobs_by_id: Mapping[str, Mapping[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    if bundles is None:
        return
    if not isinstance(bundles, list):
        errors.append(_issue("INVALID_CONFIRMATION_LEDGER", "$.confirmation_bundles", "must be an array"))
        return
    keys: set[tuple[str, int]] = set()
    revisions_by_id: dict[str, list[int]] = {}
    for index, bundle in enumerate(bundles):
        path = f"$.confirmation_bundles[{index}]"
        if not isinstance(bundle, dict):
            errors.append(_issue("INVALID_CONFIRMATION_BUNDLE", path, "must be an object"))
            continue
        bundle_id = bundle.get("bundle_id")
        revision = bundle.get("revision")
        good_id = _validate_id(bundle_id, f"{path}.bundle_id", errors)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            errors.append(_issue("INVALID_CONFIRMATION_REVISION", f"{path}.revision", "must be an integer >= 1"))
            continue
        if good_id:
            key = (bundle_id, revision)
            if key in keys:
                errors.append(_issue("DUPLICATE_CONFIRMATION_REVISION", path, "bundle_id/revision pair must be unique"))
            keys.add(key)
            revisions_by_id.setdefault(bundle_id, []).append(revision)
        status = bundle.get("status", "DRAFT")
        if status not in CONFIRMATION_STATUSES:
            errors.append(_issue("INVALID_CONFIRMATION_STATUS", f"{path}.status", "unknown confirmation status"))
        if status == "CONFIRMED" and not bundle.get("confirmed_at"):
            errors.append(_issue("MISSING_CONFIRMED_AT", f"{path}.confirmed_at", "confirmed bundle needs confirmed_at"))
        digest = bundle.get("bundle_hash")
        if digest is not None and (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)):
            errors.append(_issue("INVALID_HASH", f"{path}.bundle_hash", "must be a SHA-256 digest"))
        calls = bundle.get("calls", bundle.get("covered_calls", []))
        if not isinstance(calls, list) or not calls:
            errors.append(_issue("MISSING_CONFIRMATION_CALLS", f"{path}.calls", "bundle must cover at least one call"))
            continue
        seen_calls: set[tuple[str, str, str]] = set()
        for call_index, call in enumerate(calls):
            call_path = f"{path}.calls[{call_index}]"
            if not isinstance(call, dict):
                errors.append(_issue("INVALID_CONFIRMATION_CALL", call_path, "must be an object"))
                continue
            job_id, segment_id = call.get("job_id"), call.get("segment_id")
            variant = call.get("variant", "primary")
            call_key = (str(job_id), str(segment_id), str(variant))
            if call_key in seen_calls:
                errors.append(_issue("DUPLICATE_CONFIRMATION_CALL", call_path, "call occurs twice in the same bundle"))
            seen_calls.add(call_key)
            job = jobs_by_id.get(job_id) if isinstance(job_id, str) else None
            if job is None:
                errors.append(_issue("UNKNOWN_CONFIRMATION_JOB", f"{call_path}.job_id", "job_id is not in this batch"))
                continue
            segment = next(
                (
                    item
                    for item in job.get("segments", [])
                    if isinstance(item, dict) and item.get("segment_id") == segment_id
                ),
                None,
            )
            if segment is None:
                errors.append(_issue("UNKNOWN_CONFIRMATION_SEGMENT", f"{call_path}.segment_id", "segment_id is not in the referenced job"))
                continue
            for hash_key in ("prompt_hash", "invocation_hash"):
                digest_value = call.get(hash_key)
                if not isinstance(digest_value, str) or not SHA256_RE.fullmatch(digest_value):
                    errors.append(_issue("INVALID_HASH", f"{call_path}.{hash_key}", "must be a SHA-256 digest"))
                elif variant == "primary" and digest_value != segment.get(hash_key):
                    errors.append(_issue("CONFIRMATION_HASH_MISMATCH", f"{call_path}.{hash_key}", "does not match current segment"))
    for bundle_id, revisions in revisions_by_id.items():
        ordered = sorted(revisions)
        if ordered and ordered != list(range(1, max(ordered) + 1)):
            errors.append(_issue("CONFIRMATION_REVISION_GAP", "$.confirmation_bundles", f"bundle {bundle_id!r} revisions must be contiguous from 1"))


def _validate_media_operations(
    operations: Any,
    jobs_by_id: Mapping[str, Mapping[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    if operations is None:
        return
    if not isinstance(operations, list):
        errors.append(_issue("INVALID_MEDIA_OPERATIONS", "$.media_operations", "must be an array"))
        return
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, operation in enumerate(operations):
        path = f"$.media_operations[{index}]"
        if not isinstance(operation, dict):
            errors.append(_issue("INVALID_MEDIA_OPERATION", path, "must be an object"))
            continue
        operation_id = operation.get("operation_id")
        if _validate_id(operation_id, f"{path}.operation_id", errors):
            if operation_id in by_id:
                errors.append(_issue("DUPLICATE_OPERATION_ID", f"{path}.operation_id", "must be unique"))
            else:
                by_id[operation_id] = operation
        job_id = operation.get("job_id")
        if job_id not in jobs_by_id:
            errors.append(_issue("UNKNOWN_MEDIA_JOB", f"{path}.job_id", "job_id is not in this batch"))
        segment_id = operation.get("segment_id")
        if segment_id is not None and job_id in jobs_by_id:
            job = jobs_by_id[job_id]
            if not any(
                isinstance(segment, dict) and segment.get("segment_id") == segment_id
                for segment in job.get("segments", [])
            ):
                errors.append(_issue("UNKNOWN_MEDIA_SEGMENT", f"{path}.segment_id", "segment_id is not in the referenced job"))
        if not isinstance(operation.get("operation_type", operation.get("type")), str):
            errors.append(_issue("MISSING_OPERATION_TYPE", f"{path}.operation_type", "media operation type is required"))
        dependencies = operation.get("depends_on_operation_ids", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            errors.append(_issue("INVALID_MEDIA_DEPENDENCIES", f"{path}.depends_on_operation_ids", "must be an array of operation IDs"))
        if not isinstance(operation.get("required_for_success", True), bool):
            errors.append(_issue("INVALID_REQUIRED_FLAG", f"{path}.required_for_success", "must be boolean"))
        status = operation.get("status", "PLANNED")
        if status not in MEDIA_STATUSES:
            errors.append(_issue("INVALID_MEDIA_STATUS", f"{path}.status", "unknown media operation status"))
        if status in {"SUBMITTED", "RUNNING", "SUCCEEDED"} and not operation.get("downstream_task_id") and operation.get("execution_kind") == "cloud":
            errors.append(_issue("MISSING_DOWNSTREAM_TASK_ID", f"{path}.downstream_task_id", f"required for cloud operation in {status}"))
        if status == "SUCCEEDED" and (not operation.get("output_hash") or not operation.get("output_path")):
            errors.append(_issue("INCOMPLETE_MEDIA_SUCCESS", path, "SUCCEEDED media operation requires output_path and output_hash"))
        for hash_key in ("parameter_hash", "output_hash"):
            digest = operation.get(hash_key)
            if digest is not None and (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)):
                errors.append(_issue("INVALID_HASH", f"{path}.{hash_key}", "must be a SHA-256 digest"))
        input_hashes = operation.get("input_hashes", [])
        if not isinstance(input_hashes, (list, dict)):
            errors.append(_issue("INVALID_INPUT_HASHES", f"{path}.input_hashes", "must be an array or object"))

    adjacency: dict[str, list[str]] = {}
    for operation_id, operation in by_id.items():
        dependencies = operation.get("depends_on_operation_ids", [])
        if not isinstance(dependencies, list):
            continue
        adjacency[operation_id] = []
        for dependency in dependencies:
            if dependency not in by_id:
                errors.append(_issue("UNKNOWN_MEDIA_DEPENDENCY", f"$.media_operations[{operation_id}].depends_on_operation_ids", f"unknown dependency {dependency!r}"))
                continue
            if by_id[dependency].get("job_id") != operation.get("job_id"):
                errors.append(_issue("CROSS_JOB_MEDIA_DEPENDENCY", f"$.media_operations[{operation_id}].depends_on_operation_ids", "media dependencies must stay within one job"))
            adjacency[operation_id].append(dependency)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            errors.append(_issue("CYCLIC_MEDIA_DAG", "$.media_operations", f"cycle includes {node!r}"))
            return
        visiting.add(node)
        for dependency in adjacency.get(node, []):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for operation_id in adjacency:
        visit(operation_id)


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    base_dir: Union[os.PathLike, str] = ".",
    allowed_input_roots: Sequence[Union[os.PathLike, str]] = (),
    require_batch: bool = False,
    trusted_state: bool = False,
    require_input_files: bool = True,
) -> dict[str, Any]:
    """Validate and normalize a manifest without performing external I/O.

    ``trusted_state`` is reserved for the private state file maintained by this
    package.  It permits private output paths but does not weaken input-path,
    schema, DAG, state, or hash checks.
    """

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(manifest, Mapping):
        errors.append(_issue("INVALID_ROOT", "$", "manifest root must be an object"))
        return {
            "ok": False,
            "status": "invalid",
            "errors": errors,
            "warnings": warnings,
            "job_count": 0,
            "generation_call_count": 0,
            "split_plan": None,
            "normalized": None,
        }
    normalized = normalize_manifest(manifest)
    depth = _json_depth(normalized)
    if depth > MAX_JSON_DEPTH:
        errors.append(_issue("JSON_TOO_DEEP", "$", f"JSON nesting depth {depth} exceeds {MAX_JSON_DEPTH}"))
    for value_path, key, value in _walk(normalized):
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            errors.append(_issue("TEXT_TOO_LARGE", value_path, f"text exceeds {MAX_TEXT_BYTES} UTF-8 bytes"))
        if key in OUTPUT_PATH_KEYS and value not in (None, "") and not trusted_state:
            errors.append(_issue("UNTRUSTED_OUTPUT_PATH", value_path, "imported manifests may not choose output paths"))
        if isinstance(value, str) and key in {"url", "source_url", "reference_url", "audio_url"}:
            message = validate_public_url(value)
            if message:
                errors.append(_issue("UNSAFE_URL", value_path, message))

    schema_version = normalized.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        errors.append(_issue("UNSUPPORTED_SCHEMA_VERSION", "$.schema_version", f"expected {SCHEMA_VERSION!r}"))
    _validate_id(normalized.get("batch_id"), "$.batch_id", errors)
    batch_status = normalized.get("status", "DRAFT")
    if batch_status not in BATCH_STATUSES:
        errors.append(_issue("INVALID_BATCH_STATUS", "$.status", f"unknown status {batch_status!r}"))
    mode = normalized.get("mode")
    if mode not in MODES:
        errors.append(_issue("INVALID_MODE", "$.mode", "unknown or missing batch mapping mode"))
    jobs = normalized.get("jobs")
    if not isinstance(jobs, list):
        errors.append(_issue("INVALID_JOBS", "$.jobs", "jobs must be an array"))
        jobs = []
    job_count = len(jobs)
    is_batch = job_count >= MIN_BATCH_JOBS
    if job_count == 0:
        errors.append(_issue("EMPTY_BATCH", "$.jobs", "at least one job is required"))
    elif job_count < MIN_BATCH_JOBS:
        issue = _issue("BELOW_BATCH_MINIMUM", "$.jobs", f"{job_count} jobs is an ordinary remix, not the 10-50 batch product")
        (errors if require_batch else warnings).append(issue)
    if job_count > MAX_BATCH_JOBS:
        warnings.append(_issue("BATCH_SPLIT_REQUIRED", "$.jobs", f"{job_count} jobs require child batches of at most {MAX_BATCH_JOBS}"))
    root_duration = normalized.get("target_duration_seconds", 30)
    if is_batch and root_duration != 30:
        errors.append(_issue("INVALID_BATCH_DURATION", "$.target_duration_seconds", "batch target duration must be exactly 30 seconds"))

    known_asset_ids = _validate_assets(normalized, errors, warnings)
    job_ids: set[str] = set()
    jobs_by_id: dict[str, Mapping[str, Any]] = {}
    for index, job in enumerate(jobs):
        job_id = _validate_job(
            job,
            index,
            is_batch=is_batch,
            errors=errors,
            warnings=warnings,
            known_asset_ids=known_asset_ids,
        )
        if job_id:
            if job_id in job_ids:
                errors.append(_issue("DUPLICATE_JOB_ID", f"$.jobs[{index}].job_id", "job_id must be unique"))
            else:
                job_ids.add(job_id)
                if isinstance(job, Mapping):
                    jobs_by_id[job_id] = job

    _validate_confirmations(normalized.get("confirmation_bundles", []), jobs_by_id, errors)
    _validate_media_operations(normalized.get("media_operations", []), jobs_by_id, errors)

    base = Path(base_dir).resolve()
    roots: list[Path] = []
    for root_value in allowed_input_roots:
        try:
            roots.append(Path(root_value).resolve(strict=True))
        except OSError as exc:
            errors.append(_issue("INVALID_ALLOWED_ROOT", "$", f"cannot resolve allowed root {root_value!s}: {exc}"))
    for value_path, key, value in _walk(normalized):
        if key in INPUT_PATH_KEYS and value not in (None, ""):
            _validate_path(
                value,
                value_path,
                base_dir=base,
                allowed_roots=roots,
                require_exists=require_input_files,
                errors=errors,
            )

    calls = 0
    for job in jobs:
        if isinstance(job, dict):
            segments = job.get("segments")
            calls += len(segments) if isinstance(segments, list) and segments else 1

    split_plan: Optional[dict[str, Any]] = None
    if job_count > MAX_BATCH_JOBS:
        children = []
        for child_index, start in enumerate(range(0, job_count, MAX_BATCH_JOBS), 1):
            end = min(start + MAX_BATCH_JOBS, job_count)
            children.append(
                {
                    "child_index": child_index,
                    "job_start_index": start,
                    "job_end_index_exclusive": end,
                    "job_count": end - start,
                    "job_ids": [
                        job.get("job_id") if isinstance(job, dict) else None
                        for job in jobs[start:end]
                    ],
                    "requires_separate_confirmation": True,
                }
            )
        split_plan = {
            "total_job_count": job_count,
            "max_jobs_per_child_batch": MAX_BATCH_JOBS,
            "child_batch_count": len(children),
            "children": children,
            "confirmation_policy": "confirm_and_start_at_most_one_child_batch_at_a_time",
        }

    if errors:
        status = "invalid"
        ok = False
    elif split_plan is not None:
        status = "needs_split"
        ok = False
    else:
        status = "valid"
        ok = True
    return {
        "ok": ok,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "job_count": job_count,
        "generation_call_count": calls,
        "split_plan": split_plan,
        "normalized": normalized,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and normalize a reference-video remix JSON/CSV manifest."
    )
    parser.add_argument("manifest", help="JSON or CSV manifest path")
    parser.add_argument("--format", choices=("auto", "json", "csv"), default="auto")
    parser.add_argument(
        "--allowed-input-root",
        action="append",
        default=[],
        help="Allowed input directory after symlink resolution (repeatable)",
    )
    parser.add_argument("--normalized-out", help="Write normalized JSON atomically")
    parser.add_argument("--require-batch", action="store_true", help="Treat fewer than 10 jobs as invalid")
    parser.add_argument("--trusted-state", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-missing-inputs", action="store_true", help="Validate paths without requiring files to exist")
    parser.add_argument("--json", action="store_true", help="Emit JSON (the default; retained for automation clarity)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    source = Path(arguments.manifest)
    try:
        if source.stat().st_size > MAX_IMPORT_BYTES:
            raise ValueError(
                f"IMPORT_TOO_LARGE: file is {source.stat().st_size} bytes; maximum is {MAX_IMPORT_BYTES}"
            )
        manifest, detected_format = load_manifest(source, arguments.format)
        result = validate_manifest(
            manifest,
            base_dir=source.parent,
            allowed_input_roots=arguments.allowed_input_root,
            require_batch=arguments.require_batch,
            trusted_state=arguments.trusted_state,
            require_input_files=not arguments.allow_missing_inputs,
        )
        result["source_format"] = detected_format
        if arguments.normalized_out and result["status"] != "invalid":
            atomic_write_json(arguments.normalized_out, result["normalized"])
            result["normalized_output_written"] = True
    except (OSError, UnicodeError, csv.Error, json.JSONDecodeError, ValueError) as exc:
        result = {
            "ok": False,
            "status": "invalid",
            "errors": [_issue("IMPORT_ERROR", "$", str(exc))],
            "warnings": [],
            "job_count": 0,
            "generation_call_count": 0,
            "split_plan": None,
            "normalized": None,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "valid":
        return 0
    if result["status"] == "needs_split":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
