"""Outbound HTTPS connector for a single ThreadSatchel archive."""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timezone
import errno
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid

RUNTIME = Path(__file__).resolve().parents[1]

MAX_RESPONSE = 2 * 1024 * 1024
EVENT_MAX_BYTES = 1024 * 1024
EVENT_BACKUPS = 3
MAX_FAILURE_FILES = 1000
HEARTBEAT_INTERVAL = 15
HEARTBEAT_TIMEOUT = 10
HEARTBEAT_BUDGET = 240
HEARTBEAT_JOIN_TIMEOUT = 1
IDLE_POLL_INTERVAL = 0.75
CONNECT_ATTEMPT_DELAY = 0.25
CONNECT_MAX_PENDING = 4
CONNECT_DEFAULT_TIMEOUT = 30
KEEPALIVE_MAX_IDLE = 15
RESPONSE_READ_SIZE = 64 * 1024
KNOWN_ACTIONS = {"search_memory", "list_memories", "get_memory", "peek_memory", "timeline_memory", "get_project_brief", "memory_ai_status",
                 "save_memory", "save_handoff", "list_handoffs", "get_handoff", "read_handoff_file", "recent_save_errors"}

# Messages are fixed locally; never copy exception strings, importer stderr, packet
# contents, request keys, or server response bodies into diagnostics.
ERRORS = {
    "invalid_request": ("Invalid request or unavailable record.", False, "Correct the request before retrying."),
    "invalid_listing": ("Invalid listing arguments or archive membership changed.", False, "Restart list_memories without a cursor."),
    "invalid_packet": ("Importer rejected the packet's required structure.", False, "Correct required packet fields; the deposited original is retained."),
    "importer_rejected": ("Importer rejected the packet; the deposited original is retained.", False, "Inspect the retained packet and retry with corrected content and a new request key."),
    "importer_timeout": ("Importer outcome is uncertain after a timeout; the deposited original is retained.", True, "Retry the identical request key and content; importer reconciliation prevents duplicate imports."),
    "importer_protocol": ("Importer returned an unreadable receipt; the import outcome is uncertain.", True, "Retry the identical request key and content to confirm the committed result."),
    "request_conflict": ("Request key conflicts with a previous save.", False, "Use the original content or choose a new request key."),
    "file_access": ("The connector could not access a required local file.", True, "Check local file access and retry the identical request key and content."),
    "write_busy": ("Another archive delivery is in progress.", True, "Retry with the identical request key and content."),
    "database_busy": ("The local archive database is busy.", True, "Retry the identical request key and content after the active writer finishes."),
    "database_error": ("The local archive database reported an error.", False, "Inspect local database health before retrying; retain the original packet."),
    "connectivity": ("The connector could not reach the gateway.", True, "The connector will reconnect; check this operation again before resubmitting."),
    "integrity_failure": ("Gateway job integrity validation failed.", False, "Inspect connector and gateway health before retrying."),
    "result_too_large": ("Result exceeds the connector response limit.", False, "Request fewer results or smaller file chunks."),
    "archive_error": ("The archive operation failed.", False, "Inspect local connector diagnostics before retrying."),
    "possible_credential_review_required": ("Possible credential detected; original retained for local review and not imported.", False, "Review the retained packet locally and remove credentials before submitting corrected content with a new request key."),
}
IMPORT_ERROR_MESSAGES = {
    "invalid_packet": "Packet must be an object.",
    "unsupported_format": "Expected format threadsatchel/1.",
    "invalid_kind": "kind must be excerpt, summary, or note.",
    "invalid_metadata": "A supported metadata field has an invalid value.",
    "invalid_messages": "messages must be a nonempty list.",
    "invalid_message": "Every message must be an object.",
    "invalid_text": "Every message needs nonempty text.",
    "invalid_source_order": "source_order must be a known nonnegative integer, or omitted.",
    "duplicate_identity": "Duplicate message_id or source_order in packet; split revisions into separate packets.",
    "invalid_json_value": "Packet metadata must contain finite JSON values with at most 100 nesting levels.",
    "invalid_json": "Input is not valid JSON.",
    "invalid_utf8": "Input must use valid UTF-8 text.",
    "input_not_finalized": "Temporary input is not finalized; rename it before importing.",
    "input_changed": "Input changed before import; original expected bytes were not imported.",
    "unsupported_file_type": "Supported files: .txt, .md, .markdown, .json, .zip.",
    "size_limit": "Input exceeds the configured import limits.",
    "invalid_export": "The export layout or contents are unsupported or invalid.",
    "database_error": "Database operation failed; import success was not confirmed.",
    "file_access_error": "Input or database file could not be accessed.",
    "import_failed": "Import failed; original input was not deleted.",
}
for _code, _message in IMPORT_ERROR_MESSAGES.items():
    if _code not in ERRORS:
        ERRORS[_code] = (_message, _code == "file_access_error",
                        "Inspect the retained original and correct the cause before retrying. Use a new request key if the packet content changes.")

def safe_import_detail(value):
    """Treat subprocess output as untrusted; permit only fixed codes/locations."""
    if not isinstance(value, dict) or not isinstance(value.get("code"), str) or value["code"] not in IMPORT_ERROR_MESSAGES:
        return {}
    result = {"importer_code": value["code"]}
    location = value.get("location")
    if isinstance(location, str) and re.fullmatch(r"\$(?:\.messages\[[0-9]{1,9}\])?(?:\.(?:format|kind|title|account_id|conversation_id|source_url|source_date|derived_from|messages|text|speaker|message_id|source_order))?", location):
        result["location"] = location
    for key in ("line", "column"):
        if type(value.get(key)) is int and 0 < value[key] < 10**9:
            result[key] = value[key]
    return result

