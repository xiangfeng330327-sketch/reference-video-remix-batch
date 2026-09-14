#!/usr/bin/env python3
"""Deterministic private-state transitions for reference video remix batches.

The program is an offline orchestration helper.  It records intent and observed
results but never submits, queries, uploads, or edits media itself.  Every state
mutation is protected by a sidecar advisory lock and an atomic fsync+replace.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from validate_batch import (
    ID_RE,
    JOB_STATUSES,
    MAX_IMPORT_BYTES,
    MEDIA_STATUSES,
    QC_RESULTS,
    REQUIRED_QC_CHECK_IDS,
    SEGMENT_STATUSES,
    SHA256_RE,
    atomic_write_json,
    sha256_json,
    sha256_text,
    validate_manifest,
)
from build_confirmation_bundle import build_bundle_material


TERMINAL_SEGMENT_STATUSES = {"SUCCEEDED", "FAILED_FINAL"}
ACTIVE_SEGMENT_STATUSES = {
    "QUEUED",
    "SUBMITTING",
    "SUBMITTED",
    "GENERATING",
    "NEEDS_RECONCILIATION",
    "RETRY_QUEUED",
}
SUBMITTABLE_SEGMENT_STATUSES = {"CONFIRMED", "QUEUED", "RETRY_QUEUED"}
TERMINAL_JOB_STATUSES = {"SUCCEEDED", "FAILED_FINAL"}
TERMINAL_MEDIA_STATUSES = {"SUCCEEDED", "FAILED_FINAL"}

SEGMENT_TRANSITIONS = {
    "DRAFT": {"VALIDATED", "PAUSED_FOR_INPUT", "AWAITING_REVISION_CONFIRMATION"},
    "VALIDATED": {"CONFIRMED", "PAUSED_FOR_INPUT", "AWAITING_REVISION_CONFIRMATION"},
    "CONFIRMED": {"QUEUED", "SUBMITTING", "AWAITING_REVISION_CONFIRMATION", "PAUSED_FOR_AUTH", "PAUSED_FOR_INPUT"},
    "QUEUED": {"SUBMITTING", "CONFIRMED", "PAUSED_FOR_AUTH", "PAUSED_FOR_INPUT", "FAILED_FINAL"},
    "SUBMITTING": {"SUBMITTED", "NEEDS_RECONCILIATION", "FAILED_FINAL", "PAUSED_FOR_AUTH"},
    "SUBMITTED": {"GENERATING", "SUCCEEDED", "NEEDS_RECONCILIATION", "FAILED_FINAL", "PAUSED_FOR_AUTH"},
    "GENERATING": {"SUCCEEDED", "NEEDS_RECONCILIATION", "FAILED_FINAL", "PAUSED_FOR_AUTH"},
    "RETRY_QUEUED": {"QUEUED", "SUBMITTING", "FAILED_FINAL", "AWAITING_REVISION_CONFIRMATION"},
    "NEEDS_RECONCILIATION": {"SUBMITTED", "GENERATING", "SUCCEEDED", "FAILED_FINAL", "PAUSED_FOR_AUTH"},
    "PAUSED_FOR_AUTH": {"NEEDS_RECONCILIATION", "CONFIRMED", "QUEUED", "FAILED_FINAL"},
    "PAUSED_FOR_INPUT": {"VALIDATED", "AWAITING_REVISION_CONFIRMATION", "FAILED_FINAL"},
    "AWAITING_REVISION_CONFIRMATION": {"CONFIRMED", "FAILED_FINAL"},
    "FAILED_FINAL": set(),
    "SUCCEEDED": set(),
}

MEDIA_TRANSITIONS = {
    "PLANNED": {"QUEUED", "SUBMITTING", "RUNNING", "SUCCEEDED", "PAUSED_FOR_INPUT", "FAILED_FINAL"},
    "QUEUED": {"SUBMITTING", "RUNNING", "SUCCEEDED", "PAUSED_FOR_AUTH", "PAUSED_FOR_INPUT", "FAILED_FINAL"},
    "SUBMITTING": {"SUBMITTED", "NEEDS_RECONCILIATION", "FAILED_FINAL", "PAUSED_FOR_AUTH"},
    "SUBMITTED": {"RUNNING", "SUCCEEDED", "NEEDS_RECONCILIATION", "FAILED_FINAL", "PAUSED_FOR_AUTH"},
    "RUNNING": {"SUCCEEDED", "NEEDS_RECONCILIATION", "FAILED_FINAL", "PAUSED_FOR_AUTH"},
    "NEEDS_RECONCILIATION": {"SUBMITTED", "RUNNING", "SUCCEEDED", "FAILED_FINAL", "PAUSED_FOR_AUTH"},
    "PAUSED_FOR_AUTH": {"NEEDS_RECONCILIATION", "QUEUED", "FAILED_FINAL"},
    "PAUSED_FOR_INPUT": {"PLANNED", "QUEUED", "FAILED_FINAL"},
    "FAILED_FINAL": set(),
    "SUCCEEDED": set(),
}

IMMUTABLE_MEDIA_FIELDS = {
    "operation_id",
    "job_id",
    "segment_id",
    "operation_type",
    "type",
    "depends_on_operation_ids",
    "required_for_success",
    "input_hashes",
    "parameter_hash",
    "execution_kind",
}
COLLECTION_MARKER_RE = re.compile(
    rb"<!-- reference-video-remix-batch:collection-sha256=([0-9a-f]{64}) -->"
)


class StateError(ValueError):
    """A deterministic, user-correctable state or transition error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _reject_constant(value: str) -> None:
    raise StateError("NONFINITE_JSON", "non-finite JSON number is not allowed: %s" % value)