def utc_now():
    return datetime.now(timezone.utc).isoformat()

class OperationError(ValueError):
    def __init__(self, code, *, stage="execute", delivery=None, content_sha=None, extra=None):
        self.code = code if code in ERRORS else "archive_error"
        self.stage, self.delivery, self.content_sha, self.extra = stage, delivery, content_sha, extra or {}
        super().__init__(ERRORS[self.code][0])

def error_detail(exc, action=None, stage="execute"):
    if isinstance(exc, OperationError):
        code, stage = exc.code, exc.stage
    elif isinstance(exc, subprocess.TimeoutExpired):
        code = "importer_timeout"
    elif isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException)):
        code = "connectivity"
    elif isinstance(exc, sqlite3.OperationalError):
        # SQLite's numeric codes are safe; never inspect arbitrary exception text.
        code = "database_busy" if getattr(exc, "sqlite_errorcode", None) in (5, 6) else "database_error"
    elif isinstance(exc, sqlite3.Error):
        code = "database_error"
    elif isinstance(exc, OSError):
        code = "file_access"
    elif isinstance(exc, (ValueError, KeyError, TypeError)):
        code = "invalid_listing" if action == "list_memories" else "invalid_request"
    else:
        code = "archive_error"
    message, retryable, advice = ERRORS[code]
    result = {"code": code, "message": message, "stage": stage,
              "exception_type": type(exc).__name__ if type(exc).__module__ in ("builtins", "sqlite3", "subprocess", "urllib.error", __name__) else "Exception",
              "retryable": retryable, "retry_advice": advice,
              "outcome": "uncertain" if code in ("importer_timeout", "importer_protocol") or (stage == "receipt" and code != "request_conflict") else "failed"}
    if isinstance(exc, urllib.error.HTTPError) and type(exc.code) is int and 100 <= exc.code <= 599:
        result["http_status"] = exc.code
    if isinstance(exc, OperationError):
        if isinstance(exc.delivery, str) and re.fullmatch(r"plugin-[a-f0-9]{64}\.json", exc.delivery):
            result.update(delivery=exc.delivery, original_retained=True)
        if isinstance(exc.content_sha, str) and re.fullmatch(r"[a-f0-9]{64}", exc.content_sha):
            result["sha256"] = exc.content_sha
        extra = safe_import_detail({"code": exc.extra.get("importer_code"), **exc.extra})
        result.update(extra)
        if extra.get("importer_code"):
            result["message"] = IMPORT_ERROR_MESSAGES[extra["importer_code"]]
    return result

class Diagnostics:
    """Bounded private metadata only. Failure receipts never become success cache."""
    def __init__(self, state):
        self.state = state
        self._lock = threading.RLock()
        self.failures = state / "failures"
        self.failures.mkdir(exist_ok=True)
        self.events = state / "connector-events.jsonl"

    def event(self, event):
        with self._lock:
            return self._event(event)

    def _event(self, event):
        encoded = encode(event) + b"\n"
        if self.events.exists() and self.events.stat().st_size + len(encoded) > EVENT_MAX_BYTES:
            oldest = self.events.with_name(self.events.name + "." + str(EVENT_BACKUPS))
            if oldest.exists():
                oldest.unlink()
            for index in range(EVENT_BACKUPS - 1, 0, -1):
                prior = self.events.with_name(self.events.name + "." + str(index))
                if prior.exists():
                    os.replace(prior, self.events.with_name(self.events.name + "." + str(index + 1)))
            os.replace(self.events, self.events.with_name(self.events.name + ".1"))
        with self.events.open("ab") as out:
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())

    def record(self, operation, action, status, detail=None, *, duration_ms=None):
        with self._lock:
            return self._record(operation, action, status, detail, duration_ms=duration_ms)

    def _record(self, operation, action, status, detail=None, *, duration_ms=None):
        operation = operation if isinstance(operation, str) and re.fullmatch(r"[a-f0-9]{64}", operation) else None
        event = {"timestamp": utc_now(), "operation_id": operation,
                 "action": action if isinstance(action, str) and action in KNOWN_ACTIONS else "connector", "status": status}
        if detail is not None:
            event["error_detail"] = detail
        if type(duration_ms) is int and duration_ms >= 0:
            event["duration_ms"] = duration_ms
        self.event(event)
        if operation and status == "failed":
            atomic(self.failures / (operation + ".json"), encode(event))
            files = sorted(self.failures.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[MAX_FAILURE_FILES:]:
                old.unlink()
        elif operation and status == "succeeded":
            prior_path = self.failures / (operation + ".json")
            if prior_path.exists():
                prior = json.loads(prior_path.read_text("utf-8"))
                prior["resolved_at"] = event["timestamp"]
                atomic(prior_path, encode(prior))
        return event

def protect(data, decrypt=False):
    """Windows DPAPI: ciphertext is usable only by the installing Windows user."""
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size",wintypes.DWORD),("data",ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output)):
        raise ctypes.WinError()
    try: return ctypes.string_at(output.data,output.size)
    finally: ctypes.windll.kernel32.LocalFree(output.data)

def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

def response_tokens(value):
    """Use the runtime's offline counter; byte length is a conservative fallback."""
    try:
        from memory_budget import token_count
    except ImportError:
        return len(encode(value))
    return token_count(value)


def public_result(value, action, operation_id=None):
    """Bound fresh and replayed receipts; keep a committed save successful."""
    value = dict(value)
    value.pop("response_budget", None)
    def envelope(candidate):
        # Count the exact final public envelope, not a second arbitrary reserve.
        # Tests/callers without an operation use a conservative 64-token ID bound.
        visible = dict(candidate)
        visible.update(operation_id=operation_id or "", status="completed")
        return visible
    def visible_count(candidate):
        return response_tokens(envelope(candidate)) + (64 if operation_id is None else 0)
    def mark_if_fits(candidate):
        candidate["response_budget"] = {"v": 1, "used_tokens": 9999}
        if len(encode(envelope(candidate))) + (64 if operation_id is None else 0) > 32000:
            return False
        count = visible_count(candidate)
        candidate["response_budget"]["used_tokens"] = count
        # Count the marker itself using its actual digits. The cached response
        # can be accepted only when this final measure fits its declared bound.
        count = max(count, visible_count(candidate))
        candidate["response_budget"]["used_tokens"] = count
        return count <= 2000 and visible_count(candidate) <= count
    if mark_if_fits(value):
        return value
    if action in ("save_memory", "save_handoff") and value.get("ok") is True:
        value = {"ok": True, "result": compact_saved_result(action, value.get("result"))}
        if mark_if_fits(value):
            return value
    raise OperationError("result_too_large", stage="response")


def sha(value):
    return hashlib.sha256(value).hexdigest()

def atomic(path, data):
    temp = path.with_name(".incoming-" + uuid.uuid4().hex + ".part")
    with temp.open("xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)

def safe_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}", value) or ".." in value or value.endswith((".", " ")):
        raise ValueError("Invalid name")
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        raise ValueError("Reserved name")
    return value

def secure_path(root, relative):
    # Never allow drives, UNC paths, traversal, hidden staging or links/reparse points.
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Invalid path")
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(p in (".", "..") or p.startswith(".") for p in parts):
        raise ValueError("Invalid path")
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError("Links are not permitted")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Links are not permitted")
    resolved = current.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Path outside handoffs")
    return resolved

class Archive:
    def __init__(self, root: Path, state: Path, reader=None, importer=None):
        self.root, self.state = root.resolve(), state.resolve()
        self.handoffs = self.root / "handoffs"
        self.handoffs.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "writes").mkdir(exist_ok=True)
        self.diagnostics = Diagnostics(self.state)
        self._reader, self._importer = reader, importer
        self._credential_guard = None

    def credential_guard(self):
        if self._credential_guard is None:
            # Reuse the same decoded/raw scan as the existing inbox sweep. Importing
            # its module defines helpers only; it does not run a sweep or importer.
            import inbox_sweep
            self._credential_guard = inbox_sweep
        return self._credential_guard

    def reader(self):
        if self._reader is None:
            spec = importlib.util.spec_from_file_location("threadsatchel_readonly", RUNTIME / "server_readonly.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.DB = self.root / "memory.sqlite3"
            module.HANDOFFS = self.handoffs
            self._reader = module
        return self._reader

    def import_packet(self, packet, delivery):
        if self._importer:
            return self._importer(packet, delivery)
        if not isinstance(packet, dict):
            raise OperationError("invalid_packet", stage="validate")
        data = encode(packet)
        if len(data) > 1024 * 1024:
            raise OperationError("size_limit", stage="validate")
        inbox = self.root / "inbox"
        inbox.mkdir(exist_ok=True)
        if not re.fullmatch(r"[0-9a-f]{64}", delivery):
            raise ValueError("Invalid delivery identity")
        secure_path(self.root, "inbox/plugin-" + delivery + ".json")
        final = inbox / ("plugin-" + delivery + ".json")
        if final.exists():
            if final.read_bytes() != data:
                raise OperationError("request_conflict", stage="deposit")
        else:
            atomic(final, data)
        guard = self.credential_guard()
        if guard.SECRET.search(data.decode("utf-8")) or guard.contains_possible_credential(packet):
            raise OperationError("possible_credential_review_required", stage="credential_review", delivery=final.name, content_sha=sha(data))
        # Existing importer owns validation, originals, reconciliation and commit.
        try:
            run = subprocess.run([sys.executable, str(RUNTIME / "import_memory.py"), str(final),
                                  "--db", str(self.root / "memory.sqlite3")],
                                 cwd=RUNTIME, capture_output=True, timeout=120, shell=False)
        except subprocess.TimeoutExpired:
            raise OperationError("importer_timeout", stage="import", delivery=final.name, content_sha=sha(data)) from None
        except OSError:
            raise OperationError("file_access", stage="import", delivery=final.name, content_sha=sha(data)) from None
        try:
            receipt = json.loads(run.stdout.decode("utf-8-sig")) if len(run.stdout) <= MAX_RESPONSE else None
        except (ValueError, UnicodeError):
            receipt = None
        if run.returncode != 0:
            detail = safe_import_detail(receipt.get("error_detail")) if isinstance(receipt, dict) else {}
            raise OperationError(detail.get("importer_code", "importer_rejected"), stage="import", delivery=final.name, content_sha=sha(data), extra=detail)
        if not valid_import_receipt(receipt, final, sha(data)):
            raise OperationError("importer_protocol", stage="import", delivery=final.name, content_sha=sha(data))
        result = {"saved": True, "delivery": final.name, "sha256": sha(data), "import_result": receipt}
        result["has_warnings"] = receipt.get("has_warnings") is True
        status = receipt.get("import_status")
        result["import_status"] = status if status in ("imported", "imported_with_warnings", "already_present") else "imported"
        result["imported_with_warnings"] = result["has_warnings"]
        return result

    def write_once(self, action, args, perform):
        from codex_capture import capture_lock, CaptureLockBusy
        # Remote and stdio writers may have separate diagnostic state. Serialize
        # delivery across the archive so neither can overwrite a competing packet
        # or allocate the same next handoff version.
        try:
            with capture_lock(self.root / 'delivery-lock'):
                return self._write_once(action, args, perform)
        except CaptureLockBusy:
            raise OperationError('write_busy', stage='delivery') from None

    def _write_once(self, action, args, perform):
        key = args.get("request_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", key):
            raise ValueError("Invalid request key")
        identity = sha((action + "\n" + key).encode())
        receipt_file = self.state / "writes" / (identity + ".json")
        digest = sha(encode(args))
        if receipt_file.exists():
            prior = json.loads(receipt_file.read_text("utf-8"))
            if prior["digest"] != digest:
                raise OperationError("request_conflict", stage="receipt")
            return prior["result"]
        result = perform(identity, digest)
        try:
            atomic(receipt_file, encode({"digest": digest, "result": result}))
        except OSError:
            raise OperationError("file_access", stage="receipt") from None
        return result

    def recent_save_errors(self, limit=10):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Invalid diagnostic limit")
        errors = []
        for path in self.diagnostics.failures.glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name):
                continue
            record = json.loads(path.read_text("utf-8"))
            if record.get("action") in ("save_memory", "save_handoff"):
                errors.append(record)
        errors.sort(key=lambda record: record["timestamp"], reverse=True)
        return {"errors": errors[:limit], "retention": {"max_records": MAX_FAILURE_FILES,
                "event_log_max_bytes": EVENT_MAX_BYTES, "event_log_backups": EVENT_BACKUPS}}

    def list_handoffs(self):
        found = {}
        secure_path(self.root, "handoffs")
        for item in sorted(self.handoffs.iterdir()):
            if item.name.startswith(".") or item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                continue
            name = item.name if item.is_dir() else item.stem
            entry = found.setdefault(name, {"name": name, "versions": [], "legacy": True})
            if item.is_dir():
                entry["versions"] = sorted([p.name for p in item.iterdir() if p.is_dir() and re.fullmatch(r"v[1-9][0-9]*", p.name)], key=lambda v: int(v[1:]))
                entry["legacy"] = not bool(entry["versions"])
        return list(found.values())

    def handoff_page(self, offset=0, limit=3):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("Invalid handoff page")
        rows = self.list_handoffs()
        page = []
        for row in rows[offset:offset + limit]:
            page.append({"name": row["name"], "latest_version": row["versions"][-1] if row["versions"] else None,
                         "version_count": len(row["versions"]), "legacy": row["legacy"]})
        result = {"handoffs": page, "total_count": len(rows), "offset": offset,
                  "next_offset": offset + len(page) if offset + len(page) < len(rows) else None}
        while response_tokens(result) > 1600 and len(page) > 1:
            page.pop()
            result["next_offset"] = offset + len(page)
        return result

    def get_handoff(self, name, version=None, offset=0, limit=3):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("Invalid handoff file page")
        safe_name(name)
        names = [row["name"] for row in self.list_handoffs()]
        if name not in names:
            matches = [n for n in names if n.lower().replace("-", " ") == name.lower().replace("-", " ")]
            if len(matches) != 1:
                raise ValueError("Handoff not found; use list_handoffs")
            name = matches[0]
        folder = secure_path(self.handoffs, name)
        if version and not re.fullmatch(r"v[1-9][0-9]{0,5}", version):
            raise ValueError("Invalid version")
        files = []
        if folder.is_dir():
            versions = sorted([p.name for p in folder.iterdir() if p.is_dir() and re.fullmatch(r"v[1-9][0-9]*", p.name)], key=lambda v: int(v[1:]))
            chosen = version or (versions[-1] if versions else None)
            if chosen:
                folder = secure_path(self.handoffs, name + "/" + chosen)
            for item in folder.rglob("*"):
                relative = item.relative_to(self.handoffs).as_posix()
                if any(p.startswith(".") for p in item.relative_to(folder).parts):
                    continue
                if item.is_file():
                    secure_path(self.handoffs, relative)
                    files.append(item)
                    if len(files) > 200:
                        raise ValueError("Handoff has too many files")
        else:
            if version:
                raise ValueError("Legacy handoff has no numbered versions")
            chosen = None
        for extension in (".md", ".txt", ".zip"):
            legacy = self.handoffs / (name + extension)
            if legacy.is_file() and not version:
                secure_path(self.handoffs, legacy.name)
                files.append(legacy)
        if not files:
            raise ValueError("No files found")
        files = sorted(set(files), key=lambda p: p.relative_to(self.handoffs).as_posix())
        result = {"name": name, "version": chosen, "files": [], "file_count": len(files), "offset": offset,
                  "next_offset": None}
        for item in files[offset:offset + limit]:
            size = item.stat().st_size
            info = {"path": item.relative_to(self.handoffs).as_posix(), "name": item.name, "size": size}
            result["files"].append(info)
            if item.name.lower() in ("instructions.txt", "instructions.md", name.lower()+".md", name.lower()+".txt"):
                with item.open("r", encoding="utf-8-sig", newline="") as stream:
                    preview = stream.read(201)
                result.setdefault("instructions", []).append({"path": info["path"], "text": preview[:200],
                    "text_truncated": len(preview) > 200, "original_tool": "read_handoff_file"})
            if response_tokens(result) > 1500 and len(result["files"]) > 1:
                result["files"].pop()
                if result.get("instructions") and result["instructions"][-1]["path"] == info["path"]:
                    result["instructions"].pop()
                break
        result["next_offset"] = offset + len(result["files"]) if offset + len(result["files"]) < len(files) else None
        return result

    def read_file(self, path, offset=0, length=512):
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 1024:
            raise ValueError("Invalid chunk")
        item = secure_path(self.handoffs, path)
        if not item.is_file():
            raise ValueError("File not found")
        size = item.stat().st_size
        if size > 512 * 1024 * 1024 or offset > size:
            raise ValueError("File or offset exceeds limit")
        hasher = hashlib.sha256()
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
            stream.seek(offset)
            data = stream.read(length)
        result = {"path": path, "size": size, "offset": offset, "next_offset": offset+len(data), "eof": offset+len(data)>=size, "sha256": hasher.hexdigest(), "base64": base64.b64encode(data).decode("ascii")}
        while response_tokens(result) > 1600 and len(data) > 1:
            data = data[:max(1, len(data) * 3 // 4)]
            result.update(next_offset=offset+len(data), eof=offset+len(data)>=size,
                          base64=base64.b64encode(data).decode("ascii"))
        return result

    def save_handoff(self, args, identity, digest):
        name = safe_name(args["name"])
        instructions = args["instructions"]
        if not isinstance(instructions, str) or not instructions or len(instructions.encode())>262144:
            raise ValueError("Invalid instructions")
        files = args.get("files", [])
        if not isinstance(files, list) or len(files)>10:
            raise ValueError("Too many files")
        decoded, used, total = [], {"instructions.md", "plugin-manifest.json"}, 0
        for entry in files:
            filename = safe_name(entry["name"])
            if filename.lower() in used:
                raise ValueError("Duplicate or reserved filename")
            used.add(filename.lower())
            data = base64.b64decode(entry["base64"], validate=True)
            total += len(data)
            if total > 1024 * 1024:
                raise ValueError("Handoff files exceed 1 MiB")
            decoded.append((filename, data))
        folder = secure_path(self.handoffs, name)
        folder.mkdir(exist_ok=True)
        completed = None
        for item in folder.glob("v*/plugin-manifest.json"):
            secure_path(self.handoffs, item.relative_to(self.handoffs).as_posix())
            manifest = json.loads(item.read_text("utf-8"))
            if manifest.get("request_id") == identity:
                if manifest.get("digest") != digest:
                    raise ValueError("Conflicting existing handoff")
                completed = item.parent
                break
        if completed is None:
            numbers = [int(p.name[1:]) for p in folder.iterdir() if re.fullmatch(r"v[1-9][0-9]*", p.name)]
            version = "v"+str(max(numbers, default=0)+1)
            staging = folder / (".incoming-"+uuid.uuid4().hex)
            staging.mkdir()
            (staging / "instructions.md").write_bytes(instructions.encode())
            for filename, data in decoded:
                (staging / filename).write_bytes(data)
            (staging / "plugin-manifest.json").write_bytes(encode({"format":"threadsatchel-handoff/1","request_id":identity,"digest":digest,"name":name,"version":version,"files":[{"name":n,"sha256":sha(d),"size":len(d)} for n,d in decoded]}))
            completed = folder / version
            os.rename(staging, completed)
        packet = {"format":"threadsatchel/1","kind":"note","title":f"Handoff | {name} | {completed.name}","messages":[{"speaker":"assistant","text":instructions+"\n\nPickup: "+name+" / "+completed.name+"\nFiles: "+", ".join(n for n,d in decoded)}]}
        receipt = self.import_packet(packet, identity)
        return {"saved":True,"name":name,"version":completed.name,"index":receipt,"handoff":self.get_handoff(name,completed.name)}

    def execute(self, action, args):
        if not isinstance(args, dict):
            raise ValueError("Invalid arguments")
        if action in ("search_memory", "list_memories", "get_memory", "peek_memory", "timeline_memory", "get_project_brief", "memory_ai_status"):
            return getattr(self.reader(), action)(**args)
        if action == "save_memory":
            return self.write_once(action,args,lambda identity,digest:self.import_packet(args["packet"],identity))
        if action == "save_handoff":
            return self.write_once(action,args,lambda identity,digest:self.save_handoff(args,identity,digest))
        if action == "list_handoffs":
            return self.handoff_page(**args)
        if action == "get_handoff":
            return self.get_handoff(**args)
        if action == "read_handoff_file":
            return self.read_file(**args)
        if action == "recent_save_errors":
            return self.recent_save_errors(**args)
        raise ValueError("Unsupported action")

def valid_import_receipt(receipt, final, expected_sha):
    if not isinstance(receipt, dict) or "error" in receipt or "error_detail" in receipt or receipt.get("sha256") != expected_sha:
        return False
    if not isinstance(receipt.get("file"), str):
        return False
    try:
        if Path(receipt["file"]).resolve() != final.resolve():
            return False
    except (ValueError, OSError):
        return False
    counters = ("added_revisions", "reused_messages", "linked_entities", "repeated_packets")
    if any(type(receipt.get(key)) is not int or not 0 <= receipt[key] < 2**63 for key in counters):
        return False
    if not sum(receipt[key] for key in ("added_revisions", "reused_messages", "repeated_packets")):
        return False
    return isinstance(receipt.get("warnings"), list) and isinstance(receipt.get("uncertain"), list)

def compact_saved_result(action, value):
    """A committed write stays successful when its full receipt is too large."""
    if not isinstance(value, dict) or value.get("saved") is not True:
        raise OperationError("importer_protocol", stage="response")
    result = {"saved": True, "response_compacted": True,
              "note": "Saved successfully; full receipt remains local. Retrieve saved records or handoff files separately."}
    for key in ("delivery", "sha256", "has_warnings", "import_status", "imported_with_warnings", "name", "version"):
        if key in value and isinstance(value[key], (str, bool)) and len(str(value[key])) < 256:
            result[key] = value[key]
    if action == "save_handoff" and isinstance(value.get("index"), dict):
        result["index"] = compact_saved_result("save_memory", value["index"])
        result["pickup"] = {"tool": "get_handoff", "name": result.get("name"), "version": result.get("version")}
    receipt = value.get("import_result")
    if isinstance(receipt, dict):
        result["import_result"] = {key: receipt[key] for key in ("added_revisions", "reused_messages", "linked_entities", "repeated_packets")
                                   if type(receipt.get(key)) is int}
        result["import_result"].update(warning_count=len(receipt.get("warnings", [])), uncertain_count=len(receipt.get("uncertain", [])))
    return result

def run_job(archive, job):
    """Persist local outcomes before attempting any gateway completion request."""
    operation = job.get("id")
    payload = job.get("payload")
    if (not isinstance(operation, str) or not re.fullmatch(r"[a-f0-9]{64}", operation)
            or not isinstance(payload, str) or sha(payload.encode("utf-8")) != job.get("sha256")):
        raise OperationError("integrity_failure", stage="receive")
    saved = archive.state / (operation + ".json")
    action, stage = None, "decode"
    started = time.monotonic()
    try:
        decoded = json.loads(payload)
        action = decoded.get("action") if isinstance(decoded, dict) else None
        archive.diagnostics.record(operation, action, "started")
        if saved.exists():
            result = public_result(json.loads(saved.read_text("utf-8")), action, operation)
        else:
            stage = "execute"
            result = {"ok": True, "result": archive.execute(action, decoded["args"])}
            result = public_result(result, action, operation)
            stage = "receipt"
            atomic(saved, encode(result))
    except Exception as exc:
        detail = error_detail(exc, action, stage)
        result = {"ok": False, "error": detail["message"], "error_detail": detail,
                  "operation_id": operation, "diagnostics": {"events": "connector-events.jsonl", "latest": "failures/" + operation + ".json"}}
        archive.diagnostics.record(operation, action, "failed", detail,
                                   duration_ms=max(0, round((time.monotonic() - started) * 1000)))
    else:
        archive.diagnostics.record(operation, action, "succeeded",
                                   duration_ms=max(0, round((time.monotonic() - started) * 1000)))
    return result, action

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Redirect refused")

def racing_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                      source_address=None, *, all_errors=False):
    """Race TCP addresses locally; send HTTP only after one socket wins.

    Keep the resolver's preferred family first, then alternate families. The
    deadline covers every TCP attempt together. OS DNS resolution is synchronous
    and cannot be interrupted here; its elapsed time still consumes the budget.
    """
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        timeout = socket.getdefaulttimeout()
    budget = CONNECT_DEFAULT_TIMEOUT if timeout is None else float(timeout)
    if budget < 0:
        raise ValueError("Timeout must be nonnegative")
    deadline = time.monotonic() + budget
    host, port = address
    resolved = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    families, seen = {}, set()
    for item in resolved:
        identity = (item[0], item[1], item[2], item[4])
        if identity not in seen:
            seen.add(identity)
            families.setdefault(item[0], []).append(item)
    candidates = []
    while any(families.values()):
        for items in families.values():
            if items:
                candidates.append(items.pop(0))
    if not candidates:
        raise OSError("No TCP addresses returned")

    in_progress = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR,
                   getattr(errno, "WSAEWOULDBLOCK", 10035),
                   getattr(errno, "WSAEINPROGRESS", 10036),
                   getattr(errno, "WSAEALREADY", 10037)}
    connected = {0, errno.EISCONN, getattr(errno, "WSAEISCONN", 10056)}
    selector = selectors.DefaultSelector()
    owned, pending, failures = set(), set(), []
    next_index, next_launch = 0, time.monotonic()

    def discard(sock):
        if sock in pending:
            selector.unregister(sock)
            pending.remove(sock)
        owned.discard(sock)
        sock.close()

    def finish(sock):
        if sock in pending:
            selector.unregister(sock)
            pending.remove(sock)
        sock.settimeout(timeout)
        owned.remove(sock)
        return sock

    try:
        while next_index < len(candidates) or pending:
            now = time.monotonic()
            if now >= deadline:
                failures.append(TimeoutError("TCP connection deadline exceeded"))
                break
            if (next_index < len(candidates) and len(pending) < CONNECT_MAX_PENDING
                    and (not pending or now >= next_launch)):
                family, kind, protocol, _, target = candidates[next_index]
                next_index += 1
                sock = None
                try:
                    sock = socket.socket(family, kind, protocol)
                    owned.add(sock)
                    sock.setblocking(False)
                    if source_address is not None:
                        sock.bind(source_address)
                    result = sock.connect_ex(target)
                    if result in connected:
                        return finish(sock)
                    if result not in in_progress:
                        raise OSError(result, "TCP connection failed")
                    selector.register(sock, selectors.EVENT_WRITE)
                    pending.add(sock)
                    next_launch = time.monotonic() + CONNECT_ATTEMPT_DELAY
                except OSError as exc:
                    failures.append(exc)
                    if sock is not None:
                        discard(sock)
                    next_launch = time.monotonic()
                    continue
            if not pending:
                continue
            wake = deadline
            if next_index < len(candidates) and len(pending) < CONNECT_MAX_PENDING:
                wake = min(wake, next_launch)
            for key, _ in selector.select(max(0, wake - time.monotonic())):
                sock = key.fileobj
                result = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if result == 0:
                    return finish(sock)
                failures.append(OSError(result, "TCP connection failed"))
                discard(sock)
                next_launch = time.monotonic()
        if all_errors:
            raise ExceptionGroup("TCP connection attempts failed", failures)
        raise failures[-1] if failures else OSError("TCP connection failed")
    finally:
        selector.close()
        for sock in owned:
            sock.close()


class RacingHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Leave TLS, SNI, proxy CONNECT, and all HTTP behavior in the stdlib.
        self._create_connection = racing_connection


class RacingHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(RacingHTTPSConnection, request, context=self._context)


_transport = threading.local()


def close_thread_transport():
    """A main loop and a heartbeat thread never share an HTTP connection."""
    connection = getattr(_transport, "connection", None)
    _transport.connection = None
    _transport.endpoint = None
    if connection is not None:
        connection.close()


class RequestDeadline:
    """Bound direct requests, including slow headers and trickling bodies.

    DNS remains an OS call: its elapsed time counts, but it cannot be interrupted.
    The timer owns this request only; disarming under the lock prevents it from
    shutting down a connection subsequently reused by another request.
    """
    def __init__(self, connection, timeout):
        self.connection = connection
        self.active_socket = connection.sock
        self.deadline = time.monotonic() + timeout
        self.lock = threading.Lock()
        self.active = True
        self.expired = False
        self.timer = threading.Timer(timeout, self._expire)
        self.timer.daemon = True

    def _expire(self):
        with self.lock:
            if not self.active:
                return
            self.expired = True
            # getresponse() detaches connection.sock on Connection: close,
            # while HTTPResponse's file still owns the live socket. Retain the
            # request's socket until its entire response has been consumed.
            sock = self.connection.sock or self.active_socket
            if sock is not None:
                # Do not close HTTPResponse's buffered file here: another
                # thread can be inside its read lock. Shutdown wakes that read.
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if self.expired or remaining <= 0:
            raise TimeoutError("Gateway request deadline exceeded")
        return remaining

    def apply(self):
        remaining = self.remaining()
        self.connection.timeout = remaining
        if self.connection.sock is not None:
            self.active_socket = self.connection.sock
        if self.active_socket is not None:
            self.active_socket.settimeout(remaining)

    def dial(self, address, timeout=None, source_address=None, **kwargs):
        sock = racing_connection(address, self.remaining(), source_address, **kwargs)
        try:
            # TLS starts after TCP racing. It gets only the remaining budget.
            sock.settimeout(self.remaining())
            return sock
        except BaseException:
            sock.close()
            raise

    def stop(self):
        with self.lock:
            self.active = False
            self.active_socket = None
        self.timer.cancel()