def _object_without_duplicates(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateError("DUPLICATE_JSON_KEY", "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json_object(path: Path, maximum_bytes: int = MAX_IMPORT_BYTES) -> Dict[str, Any]:
    try:
        if path.is_symlink():
            raise StateError("SYMLINK_STATE_REJECTED", "state and record files may not be symbolic links")
        size = path.stat().st_size
        if size > maximum_bytes:
            raise StateError("STATE_TOO_LARGE", "file exceeds %d bytes" % maximum_bytes)
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_constant,
            )
    except StateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise StateError("STATE_READ_ERROR", "cannot read valid state JSON: %s" % exc)
    if not isinstance(value, dict):
        raise StateError("INVALID_STATE_ROOT", "state JSON root must be an object")
    return value


def _validation_message(result: Mapping[str, Any]) -> str:
    issues = result.get("errors", [])
    if not issues:
        return "state did not pass validation"
    return "; ".join(
        "%s %s: %s" % (item.get("code"), item.get("path"), item.get("message"))
        for item in issues[:8]
        if isinstance(item, dict)
    )


def validate_private_state(state: Mapping[str, Any], state_path: Path) -> Dict[str, Any]:
    result = validate_manifest(
        state,
        base_dir=state_path.parent,
        allowed_input_roots=[state_path.parent],
        trusted_state=True,
        require_input_files=False,
    )
    if result.get("status") == "needs_split":
        raise StateError(
            "BATCH_SPLIT_REQUIRED",
            "active private state has more than 50 jobs; split and confirm one child batch first",
        )
    if not result.get("ok"):
        raise StateError("INVALID_PRIVATE_STATE", _validation_message(result))
    normalized = result.get("normalized")
    if not isinstance(normalized, dict):
        raise StateError("INVALID_PRIVATE_STATE", "validator did not return normalized state")
    _assert_success_invariants(normalized, state_path)
    return normalized


def _lock_path(state_path: Path) -> Path:
    return state_path.with_name(".%s.lock" % state_path.name)


@contextmanager
def locked_state(state_path: Path) -> Iterator[Dict[str, Any]]:
    """Lock, load, validate, yield, revalidate and atomically persist state."""

    state_path = state_path.resolve(strict=True)
    lock_path = _lock_path(state_path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        raise StateError("LOCK_ERROR", "cannot open private state lock: %s" % exc)
    try:
        os.fchmod(lock_fd, stat.S_IRUSR | stat.S_IWUSR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = validate_private_state(load_json_object(state_path), state_path)
        yield state
        validated = validate_private_state(state, state_path)
        state.clear()
        state.update(validated)
        atomic_write_json(state_path, state, mode=0o600)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def read_state(state_path: Path) -> Dict[str, Any]:
    resolved = state_path.resolve(strict=True)
    return validate_private_state(load_json_object(resolved), resolved)


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise StateError("INVALID_ID", "%s must be a safe 1-64 character ID" % field)
    return value


def _safe_client_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
        raise StateError("INVALID_CLIENT_SUBMISSION_ID", "client_submission_id must be a non-empty string of at most 256 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise StateError("INVALID_CLIENT_SUBMISSION_ID", "client_submission_id contains control characters")
    return value


def _job_lookup(state: Mapping[str, Any], job_id: str) -> MutableMapping[str, Any]:
    for job in state.get("jobs", []):
        if isinstance(job, dict) and job.get("job_id") == job_id:
            return job
    raise StateError("UNKNOWN_JOB", "unknown job_id: %s" % job_id)


def _materialize_full_segment(job: MutableMapping[str, Any]) -> None:
    segments = job.get("segments")
    if isinstance(segments, list) and segments:
        return
    required = ("prompt_text", "prompt_hash", "invocation_spec", "invocation_hash")
    if not all(key in job for key in required):
        return
    segment: Dict[str, Any] = {
        "segment_id": "full",
        "target_time_range": {"start": 0, "end": 30},
        "prompt_text": job["prompt_text"],
        "prompt_hash": job["prompt_hash"],
        "invocation_spec": job["invocation_spec"],
        "invocation_hash": job["invocation_hash"],
        "reference_asset_ids": job.get("reference_asset_ids", []),
        "status": job.get("status", "DRAFT") if job.get("status") in SEGMENT_STATUSES else "DRAFT",
        "technical_retry_count": 0,
        "quality_revision": 0,
        "client_submission_id": None,
        "tool_task_id": None,
        "submitted_at": None,
        "last_reconciled_at": None,
        "output_path": None,
        "output_hash": None,
        "error": None,
    }
    job["segments"] = [segment]


def _all_segments(state: MutableMapping[str, Any]) -> Iterable[Tuple[MutableMapping[str, Any], MutableMapping[str, Any]]]:
    for job in state.get("jobs", []):
        if not isinstance(job, dict):
            continue
        _materialize_full_segment(job)
        segments = job.get("segments", [])
        if isinstance(segments, list):
            for segment in segments:
                if isinstance(segment, dict):
                    yield job, segment


def _segment_lookup(
    state: MutableMapping[str, Any], job_id: str, segment_id: str
) -> Tuple[MutableMapping[str, Any], MutableMapping[str, Any]]:
    job = _job_lookup(state, job_id)
    _materialize_full_segment(job)
    for segment in job.get("segments", []):
        if isinstance(segment, dict) and segment.get("segment_id") == segment_id:
            return job, segment
    raise StateError("UNKNOWN_SEGMENT", "unknown segment_id %s for job %s" % (segment_id, job_id))


def _transition(
    record: MutableMapping[str, Any],
    target: str,
    transitions: Mapping[str, set],
    kind: str,
) -> None:
    current = record.get("status")
    if current == target:
        return
    if current not in transitions:
        raise StateError("INVALID_%s_STATUS" % kind.upper(), "unknown current %s status: %r" % (kind, current))
    if target not in transitions[current]:
        raise StateError(
            "INVALID_%s_TRANSITION" % kind.upper(),
            "%s may not transition from %s to %s" % (kind, current, target),
        )
    record["status"] = target


def _bundle_calls(bundle: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    calls = bundle.get("calls", bundle.get("covered_calls", []))
    return calls if isinstance(calls, list) else []


def _bundle_lookup(
    state: Mapping[str, Any], bundle_id: Any, revision: Any
) -> Optional[Mapping[str, Any]]:
    for bundle in state.get("confirmation_bundles", []):
        if (
            isinstance(bundle, dict)
            and bundle.get("bundle_id") == bundle_id
            and bundle.get("revision") == revision
        ):
            return bundle
    return None


def _bundle_file_valid(bundle: Mapping[str, Any]) -> bool:
    raw_path = bundle.get("private_bundle_path", bundle.get("bundle_path"))
    file_hash = bundle.get("file_hash", bundle.get("file_sha256"))
    collection_hash = bundle.get("bundle_hash", bundle.get("collection_sha256"))
    if (
        not isinstance(raw_path, str)
        or not isinstance(file_hash, str)
        or not SHA256_RE.fullmatch(file_hash)
        or not isinstance(collection_hash, str)
        or not SHA256_RE.fullmatch(collection_hash)
    ):
        return False
    try:
        path = Path(raw_path)
        if path.is_symlink() or not path.is_file():
            return False
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            return False
        document = path.read_bytes()
    except OSError:
        return False
    if hashlib.sha256(document).hexdigest() != file_hash:
        return False
    marker = COLLECTION_MARKER_RE.search(document)
    return marker is not None and marker.group(1).decode("ascii") == collection_hash


def confirmation_valid(state: Mapping[str, Any], job_id: str, segment: Mapping[str, Any]) -> bool:
    bundle = _bundle_lookup(
        state, segment.get("confirmation_bundle_id"), segment.get("confirmation_revision")
    )
    if (
        bundle is None
        or bundle.get("status") != "CONFIRMED"
        or not bundle.get("confirmed_at")
        or not _bundle_file_valid(bundle)
    ):
        return False
    for call in _bundle_calls(bundle):
        if not isinstance(call, dict):
            continue
        variant = call.get("variant", "primary")
        if (
            variant == "primary"
            and call.get("job_id") == job_id
            and call.get("segment_id") == segment.get("segment_id")
            and call.get("prompt_hash") == segment.get("prompt_hash")
            and call.get("invocation_hash") == segment.get("invocation_hash")
        ):
            return True
    return False


def _timezone_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _required_asset_ids(state: Mapping[str, Any]) -> set:
    """Collect opaque asset IDs used by jobs, segments, and invocation specs."""

    required: set = set()
    scalar_keys = {
        "reference_id",
        "audio_source",
        "audio_source_id",
        "music_asset_id",
        "font_asset_id",
        "trademark_asset_id",
        "voice_asset_id",
    }
    collection_keys = {
        "subject_assets",
        "subject_asset_ids",
        "reference_assets",
        "reference_asset_ids",
        "audio_assets",
        "audio_asset_ids",
        "font_assets",
        "font_asset_ids",
        "trademark_assets",
        "trademark_asset_ids",
        "voice_assets",
        "voice_asset_ids",
    }

    def add(value: Any) -> None:
        if isinstance(value, str) and ID_RE.fullmatch(value):
            required.add(value)
        elif isinstance(value, dict):
            candidate = value.get("asset_id")
            if isinstance(candidate, str) and ID_RE.fullmatch(candidate):
                required.add(candidate)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "asset_id" or key.endswith("_asset_id") or key in scalar_keys:
                    add(item)
                if key.endswith("_asset_ids") or key in collection_keys:
                    if isinstance(item, list):
                        for member in item:
                            add(member)
                    else:
                        add(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for job in state.get("jobs", []):
        if isinstance(job, dict):
            visit(job)
    return required


def _rights_ready(state: Mapping[str, Any]) -> Tuple[bool, str]:
    rights = state.get("rights_confirmation")
    if not isinstance(rights, dict) or rights.get("confirmed") is not True:
        return False, "current-batch rights confirmation is missing"
    if rights.get("scope") != "current_batch":
        return False, "rights confirmation scope is not current_batch"
    if not _timezone_timestamp(rights.get("confirmed_at")):
        return False, "rights confirmation needs a timezone-aware confirmed_at"
    assets = rights.get("assets", [])
    if not isinstance(assets, list):
        return False, "rights asset ledger is invalid"
    by_id = {
        asset.get("asset_id"): asset
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("asset_id"), str)
    }
    for asset in assets:
        if not isinstance(asset, dict):
            return False, "rights asset ledger contains an invalid record"
        if asset.get("rights_confirmed") is not True:
            return False, "one or more assets lack rights confirmation"
        if asset.get("confirmation_scope") != "current_batch":
            return False, "one or more asset confirmations are outside the current batch"
        if not isinstance(asset.get("asset_type"), str) or not asset.get("asset_type", "").strip():
            return False, "one or more assets lack asset_type"
        if not isinstance(asset.get("processing_destination"), str) or not asset.get("processing_destination", "").strip():
            return False, "one or more assets lack processing_destination"
        if not isinstance(asset.get("processing_purpose"), str) or not asset.get("processing_purpose", "").strip():
            return False, "one or more assets lack processing_purpose"
        if not isinstance(asset.get("retention_status"), str) or not asset.get("retention_status", "").strip():
            return False, "one or more assets lack retention_status"
        if not isinstance(asset.get("upload_required"), bool) or not isinstance(asset.get("upload_confirmed"), bool):
            return False, "one or more assets have invalid upload flags"
        if asset.get("upload_required") is True and asset.get("upload_confirmed") is not True:
            return False, "one or more required uploads are not confirmed"
        if not asset.get("content_hash") and not asset.get("source_fingerprint"):
            return False, "one or more assets lack a reproducible hash or provisional fingerprint"
        if asset.get("upload_required") is True and not asset.get("content_hash"):
            return False, "uploaded assets require a local content_hash"
    required_asset_ids = _required_asset_ids(state)
    missing = sorted(asset_id for asset_id in required_asset_ids if asset_id not in by_id)
    if missing:
        return False, "rights ledger is missing %d referenced asset(s)" % len(missing)
    return True, "ready"


def _assert_unique_submission_identifiers(
    state: MutableMapping[str, Any],
    segment: Mapping[str, Any],
    client_id: Optional[str] = None,
    tool_task_id: Optional[str] = None,
) -> None:
    for _, other in _all_segments(state):
        if other is segment:
            continue
        if client_id and other.get("client_submission_id") == client_id:
            raise StateError("DUPLICATE_CLIENT_SUBMISSION_ID", "client_submission_id already belongs to another segment")
        if tool_task_id and other.get("tool_task_id") == tool_task_id:
            raise StateError("DUPLICATE_TOOL_TASK_ID", "tool_task_id already belongs to another segment")


def _verified_output(
    state_path: Path, job_id: str, raw_path: str, declared_hash: str
) -> Tuple[str, str]:
    if not isinstance(raw_path, str) or not raw_path:
        raise StateError("MISSING_OUTPUT_PATH", "successful result requires output_path")
    if not isinstance(declared_hash, str) or not SHA256_RE.fullmatch(declared_hash):
        raise StateError("INVALID_OUTPUT_HASH", "successful result requires lowercase SHA-256 output_hash")
    relative = Path(raw_path)
    if not relative.is_absolute() and any(part == ".." for part in relative.parts):
        raise StateError("UNSAFE_OUTPUT_PATH", "relative output_path may not traverse with '..'")
    candidate = relative if relative.is_absolute() else state_path.parent / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(state_path.parent.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise StateError("UNSAFE_OUTPUT_PATH", "output must be an existing file inside the batch directory: %s" % exc)
    if not resolved.is_file() or resolved.is_symlink():
        raise StateError("UNSAFE_OUTPUT_PATH", "output must be a regular non-symlink file")
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StateError("OUTPUT_READ_ERROR", "cannot hash output: %s" % exc)
    actual = digest.hexdigest()
    if actual != declared_hash:
        raise StateError("OUTPUT_HASH_MISMATCH", "declared output_hash does not match output bytes")
    return str(resolved), actual


def _validated_qc_report(raw: Mapping[str, Any], job: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise StateError("INVALID_QC_RESULT", "qc report must be an object")
    if raw.get("overall_result") != "PASS":
        raise StateError("QC_NOT_PASSED", "qc overall_result must be PASS")
    checks = raw.get("checks")
    if not isinstance(checks, list):
        raise StateError("INVALID_QC_CHECKS", "qc checks must be an array")
    observed: Dict[str, Mapping[str, Any]] = {}
    for check in checks:
        if not isinstance(check, dict):
            raise StateError("INVALID_QC_CHECK", "each qc check must be an object")
        check_id = check.get("check_id")
        if not isinstance(check_id, str) or not ID_RE.fullmatch(check_id):
            raise StateError("INVALID_QC_CHECK_ID", "qc check_id must be a safe opaque ID")
        if check_id in observed:
            raise StateError("DUPLICATE_QC_CHECK", "qc check_id occurs more than once")
        if check.get("result") not in QC_RESULTS:
            raise StateError("INVALID_QC_CHECK_RESULT", "qc result must be PASS, FAIL, or UNKNOWN")
        if check.get("result") != "PASS":
            raise StateError("QC_CHECK_NOT_PASSED", "%s is not PASS" % check_id)
        if not isinstance(check.get("method"), str) or not check.get("method", "").strip():
            raise StateError("MISSING_QC_METHOD", "%s needs a non-empty method" % check_id)
        for key in ("coverage", "confidence"):
            value = check.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise StateError("INVALID_QC_RATIO", "%s %s must be between 0 and 1" % (check_id, key))
        observed[check_id] = check
    missing = sorted(REQUIRED_QC_CHECK_IDS - set(observed))
    if missing:
        raise StateError("MISSING_REQUIRED_QC_CHECK", "missing qc checks: %s" % ", ".join(missing))
    replacement_mode = job.get("replacement_mode", job.get("remix_mode", "strict_replace"))
    if replacement_mode == "strict_replace" and float(observed["identity"]["coverage"]) < 0.95:
        raise StateError("IDENTITY_COVERAGE_TOO_LOW", "strict_replace identity coverage must be at least 0.95")
    for check_id in ("media_integrity", "duration", "audio_timeline", "visual_integrity", "text_stability", "rights"):
        if float(observed[check_id]["coverage"]) < 1.0:
            raise StateError("QC_COVERAGE_TOO_LOW", "%s coverage must be 1.0" % check_id)
    return json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))


def _assert_success_invariants(state: MutableMapping[str, Any], state_path: Path) -> None:
    succeeded = [
        job for job in state.get("jobs", [])
        if isinstance(job, dict) and job.get("status") == "SUCCEEDED"
    ]
    if not succeeded:
        return
    rights_ok, rights_reason = _rights_ready(state)
    if not rights_ok:
        raise StateError("SUCCESS_WITHOUT_RIGHTS", rights_reason)
    for job in succeeded:
        job_id = str(job.get("job_id"))
        _materialize_full_segment(job)
        segments = [item for item in job.get("segments", []) if isinstance(item, dict)]
        if not segments or any(item.get("status") != "SUCCEEDED" for item in segments):
            raise StateError("INCOMPLETE_SUCCESS_SEGMENTS", "%s has unfinished segments" % job_id)
        if any(not confirmation_valid(state, job_id, segment) for segment in segments):
            raise StateError("SUCCESS_WITHOUT_CONFIRMATION", "%s has an unconfirmed segment" % job_id)
        output_path, output_hash = _verified_output(
            state_path.resolve(strict=True),
            job_id,
            job.get("output_path"),
            job.get("output_hash"),
        )
        required_operations = [
            operation
            for operation in _media_for_job(state, job_id)
            if operation.get("required_for_success", True)
        ]
        if not required_operations or any(
            operation.get("status") != "SUCCEEDED" for operation in required_operations
        ):
            raise StateError("INCOMPLETE_MEDIA_OPERATIONS", "%s has unfinished required media operations" % job_id)
        if not any(
            operation.get("output_hash") == output_hash
            and operation.get("output_path") == output_path
            for operation in required_operations
        ):
            raise StateError("FINAL_OUTPUT_NOT_IN_MEDIA_DAG", "%s final output is not a verified required media result" % job_id)
        job["qc_result"] = _validated_qc_report(job.get("qc_result"), job)


def _media_for_job(state: Mapping[str, Any], job_id: str) -> List[MutableMapping[str, Any]]:
    return [
        operation
        for operation in state.get("media_operations", [])
        if isinstance(operation, dict) and operation.get("job_id") == job_id
    ]


def _aggregate_job(state: MutableMapping[str, Any], job: MutableMapping[str, Any]) -> None:
    if job.get("status") == "SUCCEEDED":
        return
    segments = [segment for segment in job.get("segments", []) if isinstance(segment, dict)]
    if not segments:
        return
    statuses = {segment.get("status") for segment in segments}
    if "FAILED_FINAL" in statuses:
        job["status"] = "FAILED_FINAL"
        return
    if "PAUSED_FOR_AUTH" in statuses:
        job["status"] = "PAUSED_FOR_AUTH"
        return
    if "PAUSED_FOR_INPUT" in statuses:
        job["status"] = "PAUSED_FOR_INPUT"
        return
    if statuses == {"SUCCEEDED"}:
        job["status"] = "COMPOSING"
        operations = _media_for_job(state, str(job.get("job_id")))
        required = [operation for operation in operations if operation.get("required_for_success", True)]
        if any(operation.get("status") == "FAILED_FINAL" for operation in required):
            job["status"] = "FAILED_FINAL"
            job["composition_block_reason"] = "required_media_operation_failed"
        elif any(operation.get("status") == "NEEDS_RECONCILIATION" for operation in required):
            job["composition_block_reason"] = "required_media_operation_needs_reconciliation"
        elif required and all(
            operation.get("status") == "SUCCEEDED"
            and operation.get("output_path")
            and operation.get("output_hash")
            for operation in required
        ):
            job.pop("composition_block_reason", None)
            job["status"] = "QC_FAST"
        elif not operations:
            job["composition_block_reason"] = "media_plan_required"
        return
    if statuses & {"SUBMITTING", "SUBMITTED", "GENERATING", "NEEDS_RECONCILIATION"}:
        job["status"] = "GENERATING"
    elif "RETRY_QUEUED" in statuses:
        job["status"] = "RETRY_QUEUED"
    elif "QUEUED" in statuses:
        job["status"] = "QUEUED"
    elif statuses <= {"CONFIRMED"}:
        job["status"] = "CONFIRMED"
    elif "AWAITING_REVISION_CONFIRMATION" in statuses:
        job["status"] = "AWAITING_REVISION_CONFIRMATION"
    elif "VALIDATED" in statuses:
        job["status"] = "VALIDATED"


def _aggregate_batch(state: MutableMapping[str, Any]) -> None:
    jobs = [job for job in state.get("jobs", []) if isinstance(job, dict)]
    for job in jobs:
        _aggregate_job(state, job)
    if not jobs:
        return
    statuses = {job.get("status") for job in jobs}
    if statuses == {"SUCCEEDED"}:
        state["status"] = "COMPLETED_WITH_SUMMARY"
    elif "SUCCEEDED" in statuses and statuses & {
        "FAILED_FINAL",
        "PAUSED_FOR_AUTH",
        "PAUSED_FOR_INPUT",
        "AWAITING_REVISION_CONFIRMATION",
    }:
        state["status"] = "PARTIALLY_COMPLETED"
    elif statuses & {"SUBMITTED", "GENERATING", "COMPOSING", "QC_FAST", "QC_DEEP", "QUEUED", "RETRY_QUEUED"}:
        state["status"] = "RUNNING"
    elif statuses == {"FAILED_FINAL"} or statuses <= {"FAILED_FINAL", "PAUSED_FOR_AUTH", "PAUSED_FOR_INPUT"}:
        state["status"] = "PARTIALLY_COMPLETED"


def _template_group(job: Mapping[str, Any]) -> str:
    for key in ("template_group_id", "reference_template_id", "reference_id"):
        value = job.get(key)
        if isinstance(value, str) and value:
            return value
    return "default"


def _pending_segment_refs(state: MutableMapping[str, Any]) -> List[Tuple[MutableMapping[str, Any], MutableMapping[str, Any]]]:
    pending: List[Tuple[MutableMapping[str, Any], MutableMapping[str, Any]]] = []
    for job, segment in _all_segments(state):
        if job.get("status") in TERMINAL_JOB_STATUSES:
            continue
        if segment.get("status") not in SUBMITTABLE_SEGMENT_STATUSES:
            continue
        if not confirmation_valid(state, str(job.get("job_id")), segment):
            if segment.get("status") != "AWAITING_REVISION_CONFIRMATION":
                segment["status"] = "AWAITING_REVISION_CONFIRMATION"
            continue
        pending.append((job, segment))
    return pending


def _calibration_filter(
    state: MutableMapping[str, Any],
    pending: List[Tuple[MutableMapping[str, Any], MutableMapping[str, Any]]],
) -> List[Tuple[MutableMapping[str, Any], MutableMapping[str, Any]]]:
    by_group: Dict[str, List[MutableMapping[str, Any]]] = {}
    for job in state.get("jobs", []):
        if isinstance(job, dict):
            by_group.setdefault(_template_group(job), []).append(job)
    blocked_groups: set = set()
    calibration_pending_groups: set = set()
    for group, jobs in by_group.items():
        calibrations = [job for job in jobs if job.get("is_calibration") is True]
        if not calibrations:
            continue
        if any(job.get("status") == "FAILED_FINAL" for job in calibrations):
            blocked_groups.add(group)
            for job in jobs:
                if not job.get("is_calibration") and job.get("status") not in TERMINAL_JOB_STATUSES:
                    job["status"] = "AWAITING_REVISION_CONFIRMATION"
                    for segment in job.get("segments", []):
                        if isinstance(segment, dict) and segment.get("status") in SUBMITTABLE_SEGMENT_STATUSES:
                            segment["status"] = "AWAITING_REVISION_CONFIRMATION"
        elif not all(job.get("status") == "SUCCEEDED" for job in calibrations):
            calibration_pending_groups.add(group)
    result = []
    for job, segment in pending:
        group = _template_group(job)
        if group in blocked_groups:
            continue
        if group in calibration_pending_groups and job.get("is_calibration") is not True:
            continue
        result.append((job, segment))
    return result


def _last_wave_records(
    state: MutableMapping[str, Any], wave: Mapping[str, Any]
) -> List[MutableMapping[str, Any]]:
    records: List[MutableMapping[str, Any]] = []
    refs = wave.get("segments", [])
    if not isinstance(refs, list):
        return records
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        try:
            _, segment = _segment_lookup(state, str(ref.get("job_id")), str(ref.get("segment_id")))
        except StateError:
            continue
        records.append(segment)
    return records


def _apply_scheduler_feedback(
    state: MutableMapping[str, Any], adapter: str, explicit_outcome: Optional[str] = None
) -> Optional[str]:
    scheduler = state.setdefault("scheduler_state", {})
    if not isinstance(scheduler, dict):
        raise StateError("INVALID_SCHEDULER_STATE", "scheduler_state must be an object")
    wave = scheduler.get("last_wave")
    if not isinstance(wave, dict) or wave.get("feedback_applied") is True:
        if explicit_outcome:
            outcome = explicit_outcome
        else:
            return None
    else:
        records = _last_wave_records(state, wave)
        if not records or any(record.get("status") not in TERMINAL_SEGMENT_STATUSES | {"NEEDS_RECONCILIATION"} for record in records):
            return "waiting_for_last_wave"
        if any(record.get("status") == "NEEDS_RECONCILIATION" for record in records):
            outcome = "uncertain"
        elif all(record.get("status") == "SUCCEEDED" for record in records):
            task_ids = [record.get("tool_task_id") for record in records]
            outcome = "healthy" if all(task_ids) and len(task_ids) == len(set(task_ids)) else "uncertain"
        else:
            categories = {
                (record.get("error") or {}).get("category")
                for record in records
                if isinstance(record.get("error"), dict)
            }
            if categories & {"rate_limit", "queue_pressure"}:
                outcome = "limit"
            elif categories & {"auth", "parameter", "platform"}:
                outcome = "failure"
            else:
                outcome = "failure"
        wave["feedback_applied"] = True
        wave["outcome"] = outcome

    current = scheduler.get("current_concurrency", 1)
    if not isinstance(current, int) or current < 1:
        current = 1
    if outcome == "healthy":
        scheduler["consecutive_backoffs"] = 0
        if adapter == "api":
            capacity = state.get("capacity_snapshot", {}).get("reported_capacity", current)
            if isinstance(capacity, int) and capacity > 0:
                scheduler["current_concurrency"] = min(capacity, max(1, current * 2))
        elif isinstance(wave, dict) and wave.get("wave_kind") == "ui_double_probe":
            scheduler["current_concurrency"] = 2
            scheduler["ui_double_probe_passed"] = True
    elif outcome in {"limit", "queue_pressure", "uncertain", "timeout"}:
        scheduler["current_concurrency"] = max(1, current // 2)
        count = int(scheduler.get("consecutive_backoffs", 0)) + 1
        scheduler["consecutive_backoffs"] = count
        scheduler["retry_after_seconds"] = int(scheduler.get("retry_after_seconds", 60) or 60)
        if count >= 3:
            scheduler["generation_paused"] = True
            scheduler["pause_reason"] = "three_consecutive_backoffs"
    elif outcome in {"failure", "platform_failure", "parameter_failure", "auth_failure"}:
        if adapter == "ui" and isinstance(wave, dict) and wave.get("wave_kind") == "ui_double_probe":
            scheduler["current_concurrency"] = 1
            scheduler["ui_double_probe_passed"] = False
    scheduler["last_feedback"] = outcome
    scheduler["updated_at"] = utc_now()
    return outcome


def command_plan_wave(args: argparse.Namespace) -> Dict[str, Any]:
    state_path = Path(args.state)
    with locked_state(state_path) as state:
        rights_ready, rights_reason = _rights_ready(state)
        if not rights_ready:
            state["status"] = "PAUSED_FOR_RIGHTS"
            return {
                "ok": True,
                "status": "paused",
                "command": "plan-wave",
                "reason": rights_reason,
                "planned_count": 0,
                "segments": [],
            }
        adapter = args.adapter or state.get("capacity_snapshot", {}).get("adapter_type") or "ui"
        if adapter not in {"ui", "api"}:
            raise StateError("INVALID_ADAPTER", "adapter must be ui or api")
        capacity = state.setdefault("capacity_snapshot", {})
        if not isinstance(capacity, dict):
            raise StateError("INVALID_CAPACITY_SNAPSHOT", "capacity_snapshot must be an object")
        capacity["adapter_type"] = adapter
        if args.reported_capacity is not None:
            if args.reported_capacity < 1:
                raise StateError("INVALID_CAPACITY", "reported_capacity must be positive")
            capacity["reported_capacity"] = args.reported_capacity
        if adapter == "api" and not isinstance(capacity.get("reported_capacity"), int):
            raise StateError("MISSING_REPORTED_CAPACITY", "API scheduling requires reported_capacity")
        scheduler = state.setdefault("scheduler_state", {})
        if not isinstance(scheduler, dict):
            raise StateError("INVALID_SCHEDULER_STATE", "scheduler_state must be an object")
        if "current_concurrency" not in scheduler:
            scheduler["current_concurrency"] = (
                min(4, capacity["reported_capacity"]) if adapter == "api" else 1
            )
        if args.retry_after_seconds is not None:
            if args.retry_after_seconds < 0:
                raise StateError("INVALID_RETRY_AFTER", "retry_after_seconds may not be negative")
            scheduler["retry_after_seconds"] = args.retry_after_seconds
        feedback = _apply_scheduler_feedback(state, adapter, args.previous_wave_outcome)
        if feedback == "waiting_for_last_wave":
            _aggregate_batch(state)
            return {
                "ok": True,
                "status": "waiting",
                "command": "plan-wave",
                "reason": "last_wave_not_terminal",
                "planned_count": 0,
                "segments": [],
            }
        if scheduler.get("generation_paused") is True:
            _aggregate_batch(state)
            return {
                "ok": True,
                "status": "paused",
                "command": "plan-wave",
                "reason": scheduler.get("pause_reason", "scheduler_paused"),
                "planned_count": 0,
                "segments": [],
                "retry_after_seconds": scheduler.get("retry_after_seconds", 60),
            }
        pending = _calibration_filter(state, _pending_segment_refs(state))
        if not pending:
            _aggregate_batch(state)
            return {
                "ok": True,
                "status": "idle",
                "command": "plan-wave",
                "reason": "no_confirmed_submittable_segments",
                "planned_count": 0,
                "segments": [],
            }
        current = int(scheduler.get("current_concurrency", 1))
        if adapter == "ui":
            current = min(current, 2)
        else:
            current = min(current, int(capacity["reported_capacity"]))
        if args.limit is not None and args.limit < 1:
            raise StateError("INVALID_WAVE_LIMIT", "wave limit must be positive")
        requested = current if args.limit is None else min(args.limit, current)
        wave_kind = "normal"
        if adapter == "ui" and current == 1 and not scheduler.get("ui_double_probe_passed"):
            completed_successes = sum(
                1 for _, segment in _all_segments(state) if segment.get("status") == "SUCCEEDED"
            )
            probe_attempted = bool(scheduler.get("ui_double_probe_attempted"))
            if completed_successes >= 1 and len(pending) >= 2 and not probe_attempted:
                requested = 2 if args.limit is None else min(2, args.limit)
                wave_kind = "ui_double_probe"
                if requested == 2:
                    scheduler["ui_double_probe_attempted"] = True
        limit = min(requested, 2 if adapter == "ui" else int(capacity["reported_capacity"]))
        selected = pending[:limit]
        timestamp = args.now or utc_now()
        wave_id = args.wave_id or "wave-%04d" % (int(scheduler.get("wave_sequence", 0)) + 1)
        _safe_id(wave_id, "wave_id")
        refs: List[Dict[str, str]] = []
        for job, segment in selected:
            if segment.get("status") in {"CONFIRMED", "RETRY_QUEUED"}:
                _transition(segment, "QUEUED", SEGMENT_TRANSITIONS, "segment")
            if job.get("status") not in TERMINAL_JOB_STATUSES:
                job["status"] = "QUEUED"
            refs.append({"job_id": str(job["job_id"]), "segment_id": str(segment["segment_id"])})
        scheduler["wave_sequence"] = int(scheduler.get("wave_sequence", 0)) + 1
        scheduler["last_wave"] = {
            "wave_id": wave_id,
            "wave_kind": wave_kind,
            "adapter_type": adapter,
            "planned_at": timestamp,
            "segments": refs,
            "feedback_applied": False,
        }
        scheduler["updated_at"] = timestamp
        capacity["observed_at"] = timestamp
        state["status"] = "RUNNING"
        _aggregate_batch(state)
        return {
            "ok": True,
            "status": "planned",
            "command": "plan-wave",
            "wave_id": wave_id,
            "wave_kind": wave_kind,
            "adapter_type": adapter,
            "effective_concurrency": limit,
            "planned_count": len(refs),
            "segments": refs,
        }


def command_record_submit(args: argparse.Namespace) -> Dict[str, Any]:
    state_path = Path(args.state)
    with locked_state(state_path) as state:
        rights_ready, rights_reason = _rights_ready(state)
        if not rights_ready:
            raise StateError("RIGHTS_NOT_CONFIRMED", rights_reason)
        job, segment = _segment_lookup(state, args.job_id, args.segment_id)
        target = args.status
        client_id = _safe_client_id(args.client_submission_id)
        current_client = segment.get("client_submission_id")
        if target == "SUBMITTING":
            if not confirmation_valid(state, args.job_id, segment):
                raise StateError("UNCONFIRMED_CALL", "segment hashes are not covered by its referenced confirmed bundle")
            if current_client not in (None, client_id):
                raise StateError("IDEMPOTENCY_CONFLICT", "segment already has a different client_submission_id")
            _assert_unique_submission_identifiers(state, segment, client_id=client_id)
            segment["client_submission_id"] = client_id
        else:
            if not current_client:
                raise StateError("SUBMITTING_NOT_PERSISTED", "record SUBMITTING with client_submission_id before an external call")
            if current_client != client_id:
                raise StateError("IDEMPOTENCY_CONFLICT", "client_submission_id does not match persisted value")
        if target == "SUBMITTED" and not args.tool_task_id:
            raise StateError("MISSING_TOOL_TASK_ID", "SUBMITTED requires --tool-task-id")
        _transition(segment, target, SEGMENT_TRANSITIONS, "segment")
        if args.tool_task_id:
            existing = segment.get("tool_task_id")
            if existing not in (None, args.tool_task_id):
                raise StateError("TASK_ID_CONFLICT", "segment already has a different tool_task_id")
            _assert_unique_submission_identifiers(state, segment, tool_task_id=args.tool_task_id)
            segment["tool_task_id"] = args.tool_task_id
        if args.increment_retry:
            count = int(segment.get("technical_retry_count", 0)) + 1
            if count > 1 and not args.user_authorized_revision:
                raise StateError("RETRY_LIMIT_REACHED", "automatic technical retry is limited to one")
            segment["technical_retry_count"] = count
        if target == "SUBMITTED":
            segment["submitted_at"] = args.now or utc_now()
        if target == "NEEDS_RECONCILIATION":
            segment["error"] = {
                "category": args.error_category or "uncertain_submission",
                "code": args.error_code or "submission_outcome_unknown",
            }
        if target == "FAILED_FINAL":
            segment["error"] = {
                "category": args.error_category or "submission",
                "code": args.error_code or "submission_failed_final",
            }
        _aggregate_batch(state)
        return {
            "ok": True,
            "status": "recorded",
            "command": "record-submit",
            "job_id": args.job_id,
            "segment_id": args.segment_id,
            "segment_status": segment["status"],
            "client_submission_id": client_id,
        }


def command_record_query(args: argparse.Namespace) -> Dict[str, Any]:
    state_path = Path(args.state)
    with locked_state(state_path) as state:
        job, segment = _segment_lookup(state, args.job_id, args.segment_id)
        target = args.status
        if target in {"SUBMITTED", "GENERATING", "SUCCEEDED"} and not segment.get("tool_task_id"):
            if not args.tool_task_id:
                raise StateError("MISSING_TOOL_TASK_ID", "%s requires a persisted tool task ID" % target)
            _assert_unique_submission_identifiers(state, segment, tool_task_id=args.tool_task_id)
            segment["tool_task_id"] = args.tool_task_id
        elif args.tool_task_id:
            existing = segment.get("tool_task_id")
            if existing not in (None, args.tool_task_id):
                raise StateError("TASK_ID_CONFLICT", "query returned a conflicting tool task ID")
            _assert_unique_submission_identifiers(state, segment, tool_task_id=args.tool_task_id)
            segment["tool_task_id"] = args.tool_task_id
        if target == "SUCCEEDED":
            output_path, output_hash = _verified_output(
                state_path.resolve(strict=True), args.job_id, args.output_path, args.output_hash
            )
            segment["output_path"] = output_path
            segment["output_hash"] = output_hash
            segment["error"] = None
        elif target in {"FAILED_FINAL", "NEEDS_RECONCILIATION", "PAUSED_FOR_AUTH"}:
            segment["error"] = {
                "category": args.error_category or (
                    "auth" if target == "PAUSED_FOR_AUTH" else "generation"
                ),
                "code": args.error_code or target.lower(),
            }
        _transition(segment, target, SEGMENT_TRANSITIONS, "segment")
        segment["last_reconciled_at"] = args.now or utc_now()
        _aggregate_batch(state)
        return {
            "ok": True,
            "status": "recorded",
            "command": "record-query",
            "job_id": args.job_id,
            "segment_id": args.segment_id,
            "segment_status": segment["status"],
            "job_status": job.get("status"),
            "batch_status": state.get("status"),
        }


def _derive_confirmation_calls(state: MutableMapping[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for job, segment in _all_segments(state):
        prompt_hash = segment.get("prompt_hash")
        invocation_hash = segment.get("invocation_hash")
        if not isinstance(prompt_hash, str) or not isinstance(invocation_hash, str):
            raise StateError("MISSING_CALL_HASH", "all confirmation calls require prompt_hash and invocation_hash")
        calls.append(
            {
                "job_id": job.get("job_id"),
                "segment_id": segment.get("segment_id"),
                "variant": "primary",
                "prompt_hash": prompt_hash,
                "invocation_hash": invocation_hash,
            }
        )
        retries = segment.get(
            "preconfirmed_retry_versions", segment.get("retry_versions", [])
        )
        if retries is None:
            retries = []
        if not isinstance(retries, list):
            raise StateError("INVALID_RETRY_VERSIONS", "preconfirmed_retry_versions must be an array")
        for retry_index, retry in enumerate(retries, 1):
            if not isinstance(retry, dict):
                raise StateError("INVALID_RETRY_VERSION", "each preconfirmed retry version must be an object")
            variant = retry.get("variant_id", "retry-%d" % retry_index)
            _safe_id(variant, "retry variant_id")
            retry_prompt = retry.get("prompt_text")
            retry_spec = retry.get("invocation_spec")
            if not isinstance(retry_prompt, str) or not isinstance(retry_spec, dict):
                raise StateError("INVALID_RETRY_VERSION", "retry version needs prompt_text and invocation_spec")
            retry_prompt_hash = retry.get("prompt_hash") or sha256_text(retry_prompt)
            retry_invocation_hash = retry.get("invocation_hash") or sha256_json(retry_spec)
            if retry_prompt_hash != sha256_text(retry_prompt) or retry_invocation_hash != sha256_json(retry_spec):
                raise StateError("RETRY_HASH_MISMATCH", "preconfirmed retry hashes do not match their content")
            calls.append(
                {
                    "job_id": job.get("job_id"),
                    "segment_id": segment.get("segment_id"),
                    "variant": variant,
                    "prompt_hash": retry_prompt_hash,
                    "invocation_hash": retry_invocation_hash,
                }
            )
    return calls


def _normalize_confirmation_record(
    raw: Mapping[str, Any], state: MutableMapping[str, Any], confirmed_at: Optional[str]
) -> Dict[str, Any]:
    bundle_id = _safe_id(raw.get("bundle_id"), "bundle_id")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise StateError("INVALID_CONFIRMATION_REVISION", "revision must be an integer >= 1")
    calls = raw.get("calls", raw.get("covered_calls"))
    if calls is None:
        calls = _derive_confirmation_calls(state)
    if not isinstance(calls, list) or not calls:
        raise StateError("MISSING_CONFIRMATION_CALLS", "confirmation record needs at least one call")
    normalized_calls: List[Dict[str, Any]] = []
    seen: set = set()
    for call in calls:
        if not isinstance(call, dict):
            raise StateError("INVALID_CONFIRMATION_CALL", "each confirmation call must be an object")
        variant = call.get("variant", "primary")
        job_id = _safe_id(call.get("job_id"), "confirmation call job_id")
        segment_id = _safe_id(call.get("segment_id"), "confirmation call segment_id")
        call_key = (job_id, segment_id, variant)
        if call_key in seen:
            raise StateError("DUPLICATE_CONFIRMATION_CALL", "duplicate call in confirmation record")
        seen.add(call_key)
        _, segment = _segment_lookup(state, job_id, segment_id)
        prompt_hash = call.get("prompt_hash")
        invocation_hash = call.get("invocation_hash")
        if variant == "primary" and (
            prompt_hash != segment.get("prompt_hash")
            or invocation_hash != segment.get("invocation_hash")
        ):
            raise StateError("CONFIRMATION_HASH_MISMATCH", "primary confirmation call differs from current segment")
        if not isinstance(prompt_hash, str) or not SHA256_RE.fullmatch(prompt_hash):
            raise StateError("INVALID_HASH", "confirmation prompt_hash must be SHA-256")
        if not isinstance(invocation_hash, str) or not SHA256_RE.fullmatch(invocation_hash):
            raise StateError("INVALID_HASH", "confirmation invocation_hash must be SHA-256")
        normalized_calls.append(
            {
                "job_id": job_id,
                "segment_id": segment_id,
                "variant": variant,
                "prompt_hash": prompt_hash,
                "invocation_hash": invocation_hash,
            }
        )
    bundle_hash = raw.get("bundle_hash", raw.get("collection_sha256"))
    file_hash = raw.get("file_hash", raw.get("file_sha256"))
    if not isinstance(bundle_hash, str) or not SHA256_RE.fullmatch(bundle_hash):
        raise StateError("INVALID_BUNDLE_HASH", "bundle_hash/collection_sha256 must be SHA-256")
    if file_hash is not None and (not isinstance(file_hash, str) or not SHA256_RE.fullmatch(file_hash)):
        raise StateError("INVALID_FILE_HASH", "file_hash/file_sha256 must be SHA-256")
    bundle_path = raw.get("bundle_path", raw.get("private_bundle_path"))
    if not bundle_path or not file_hash:
        raise StateError("MISSING_BUNDLE_FILE", "confirmed ledger entry requires bundle_path and file_hash")
    created_at = raw.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise StateError("MISSING_BUNDLE_CREATED_AT", "confirmation receipt needs created_at")
    try:
        path = Path(str(bundle_path)).resolve(strict=True)
        document = path.read_bytes()
        actual = hashlib.sha256(document).hexdigest()
    except OSError as exc:
        raise StateError("BUNDLE_READ_ERROR", "cannot verify confirmation file: %s" % exc)
    if actual != file_hash:
        raise StateError("BUNDLE_FILE_HASH_MISMATCH", "confirmation file bytes do not match file_hash")
    marker = COLLECTION_MARKER_RE.search(document)
    if marker is None or marker.group(1).decode("ascii") != bundle_hash:
        raise StateError("BUNDLE_COLLECTION_HASH_MISMATCH", "confirmation document marker does not match bundle_hash")
    try:
        expected_payload, expected_collection_hash, expected_document = build_bundle_material(
            state,
            bundle_id=bundle_id,
            revision=revision,
            created_at=created_at,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise StateError(
            "BUNDLE_RECONSTRUCTION_ERROR",
            "cannot reconstruct the exact confirmation document: %s" % exc,
        )
    expected_calls = [
        {
            "job_id": call["job_id"],
            "segment_id": call["segment_id"],
            "variant": call["variant"],
            "prompt_hash": call["prompt_hash"],
            "invocation_hash": call["invocation_hash"],
        }
        for call in expected_payload["calls"]
    ]
    if normalized_calls != expected_calls:
        raise StateError(
            "BUNDLE_CALL_SET_MISMATCH",
            "confirmation receipt calls do not match the exact calls rendered in the bundle",
        )
    if bundle_hash != expected_collection_hash or document != expected_document:
        raise StateError(
            "BUNDLE_CONTENT_MISMATCH",
            "confirmation file is not the canonical document for the current call content",
        )
    declared_count = raw.get("call_count")
    if declared_count is not None and declared_count != len(normalized_calls):
        raise StateError("BUNDLE_CALL_COUNT_MISMATCH", "bundle call_count does not match current confirmed call set")
    try:
        os.chmod(path, stat.S_IRUSR)
    except OSError as exc:
        raise StateError("BUNDLE_PERMISSION_ERROR", "cannot make confirmed bundle read-only: %s" % exc)
    file_hash = actual
    bundle_path = str(path)
    timestamp = confirmed_at or raw.get("confirmed_at") or utc_now()
    return {
        "bundle_id": bundle_id,
        "revision": revision,
        "created_at": created_at,
        "status": "CONFIRMED",
        "confirmed_at": timestamp,
        "bundle_hash": bundle_hash,
        "file_hash": file_hash,
        "private_bundle_path": bundle_path,
        "calls": normalized_calls,
    }


def command_record_confirmation(args: argparse.Namespace) -> Dict[str, Any]:
    state_path = Path(args.state)
    raw = load_json_object(Path(args.bundle_json).resolve(strict=True))
    with locked_state(state_path) as state:
        record = _normalize_confirmation_record(raw, state, args.confirmed_at)
        ledger = state.setdefault("confirmation_bundles", [])
        existing = _bundle_lookup(state, record["bundle_id"], record["revision"])
        if existing is not None:
            if existing == record:
                return {
                    "ok": True,
                    "status": "already_recorded",
                    "command": "record-confirmation",
                    "bundle_id": record["bundle_id"],
                    "revision": record["revision"],
                }
            raise StateError("APPEND_ONLY_CONFLICT", "bundle_id/revision already exists with different content")
        revisions = [
            bundle.get("revision")
            for bundle in ledger
            if isinstance(bundle, dict) and bundle.get("bundle_id") == record["bundle_id"]
        ]
        expected = max([value for value in revisions if isinstance(value, int)], default=0) + 1
        if record["revision"] != expected:
            raise StateError("CONFIRMATION_REVISION_GAP", "next revision for this bundle_id must be %d" % expected)
        ledger.append(record)
        linked = 0
        for call in record["calls"]:
            if call.get("variant", "primary") != "primary":
                continue
            job, segment = _segment_lookup(state, call["job_id"], call["segment_id"])
            segment["confirmation_bundle_id"] = record["bundle_id"]
            segment["confirmation_revision"] = record["revision"]
            if segment.get("status") in {
                "DRAFT",
                "VALIDATED",
                "AWAITING_REVISION_CONFIRMATION",
            }:
                segment["status"] = "CONFIRMED"
            linked += 1
        _aggregate_batch(state)
        if all(
            confirmation_valid(state, str(job.get("job_id")), segment)
            for job, segment in _all_segments(state)
        ) and state.get("status") in {"DRAFT", "VALIDATED", "AWAITING_CONFIRMATION"}:
            state["status"] = "AWAITING_CONFIRMATION"
        return {
            "ok": True,
            "status": "recorded",
            "command": "record-confirmation",
            "bundle_id": record["bundle_id"],
            "revision": record["revision"],
            "linked_primary_call_count": linked,
        }


def _operation_lookup(state: Mapping[str, Any], operation_id: str) -> Optional[MutableMapping[str, Any]]:
    for operation in state.get("media_operations", []):
        if isinstance(operation, dict) and operation.get("operation_id") == operation_id:
            return operation
    return None


def _normalize_media_patch(raw: Mapping[str, Any]) -> Dict[str, Any]:
    record = dict(raw)
    if "type" in record and "operation_type" not in record:
        record["operation_type"] = record.pop("type")
    return record


def command_record_media_operation(args: argparse.Namespace) -> Dict[str, Any]:
    state_path = Path(args.state)
    patch = _normalize_media_patch(load_json_object(Path(args.record_json).resolve(strict=True)))
    operation_id = _safe_id(patch.get("operation_id"), "operation_id")
    with locked_state(state_path) as state:
        operations = state.setdefault("media_operations", [])
        existing = _operation_lookup(state, operation_id)
        is_new = existing is None
        if is_new:
            required = {
                "job_id",
                "operation_type",
                "depends_on_operation_ids",
                "required_for_success",
                "input_hashes",
                "parameter_hash",
                "status",
            }
            missing = sorted(key for key in required if key not in patch)
            if missing:
                raise StateError("MISSING_MEDIA_FIELDS", "new media operation is missing: %s" % ", ".join(missing))
            _job_lookup(state, str(patch.get("job_id")))
            record = dict(patch)
            record.setdefault("technical_retry_count", 0)
            record.setdefault("client_submission_id", None)
            record.setdefault("downstream_task_id", None)
            record.setdefault("submitted_at", None)
            record.setdefault("last_reconciled_at", None)
            record.setdefault("output_path", None)
            record.setdefault("output_hash", None)
            record.setdefault("error", None)
            operations.append(record)
        else:
            record = existing
            for key in IMMUTABLE_MEDIA_FIELDS:
                if key in patch and key in record and patch[key] != record[key]:
                    raise StateError("IMMUTABLE_MEDIA_CONFLICT", "%s cannot change after operation creation" % key)
            target_status = patch.get("status", record.get("status"))
            if target_status != record.get("status"):
                _transition(record, str(target_status), MEDIA_TRANSITIONS, "media")
            for key, value in patch.items():
                if key not in IMMUTABLE_MEDIA_FIELDS and key != "status":
                    if key in {"client_submission_id", "downstream_task_id"} and record.get(key) not in (None, value):
                        raise StateError("IDEMPOTENCY_CONFLICT", "%s conflicts with the persisted value" % key)
                    record[key] = value
        if is_new:
            status = record.get("status")
            if status not in MEDIA_STATUSES:
                raise StateError("INVALID_MEDIA_STATUS", "unknown media operation status")
        if record.get("status") in {"SUBMITTING", "SUBMITTED", "RUNNING", "NEEDS_RECONCILIATION"}:
            client = record.get("client_submission_id")
            if not client:
                raise StateError("MISSING_CLIENT_SUBMISSION_ID", "remote/async media operation state requires client_submission_id")
            _safe_client_id(client)
        if record.get("execution_kind") == "cloud" and record.get("status") in {"SUBMITTED", "RUNNING", "SUCCEEDED"} and not record.get("downstream_task_id"):
            raise StateError("MISSING_DOWNSTREAM_TASK_ID", "cloud media operation requires downstream_task_id")
        dependencies = record.get("depends_on_operation_ids", [])
        if not isinstance(dependencies, list):
            raise StateError("INVALID_MEDIA_DEPENDENCIES", "depends_on_operation_ids must be an array")
        for dependency_id in dependencies:
            dependency = _operation_lookup(state, dependency_id)
            if dependency is None:
                raise StateError("UNKNOWN_MEDIA_DEPENDENCY", "unknown dependency: %s" % dependency_id)
            if dependency.get("job_id") != record.get("job_id"):
                raise StateError("CROSS_JOB_MEDIA_DEPENDENCY", "dependencies may not cross jobs")
        if record.get("status") in {"QUEUED", "SUBMITTING", "SUBMITTED", "RUNNING", "SUCCEEDED"} and any(
            _operation_lookup(state, dependency_id).get("status") != "SUCCEEDED"
            for dependency_id in dependencies
        ):
            raise StateError("MEDIA_DEPENDENCY_NOT_READY", "all dependencies must succeed before this operation can run")
        if record.get("status") == "SUCCEEDED":
            output_path, output_hash = _verified_output(
                state_path.resolve(strict=True), str(record.get("job_id")), record.get("output_path"), record.get("output_hash")
            )
            record["output_path"] = output_path
            record["output_hash"] = output_hash
            record["error"] = None
        if record.get("status") == "FAILED_FINAL":
            error = record.get("error") if isinstance(record.get("error"), dict) else {}
            record["error"] = {
                "category": error.get("category", "media"),
                "code": error.get("code", "media_failed_final"),
            }
        record["last_reconciled_at"] = args.now or utc_now()
        _aggregate_batch(state)
        return {
            "ok": True,
            "status": "created" if is_new else "updated",
            "command": "record-media-operation",
            "operation_id": operation_id,
            "operation_status": record.get("status"),
            "job_id": record.get("job_id"),
            "job_status": _job_lookup(state, str(record.get("job_id"))).get("status"),
        }


def command_record_qc(args: argparse.Namespace) -> Dict[str, Any]:
    state_path = Path(args.state)
    qc_path = Path(args.qc_json).resolve(strict=True)
    if stat.S_IMODE(qc_path.stat().st_mode) & 0o077:
        raise StateError("INSECURE_QC_FILE", "qc report must not be accessible to group or other users")
    raw_qc = load_json_object(qc_path)
    with locked_state(state_path) as state:
        job = _job_lookup(state, args.job_id)
        if job.get("status") not in {"QC_FAST", "QC_DEEP"}:
            raise StateError("QC_NOT_READY", "job must be in QC_FAST or QC_DEEP before final qc is recorded")
        rights_ok, rights_reason = _rights_ready(state)
        if not rights_ok:
            raise StateError("RIGHTS_NOT_READY", rights_reason)
        segments = [item for item in job.get("segments", []) if isinstance(item, dict)]
        if not segments or any(item.get("status") != "SUCCEEDED" for item in segments):
            raise StateError("SEGMENTS_NOT_READY", "all generation segments must be SUCCEEDED")
        if any(not confirmation_valid(state, args.job_id, segment) for segment in segments):
            raise StateError("CONFIRMATION_NOT_READY", "all generation segments must retain exact confirmed hashes")
        required_operations = [
            operation
            for operation in _media_for_job(state, args.job_id)
            if operation.get("required_for_success", True)
        ]
        if not required_operations or any(
            operation.get("status") != "SUCCEEDED" for operation in required_operations
        ):
            raise StateError("MEDIA_NOT_READY", "all required media operations must be SUCCEEDED")
        output_path, output_hash = _verified_output(
            state_path.resolve(strict=True),
            args.job_id,
            args.output_path,
            args.output_hash,
        )
        if not any(
            operation.get("output_path") == output_path
            and operation.get("output_hash") == output_hash
            for operation in required_operations
        ):
            raise StateError("FINAL_OUTPUT_NOT_IN_MEDIA_DAG", "final output must match a verified required media operation")
        qc = _validated_qc_report(raw_qc, job)
        job["qc_result"] = qc
        job["qc_report_hash"] = sha256_json(qc)
        job["qc_recorded_at"] = args.now or utc_now()
        job["output_path"] = output_path
        job["output_hash"] = output_hash
        job["status"] = "SUCCEEDED"
        _aggregate_batch(state)
        return {
            "ok": True,
            "status": "recorded",
            "command": "record-qc",
            "job_id": args.job_id,
            "job_status": job["status"],
            "batch_status": state.get("status"),
            "qc_report_hash": job["qc_report_hash"],
        }


def _past_generation_timeout(state: Mapping[str, Any], segment: Mapping[str, Any]) -> bool:
    submitted = segment.get("submitted_at")
    if not isinstance(submitted, str):
        return False
    try:
        submitted_at = dt.datetime.fromisoformat(submitted.replace("Z", "+00:00"))
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return False
    snapshot = state.get("capacity_snapshot")
    timeout = snapshot.get("nominal_timeout_seconds", 30 * 60) if isinstance(snapshot, dict) else 30 * 60
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = 30 * 60
    return (dt.datetime.now(dt.timezone.utc) - submitted_at.astimezone(dt.timezone.utc)).total_seconds() >= timeout


def derive_next_actions(state: MutableMapping[str, Any]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    rights_ready, _ = _rights_ready(state)
    if not rights_ready:
        actions.append({"priority": 10, "action": "confirm_rights", "scope": "batch"})
    for job, segment in _all_segments(state):
        job_id = str(job.get("job_id"))
        segment_id = str(segment.get("segment_id"))
        status = segment.get("status")
        base = {"job_id": job_id, "segment_id": segment_id}
        if status in {"SUBMITTING", "NEEDS_RECONCILIATION"}:
            actions.append(dict(base, priority=20, action="reconcile_generation_submission"))
        elif status in {"SUBMITTED", "GENERATING"}:
            if _past_generation_timeout(state, segment):
                actions.append(dict(base, priority=20, action="reconcile_generation_timeout"))
            else:
                actions.append(dict(base, priority=30, action="query_generation_status"))
        elif status in SUBMITTABLE_SEGMENT_STATUSES:
            if confirmation_valid(state, job_id, segment):
                actions.append(dict(base, priority=50, action="submit_confirmed_segment"))
            else:
                actions.append(dict(base, priority=40, action="confirm_segment_revision"))
        elif status == "AWAITING_REVISION_CONFIRMATION":
            actions.append(dict(base, priority=40, action="confirm_segment_revision"))
        elif status == "PAUSED_FOR_AUTH":
            actions.append(dict(base, priority=25, action="resolve_generation_auth"))
        elif status == "PAUSED_FOR_INPUT":
            actions.append(dict(base, priority=25, action="provide_missing_input"))

    operations = [operation for operation in state.get("media_operations", []) if isinstance(operation, dict)]
    by_id = {str(operation.get("operation_id")): operation for operation in operations}
    for operation in operations:
        operation_id = str(operation.get("operation_id"))
        base = {"job_id": str(operation.get("job_id")), "operation_id": operation_id}
        if operation.get("segment_id") is not None:
            base["segment_id"] = str(operation.get("segment_id"))
        status = operation.get("status")
        dependencies = operation.get("depends_on_operation_ids", [])
        ready = all(by_id.get(str(dep), {}).get("status") == "SUCCEEDED" for dep in dependencies)
        if status in {"SUBMITTING", "NEEDS_RECONCILIATION"}:
            actions.append(dict(base, priority=20, action="reconcile_media_operation"))
        elif status in {"SUBMITTED", "RUNNING"}:
            actions.append(dict(base, priority=30, action="query_media_operation"))
        elif status in {"PLANNED", "QUEUED"} and ready:
            actions.append(dict(base, priority=60, action="run_media_operation"))
        elif status == "PAUSED_FOR_AUTH":
            actions.append(dict(base, priority=25, action="resolve_media_auth"))
        elif status == "PAUSED_FOR_INPUT":
            actions.append(dict(base, priority=25, action="provide_media_input"))

    for job in state.get("jobs", []):
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id"))
        if job.get("status") == "COMPOSING" and not _media_for_job(state, job_id):
            actions.append({"priority": 55, "action": "create_media_operation_plan", "job_id": job_id})
        elif job.get("status") == "QC_FAST":
            actions.append({"priority": 70, "action": "run_fast_qc", "job_id": job_id})
        elif job.get("status") == "QC_DEEP":
            actions.append({"priority": 70, "action": "run_deep_qc", "job_id": job_id})
    return sorted(
        actions,
        key=lambda item: (
            int(item.get("priority", 999)),
            str(item.get("job_id", "")),
            str(item.get("segment_id", "")),
            str(item.get("operation_id", "")),
        ),
    )


def command_next_actions(args: argparse.Namespace) -> Dict[str, Any]:
    state = read_state(Path(args.state))
    actions = derive_next_actions(state)
    return {
        "ok": True,
        "status": "ready",
        "command": "next-actions",
        "action_count": len(actions),
        "actions": actions,
    }


def _status_counts(records: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counts = Counter(str(record.get("status", "UNKNOWN")) for record in records)
    return dict(sorted(counts.items()))


def command_resume_summary(args: argparse.Namespace) -> Dict[str, Any]:
    state = read_state(Path(args.state))
    jobs = [job for job in state.get("jobs", []) if isinstance(job, dict)]
    segments = [segment for job in jobs for segment in job.get("segments", []) if isinstance(segment, dict)]
    operations = [operation for operation in state.get("media_operations", []) if isinstance(operation, dict)]
    actions = derive_next_actions(state)
    action_counts = Counter(str(action.get("action")) for action in actions)
    return {
        "ok": True,
        "status": "summarized",
        "command": "resume-summary",
        "batch_id": state.get("batch_id"),
        "batch_status": state.get("status"),
        "job_count": len(jobs),
        "job_status_counts": _status_counts(jobs),
        "segment_count": len(segments),
        "segment_status_counts": _status_counts(segments),
        "media_operation_count": len(operations),
        "media_operation_status_counts": _status_counts(operations),
        "confirmation_revision_count": len(state.get("confirmation_bundles", [])),
        "next_action_count": len(actions),
        "next_action_counts": dict(sorted(action_counts.items())),
        "scheduler": {
            "adapter_type": state.get("capacity_snapshot", {}).get("adapter_type")
            if isinstance(state.get("capacity_snapshot"), dict)
            else None,
            "current_concurrency": state.get("scheduler_state", {}).get("current_concurrency")
            if isinstance(state.get("scheduler_state"), dict)
            else None,
            "consecutive_backoffs": state.get("scheduler_state", {}).get("consecutive_backoffs")
            if isinstance(state.get("scheduler_state"), dict)
            else None,
            "generation_paused": bool(state.get("scheduler_state", {}).get("generation_paused"))
            if isinstance(state.get("scheduler_state"), dict)
            else False,
        },
    }


def _opaque_asset_ids(job: Mapping[str, Any]) -> List[str]:
    values = job.get("subject_assets", job.get("subject_asset_ids", []))
    if not isinstance(values, list):
        return []
    result: List[str] = []
    for value in values:
        if isinstance(value, str) and ID_RE.fullmatch(value):
            result.append(value)
        elif isinstance(value, dict):
            asset_id = value.get("asset_id")
            if isinstance(asset_id, str) and ID_RE.fullmatch(asset_id):
                result.append(asset_id)
    return result


def _public_ratio(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    ratio = float(value)
    if not math.isfinite(ratio) or ratio < 0.0 or ratio > 1.0:
        return None
    return ratio


def _public_qc(qc: Any) -> Dict[str, Any]:
    if not isinstance(qc, dict):
        return {}
    result: Dict[str, Any] = {}
    if qc.get("overall_result") in {"PASS", "FAIL", "UNKNOWN"}:
        result["overall_result"] = qc["overall_result"]
    checks = qc.get("checks")
    if isinstance(checks, list):
        clean_checks = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            clean: Dict[str, Any] = {}
            check_id = check.get("check_id")
            coverage = _public_ratio(check.get("coverage"))
            confidence = _public_ratio(check.get("confidence"))
            if check_id in REQUIRED_QC_CHECK_IDS:
                clean["check_id"] = check_id
            if coverage is not None:
                clean["coverage"] = coverage
            if confidence is not None:
                clean["confidence"] = confidence
            if check.get("result") in {"PASS", "FAIL", "UNKNOWN"}:
                clean["result"] = check["result"]
                clean_checks.append(clean)
        result["checks"] = clean_checks
    return result


def public_export(state: Mapping[str, Any]) -> Dict[str, Any]:
    jobs_out: List[Dict[str, Any]] = []
    for job in state.get("jobs", []):
        if not isinstance(job, dict):
            continue
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not ID_RE.fullmatch(job_id):
            continue
        reference_id = job.get("reference_id")
        clean: Dict[str, Any] = {
            "job_id": job_id,
            "status": job.get("status") if job.get("status") in JOB_STATUSES else None,
            "target_duration_seconds": 30,
            "aspect_ratio": job.get("aspect_ratio")
            if job.get("aspect_ratio") in {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9"}
            else None,
            "prompt_hash": job.get("prompt_hash")
            if isinstance(job.get("prompt_hash"), str) and SHA256_RE.fullmatch(job["prompt_hash"])
            else None,
            "invocation_hash": job.get("invocation_hash")
            if isinstance(job.get("invocation_hash"), str) and SHA256_RE.fullmatch(job["invocation_hash"])
            else None,
            "subject_asset_ids": _opaque_asset_ids(job),
            "relative_output_name": "%s.mp4" % job_id if job.get("status") == "SUCCEEDED" else None,
            "qc_summary": _public_qc(job.get("qc_result")),
        }
        if isinstance(reference_id, str) and ID_RE.fullmatch(reference_id):
            clean["reference_id"] = reference_id
        error = job.get("error")
        if isinstance(error, dict):
            clean["has_error"] = True
        jobs_out.append(clean)
    asset_ids = []
    rights = state.get("rights_confirmation")
    if isinstance(rights, dict) and isinstance(rights.get("assets"), list):
        asset_ids = [
            asset.get("asset_id")
            for asset in rights["assets"]
            if isinstance(asset, dict)
            and isinstance(asset.get("asset_id"), str)
            and ID_RE.fullmatch(asset["asset_id"])
        ]
    return {
        "schema_version": "1.0",
        "batch_id": state.get("batch_id")
        if isinstance(state.get("batch_id"), str) and ID_RE.fullmatch(state["batch_id"])
        else None,
        "status": state.get("status") if state.get("status") in {
            "DRAFT", "VALIDATED", "AWAITING_CONFIRMATION", "RUNNING",
            "COMPLETED_WITH_SUMMARY", "PAUSED_FOR_RIGHTS", "PAUSED_FOR_INPUT",
            "PAUSED_FOR_AUTH", "PARTIALLY_COMPLETED"
        } else None,
        "mode": state.get("mode") if state.get("mode") in {
            "one_reference_many_subjects", "multiple_references_mapped_subjects",
            "mapped_references", "cartesian_product"
        } else None,
        "target_duration_seconds": 30,
        "asset_ids": asset_ids,
        "jobs": jobs_out,
    }


def command_export_public(args: argparse.Namespace) -> Dict[str, Any]:
    state_path = Path(args.state).resolve(strict=True)
    output_path = Path(args.output).expanduser()
    try:
        if output_path.resolve() == state_path:
            raise StateError("UNSAFE_EXPORT_PATH", "public export may not overwrite private state")
    except OSError:
        pass
    state = read_state(state_path)
    exported = public_export(state)
    atomic_write_json(output_path, exported, mode=0o600)
    return {
        "ok": True,
        "status": "exported",
        "command": "export-public",
        "batch_id": state.get("batch_id"),
        "job_count": len(exported["jobs"]),
        "output": str(output_path.resolve()),
    }


def _add_common_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit JSON (the default; kept for automation clarity)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely mutate and inspect private remix batch state.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan-wave", help="atomically reserve the next confirmed generation wave")
    plan.add_argument("state", help="batch-state.private.json")
    plan.add_argument("--adapter", choices=("ui", "api"))
    plan.add_argument("--reported-capacity", type=int)
    plan.add_argument("--limit", type=int)
    plan.add_argument("--wave-id")
    plan.add_argument("--now")
    plan.add_argument(
        "--previous-wave-outcome",
        choices=("healthy", "limit", "queue_pressure", "uncertain", "timeout", "failure", "platform_failure", "parameter_failure", "auth_failure"),
    )
    plan.add_argument("--retry-after-seconds", type=int)
    _add_common_json(plan)
    plan.set_defaults(handler=command_plan_wave)

    submit = subparsers.add_parser("record-submit", help="record the two-phase generation submission state")
    submit.add_argument("state")
    submit.add_argument("--job-id", required=True)
    submit.add_argument("--segment-id", required=True)
    submit.add_argument("--client-submission-id", required=True)
    submit.add_argument(
        "--status",
        required=True,
        choices=("SUBMITTING", "SUBMITTED", "NEEDS_RECONCILIATION", "FAILED_FINAL", "PAUSED_FOR_AUTH"),
    )
    submit.add_argument("--tool-task-id")
    submit.add_argument("--increment-retry", action="store_true")
    submit.add_argument("--user-authorized-revision", action="store_true")
    submit.add_argument("--error-category")
    submit.add_argument("--error-code")
    submit.add_argument("--now")
    _add_common_json(submit)
    submit.set_defaults(handler=command_record_submit)

    query = subparsers.add_parser("record-query", help="record an observed generation status")
    query.add_argument("state")
    query.add_argument("--job-id", required=True)
    query.add_argument("--segment-id", required=True)
    query.add_argument(
        "--status",
        required=True,
        choices=("SUBMITTED", "GENERATING", "SUCCEEDED", "FAILED_FINAL", "NEEDS_RECONCILIATION", "PAUSED_FOR_AUTH"),
    )
    query.add_argument("--tool-task-id")
    query.add_argument("--output-path")
    query.add_argument("--output-hash")
    query.add_argument("--error-category")
    query.add_argument("--error-code")
    query.add_argument("--now")
    _add_common_json(query)
    query.set_defaults(handler=command_record_query)

    confirmation = subparsers.add_parser("record-confirmation", help="append a confirmed immutable bundle ledger entry")
    confirmation.add_argument("state")
    confirmation.add_argument("--bundle-json", required=True, help="bundle build result or full ledger-entry JSON")
    confirmation.add_argument("--confirmed-at")
    _add_common_json(confirmation)
    confirmation.set_defaults(handler=command_record_confirmation)

    media = subparsers.add_parser("record-media-operation", help="create or update one idempotent media DAG operation")
    media.add_argument("state")
    media.add_argument("--record-json", required=True, help="operation create/update JSON object")
    media.add_argument("--now")
    _add_common_json(media)
    media.set_defaults(handler=command_record_media_operation)

    qc = subparsers.add_parser("record-qc", help="validate final QA evidence and mark one job successful")
    qc.add_argument("state")
    qc.add_argument("--job-id", required=True)
    qc.add_argument("--qc-json", required=True, help="private PASS/FAIL/UNKNOWN qc report JSON")
    qc.add_argument("--output-path", required=True, help="final file inside the batch directory")
    qc.add_argument("--output-hash", required=True, help="lowercase SHA-256 of the final file")
    qc.add_argument("--now", help="timezone-aware event timestamp")
    _add_common_json(qc)
    qc.set_defaults(handler=command_record_qc)

    actions = subparsers.add_parser("next-actions", help="derive recovery-safe next actions without mutation")
    actions.add_argument("state")
    _add_common_json(actions)
    actions.set_defaults(handler=command_next_actions)

    summary = subparsers.add_parser("resume-summary", help="show non-sensitive status and recovery counts")
    summary.add_argument("state")
    _add_common_json(summary)
    summary.set_defaults(handler=command_resume_summary)

    export = subparsers.add_parser("export-public", help="write a fixed-whitelist public manifest")
    export.add_argument("state")
    export.add_argument("--output", required=True)
    _add_common_json(export)
    export.set_defaults(handler=command_export_public)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        result = arguments.handler(arguments)
    except StateError as exc:
        result = {
            "ok": False,
            "status": "error",
            "command": getattr(arguments, "command", None),
            "error": {"code": exc.code, "message": str(exc)},
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    except (FileNotFoundError, PermissionError, OSError) as exc:
        result = {
            "ok": False,
            "status": "error",
            "command": getattr(arguments, "command", None),
            "error": {"code": "IO_ERROR", "message": str(exc)},
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