def _persistent_post(endpoint, data, headers, timeout):
    connection = getattr(_transport, "connection", None)
    if (connection is not None and
            (getattr(_transport, "endpoint", None) != endpoint or
             connection.sock is None or
             time.monotonic() - _transport.last_used >= KEEPALIVE_MAX_IDLE)):
        close_thread_transport()
        connection = None
    if connection is None:
        host = urllib.parse.urlsplit(endpoint).hostname
        connection = RacingHTTPSConnection(host, timeout=timeout)
        _transport.connection, _transport.endpoint = connection, endpoint
    deadline = RequestDeadline(connection, timeout)
    original_dial = connection._create_connection
    connection._create_connection = deadline.dial
    response = None
    reusable = False
    try:
        deadline.timer.start()
        deadline.apply()
        if connection.sock is None:
            connection.connect()
            deadline.apply()
        # Exactly one HTTP attempt. A stale socket or ambiguous response is
        # reported to the durable job loop, never replayed inside the transport.
        connection.request("POST", "/api/agent", body=data, headers=headers)
        deadline.apply()
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ValueError("Redirect refused")
        if not 200 <= response.status < 300:
            raise urllib.error.HTTPError(endpoint, response.status,
                                         "Gateway request failed", response.headers, None)
        if response.length is not None and response.length > MAX_RESPONSE:
            raise ValueError("Gateway response too large")
        raw = bytearray()
        while True:
            deadline.apply()
            chunk = response.read1(min(RESPONSE_READ_SIZE, MAX_RESPONSE + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("Gateway response too large")
        # read1 permits a premature EOF; enforce a complete Content-Length.
        if response.length not in (None, 0):
            raise http.client.IncompleteRead(bytes(raw), response.length)
        result = json.loads(raw)
        deadline.remaining()
        reusable = not response.will_close and connection.sock is not None
        return result
    except BaseException as exc:
        if deadline.expired:
            raise TimeoutError("Gateway request deadline exceeded") from None
        if isinstance(exc, OSError) and not isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError)):
            raise urllib.error.URLError(exc) from None
        raise
    finally:
        deadline.stop()
        connection._create_connection = original_dial
        try:
            if response is not None:
                response.close()
        finally:
            if reusable and not deadline.expired:
                _transport.last_used = time.monotonic()
            else:
                close_thread_transport()


def post(config, payload, *, timeout=30):
    endpoint = config["endpoint"]
    if not re.fullmatch(r"https://[a-z0-9.-]+/api/agent", endpoint):
        raise ValueError("Expected an HTTPS connector endpoint")
    data = encode(payload)
    if len(data)>MAX_RESPONSE:
        raise ValueError("Response exceeds connector limit")
    headers = {"Content-Type":"application/json", "X-ThreadSatchel-Key":config["key"], "Authorization":"Bearer "+config["access_token"]}
    # Preserve urllib's platform proxy discovery, bypass and CONNECT behavior.
    # Proxy connections retain the established one-request transport path.
    if not any(value for name, value in urllib.request.getproxies().items() if name != "no"):
        return _persistent_post(endpoint, data, headers, timeout)
    close_thread_transport()
    with urllib.request.build_opener(NoRedirect(), RacingHTTPSHandler()).open(urllib.request.Request(endpoint,data=data,headers=headers),timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE+1)
        if len(raw)>MAX_RESPONSE:
            raise ValueError("Gateway response too large")
        return json.loads(raw)

class BusyHeartbeat:
    """Keep one active lease alive without polling for or executing other jobs."""
    def __init__(self, config, archive, job):
        self.config, self.archive, self.job = config, archive, job
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="threadsatchel-heartbeat", daemon=True)

    def _record(self, status, detail=None):
        try:
            self.archive.diagnostics.record(self.job.get("id"), "connector", status, detail)
        except OSError:
            print('{"code":"diagnostic_storage_unavailable","status":"retrying"}', file=sys.stderr, flush=True)

    def _run(self):
        try:
            self._run_heartbeat()
        finally:
            close_thread_transport()

    def _run_heartbeat(self):
        deadline = time.monotonic() + HEARTBEAT_BUDGET
        failed = False
        acknowledged = False
        # Claiming the job already refreshes its lease and online status. Short
        # operations can finish without starting an unnecessary HTTP request.
        if self.stop_event.wait(min(HEARTBEAT_INTERVAL, max(0, deadline-time.monotonic()))):
            return
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._record("heartbeat_budget_exhausted")
                return
            try:
                response = post(self.config, {"action": "heartbeat", "id": self.job["id"],
                                              "lease": self.job["lease"]},
                                timeout=min(HEARTBEAT_TIMEOUT, remaining))
                if not isinstance(response, dict) or response.get("acknowledged") is not True:
                    raise OperationError("connectivity", stage="heartbeat")
                if response.get("completed") is True:
                    return
                if not acknowledged:
                    self._record("heartbeat_started")
                    acknowledged = True
                if failed:
                    self._record("heartbeat_recovered")
                failed = False
            except Exception as exc:
                if not self.stop_event.is_set():
                    self._record("heartbeat_failed", error_detail(exc, stage="heartbeat"))
                failed = True
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 401, 403, 409):
                    return
            self.stop_event.wait(min(HEARTBEAT_INTERVAL, max(0, deadline - time.monotonic())))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        self.thread.join(timeout=HEARTBEAT_JOIN_TIMEOUT)


def start_prewarm(root):
    """Reuse the opt-in, resource-aware worker in this connector's own process."""
    try:
        from memory_prewarm import start
        return start(Path(root))
    except ImportError:
        return None


def load_config(path):
    if path:
        if sys.platform != 'win32':
            raise ValueError('DPAPI configuration requires Windows. Use environment settings on this platform.')
        return json.loads(protect(path.read_bytes(), decrypt=True).decode('utf-8'))
    names = {'archive': 'THREADSATCHEL_ARCHIVE', 'endpoint': 'THREADSATCHEL_AGENT_URL',
             'key': 'THREADSATCHEL_CONNECTOR_KEY', 'access_token': 'THREADSATCHEL_CONNECTOR_TOKEN'}
    config = {key: os.environ.get(name) for key, name in names.items()}
    if not all(config.values()):
        raise ValueError('Set ' + ', '.join(names.values()) + ' to run the connector.')
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, help="Optional Windows current-user DPAPI configuration")
    parser.add_argument("--once", action="store_true")
    options = parser.parse_args()
    config = load_config(options.config)
    archive_root = Path(config["archive"]).expanduser().resolve()
    if not (archive_root / "memory.sqlite3").is_file():
        raise ValueError("Initialize the selected ThreadSatchel archive before connecting it.")
    state = options.config.parent / "state" if options.config else archive_root / "connector-state"
    from codex_capture import capture_lock
    with capture_lock(state):
        archive = Archive(archive_root, state)
        prewarm = start_prewarm(archive.root)
        try:
            run_loop(config, archive, once=options.once)
        finally:
            close_thread_transport()
            if prewarm is not None:
                prewarm.stop()


def run_loop(config, archive, *, once=False):
    state = archive.state
    failure_count = 0
    while True:
        job, action, stage = None, None, "poll"
        try:
            incoming = post(config,{"action":"poll"})
            job = incoming.get("job")
            if job:
                stage = "execute"
                atomic(state / "health.json", encode({"last_success": time.time(), "status": "busy"}))
                with BusyHeartbeat(config, archive, job):
                    result, action = run_job(archive, job)
                stage = "complete"
                post(config,{"action":"complete","id":job["id"],"lease":job["lease"],"result":result})
            failure_count = 0
            atomic(state / "health.json",encode({"last_success":time.time(),"status":"connected"}))
            # Results are transient; permanent write receipts are in writes/.
            for item in state.glob("*.json"):
                if re.fullmatch(r"[a-f0-9]{64}\.json",item.name) and item.stat().st_mtime < time.time()-172800:
                    item.unlink()
        except Exception as exc:
            failure_count += 1
            detail = error_detail(exc, action, stage)
            # A failed completion must not erase an already recorded import outcome.
            operation = job.get("id") if isinstance(job, dict) else None
            try:
                archive.diagnostics.record(operation, action, stage + "_failed", detail)
                atomic(state / "health.json",encode({"last_error":time.time(),"status":"retrying","consecutive_failures":failure_count,"error_detail":detail}))
            except OSError:
                # Even with a failed disk, do not print exception text or credentials.
                print('{"code":"diagnostic_storage_unavailable","status":"retrying"}', file=sys.stderr, flush=True)
        if once:
            return
        if failure_count:
            time.sleep(min(60, 2 ** min(failure_count, 6)))
        elif not job:
            # Empty queues remain rate-limited. After a completed job, poll
            # immediately so queued reads do not incur an extra fixed pause.
            time.sleep(IDLE_POLL_INTERVAL)

if __name__ == "__main__":
    main()
