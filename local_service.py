#!/usr/bin/env python3
"""Per-user loopback review service: HTTP + CLI (start/serve/stop/status/import).

Standard library only. Owns SQLite persistence (review_store) and the browser
UI. Binds 127.0.0.1 only. MCP-to-service writes need the per-user Bearer
credential from the private api_credential file; browsers use the unguessable
per-job token in the URL path. Diagnostics never print credentials or tokens.
"""
import fcntl
import hashlib
import hmac
import http.client
import http.server
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_schema import (  # noqa: E402
    JOB_TYPES,
    ValidationError,
    check_keys,
    parse_json_strict,
    validate_review,
    validate_review_context,
    validate_revision,
    validate_skip,
)
from review_store import (  # noqa: E402
    ConflictError,
    ImportAbort,
    ReviewStore,
    StaleRevisionError,
    StoreError,
    SCOPE,
    ensure_private_dir,
    resolve_data_dir,
    secure_existing_private,
    secure_file,
)
from task_schema import StaleTaskRevisionError  # noqa: E402

SERVICE_VERSION = "0.6.0"
REPO_ROOT = Path(__file__).resolve().parent
UI_DIR = REPO_ROOT / "rating-popup"
APP_UI_DIR = REPO_ROOT / "workspace-ui"
MAX_INTERNAL_BYTES = 65536
MAX_BROWSER_BYTES = 16384
STARTUP_TIMEOUT = 20.0

STATIC_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "styles.css": ("styles.css", "text/css; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
}
APP_STATIC_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def log(message):
    print(f"agent-performance-review: {message}", file=sys.stderr, flush=True)


def _secret_path(data_dir, name):
    path = os.path.join(str(data_dir), name)
    if os.path.islink(path):
        raise StoreError(f"refusing symlinked sensitive file: {path}")
    return path


def _refuse_symlink(path, label="sensitive path"):
    if os.path.islink(path):
        raise StoreError(f"refusing symlinked {label}: {path}")


def _atomic_write_private(path, text):
    """Write a 0600 file atomically; never follow a symlinked target."""
    _refuse_symlink(path, "sensitive file")
    parent = os.path.dirname(path) or "."
    _refuse_symlink(parent, "data directory")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def load_or_create_config(data_dir):
    """Return (port, credential); create both privately on first run.

    An existing config with a missing or invalid port is a recovery error,
    never a reason to silently pick a new port (that would strand old URLs).
    """
    data_dir = Path(data_dir)
    ensure_private_dir(data_dir)
    config_path = _secret_path(data_dir, "config.json")
    cred_path = _secret_path(data_dir, "api_credential")
    if os.path.exists(config_path):
        secure_file(config_path)
        with open(config_path, encoding="utf-8") as handle:
            try:
                config = json.load(handle)
            except json.JSONDecodeError as exc:
                raise StoreError(
                    f"config is not valid JSON: {exc}; back up and remove"
                    f" {config_path} to regenerate (old rating URLs change)"
                ) from exc
        port = config.get("port")
        if isinstance(port, bool) or not isinstance(port, int) \
                or not 1 <= port <= 65535:
            raise StoreError(
                f"config {config_path} has no valid port; back it up, then fix"
                " the port or remove the file to regenerate"
                " (old rating URLs change)")
    else:
        port = _pick_free_port()
        _atomic_write_private(config_path, json.dumps({"port": port}))
    if os.path.exists(cred_path):
        secure_file(cred_path)
        with open(cred_path, encoding="utf-8") as handle:
            credential = handle.read().strip()
        if not credential:
            raise StoreError(
                f"credential file {cred_path} is empty; remove it to regenerate")
    else:
        credential = secrets.token_urlsafe(32)
        _atomic_write_private(cred_path, credential + "\n")
    return port, credential


def load_existing_config(data_dir):
    """Read port + credential without creating anything.

    Used by read-only paths (status, stop, spawned serve child): a fresh or
    incomplete data directory reports "stopped" instead of minting a port
    or credential. Raises StoreError on malformed config.
    """
    data_dir = Path(data_dir)
    config_path = os.path.join(str(data_dir), "config.json")
    cred_path = os.path.join(str(data_dir), "api_credential")
    if os.path.islink(config_path) or os.path.islink(cred_path):
        raise StoreError("refusing symlinked service config")
    if not (os.path.isfile(config_path) and os.path.isfile(cred_path)):
        return None, None
    secure_file(config_path)
    secure_file(cred_path)
    with open(config_path, encoding="utf-8") as handle:
        try:
            config = json.load(handle)
        except json.JSONDecodeError as exc:
            raise StoreError(
                f"config is not valid JSON: {exc}; back up and remove"
                f" {config_path} to regenerate (old rating URLs change)"
            ) from exc
    port = config.get("port")
    if isinstance(port, bool) or not isinstance(port, int) \
            or not 1 <= port <= 65535:
        raise StoreError(
            f"config {config_path} has no valid port; back it up, then fix"
            " the port or remove the file to regenerate"
            " (old rating URLs change)")
    with open(cred_path, encoding="utf-8") as handle:
        credential = handle.read().strip()
    if not credential:
        raise StoreError(
            f"credential file {cred_path} is empty; remove it to regenerate")
    return port, credential


def backup_database(source_db, backup_path):
    """Copy a live SQLite store with the online backup API (WAL-safe).

    The target is created exclusively (never overwrites) with mode 0600.
    """
    _refuse_symlink(backup_path, "backup target")
    parent = os.path.dirname(backup_path) or "."
    _refuse_symlink(parent, "backup directory")
    for suffix in ("-wal", "-shm", "-journal"):
        _refuse_symlink(backup_path + suffix, "backup sidecar target")
    fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(backup_path, 0o600)
    try:
        source = sqlite3.connect(source_db, timeout=10.0)
        try:
            target = sqlite3.connect(backup_path, timeout=10.0)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except Exception:
        try:
            os.unlink(backup_path)
        except OSError:
            pass
        raise
    os.chmod(backup_path, 0o600)
    return backup_path


def validate_browser_payload(payload, card):
    """Validate a browser save/skip payload; revision is mandatory for saves.

    The stale-card guard only works with an explicit revision from the card
    the user actually saw, so absent, boolean, or malformed revisions are
    rejected instead of defaulting to the current revision.
    """
    if not isinstance(payload, dict):
        raise ValidationError("rating response must be an object")
    if "skip" in payload:
        validate_skip(payload)
        return {"skip": True}
    allowed = {"completion", "gap_reasons", "ratings", "revision"}
    extra = sorted(set(payload) - allowed)
    if extra:
        raise ValidationError(f"unexpected rating fields: {', '.join(extra)}")
    if "revision" not in payload:
        raise ValidationError(
            "revision is required: reload the rating link, then save again")
    revision = validate_revision(payload["revision"])
    full = validate_review({**card["snapshot"], **{
        k: payload[k] for k in ("completion", "gap_reasons", "ratings")
        if k in payload}})
    return {"full": full, "revision": revision}


def _pick_free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def base_url_for(port):
    return f"http://127.0.0.1:{port}"


def decode_path_segment(value):
    """Decode one browser route component (legacy review ids include colons)."""
    return unquote(value)


# Exact loopback origins this client ever talks to: nothing else may
# receive the per-user Bearer [REDACTED] or review content.
_BASE_URL_RE = re.compile(r"^http://127\.0\.0\.1:(0|[1-9][0-9]{0,4})$")


def validate_base_url(base_url):
    """Accept only the exact loopback target form http://127.0.0.1:PORT."""
    if not isinstance(base_url, str) or not _BASE_URL_RE.match(base_url):
        raise StoreError(
            f"refusing non-loopback service target: {base_url!r}")
    port = int(base_url.rsplit(":", 1)[1])
    if not 1 <= port <= 65535 or (base_url.endswith(":0")):
        raise StoreError(
            f"refusing non-loopback service target: {base_url!r}")
    return base_url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, even loopback-to-loopback.

    Returning None makes the handler decline the hop (the 3xx surfaces as
    an HTTPError to the caller), so Authorization/review content is never
    forwarded to a redirect target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _private_opener():
    # Empty ProxyHandler: environment proxies (http_proxy/https_proxy, even
    # lowercase) are ignored, so private traffic never leaves loopback.
    # Retained for the opener-shape contract tests; the private transport
    # below uses a raw HTTPConnection (no proxy, no redirect) so hostile
    # proxy/redirect configuration cannot see credentials even if the
    # opener were misused.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       _NoRedirect())


_PRIVATE_OPENER = _private_opener()


# Authenticated service identity: the client proves the listener holds the
# private credential BEFORE transmitting any reusable bearer or review
# content. Proof is HMAC-SHA256(credential, domain + fresh client nonce)
# returned over the SAME TCP connection that then carries the authenticated
# request. The socket object is captured after connect(), auto_open is
# disabled so http.client can never silently reconnect, and any
# close/will_close or socket swap after the handshake aborts before the
# secret is sent. No second connection is ever opened.
IDENTITY_MAX_PROOF_BYTES = 4096
IDENTITY_PATH = "/api/internal/identity"
IDENTITY_NONCE_HEADER = "X-APR-Nonce"
IDENTITY_DOMAIN = "apr-local-identity-v1:"
_NONCE_RE = re.compile(r"^[0-9a-f]{32,64}$")


def identity_proof(credential, nonce):
    return hmac.new(credential.encode("utf-8"),
                    (IDENTITY_DOMAIN + nonce).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _constant_time_equal(a, b):
    """Constant-time string equality that never raises on untrusted input.

    hmac.compare_digest(str, str) raises TypeError for non-ASCII text, so
    untrusted values are compared as UTF-8 bytes: any mismatch (including
    non-ASCII) simply returns False instead of crashing the caller.
    """
    try:
        if not isinstance(a, str) or not isinstance(b, str):
            return False
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def _port_for(base_url):
    validate_base_url(base_url)
    return int(base_url.rsplit(":", 1)[1])


def http_call(base_url, path, credential=None, payload=None, timeout=10,
              origin=None, host=None):
    port = _port_for(base_url)
    if not path.startswith("/"):
        raise StoreError(f"refusing non-local service path: {path!r}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    method = "POST" if data is not None else "GET"
    expected_host = f"127.0.0.1:{port}"

    def base_headers(extra=None):
        headers = {"Host": host if host is not None else expected_host,
                   "Connection": "keep-alive"}
        if origin is not None:
            headers["Origin"] = origin
        if extra:
            headers.update(extra)
        return headers

    if not credential:
        # No secret involved: one direct connection, redirects returned
        # as-is and never followed.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            headers = base_headers(
                {"Content-Type": "application/json"} if data is not None else None)
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionError(
                f"cannot reach local service at {base_url}: {exc}") from exc
        finally:
            conn.close()
    # Authenticated path: handshake + request on ONE explicitly connected
    # socket. Raw HTTPConnection never consults proxy env vars and never
    # follows redirects, preserving the existing no-proxy/no-redirect policy.
    nonce = secrets.token_hex(16)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.connect()
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        raise ConnectionError(
            f"cannot reach local service at {base_url}: {exc}") from exc
    try:
        # Python 3.14 HTTPConnection defaults auto_open=1, which would let
        # send() silently reconnect a closed socket; disable it so the
        # same-socket guards below are the only permitted path.
        conn.auto_open = 0
        sock = conn.sock
        if sock is None:
            raise ConnectionError(
                f"service identity check failed at {base_url}: not connected")
        conn.request("GET", IDENTITY_PATH, body=None,
                     headers=base_headers({IDENTITY_NONCE_HEADER: nonce}))
        proof_resp = conn.getresponse()
        # Bound the unauthenticated proof body before parsing: a fake
        # listener must not force unbounded allocation while authenticating.
        raw = proof_resp.read(IDENTITY_MAX_PROOF_BYTES + 1)
        if len(raw) > IDENTITY_MAX_PROOF_BYTES:
            raise ConnectionError(
                f"service identity check failed at {base_url}: proof too large")
        # Refuse any reconnect: the peer must keep THIS socket open.
        if proof_resp.will_close:
            raise ConnectionError(
                f"service identity check failed at {base_url}: peer closed"
                " the connection after the handshake; refusing to reconnect")
        if conn.sock is None or conn.sock is not sock:
            raise ConnectionError(
                f"service identity check failed at {base_url}: connection"
                " changed after the handshake; refusing to reconnect")
        try:
            if sock.fileno() == -1:
                raise ConnectionError(
                    f"service identity check failed at {base_url}: peer closed"
                    " the connection after the handshake; refusing to reconnect")
        except OSError as exc:
            raise ConnectionError(
                f"service identity check failed at {base_url}: {exc}") from exc
        if proof_resp.status != 200:
            raise ConnectionError(
                f"service identity check failed at {base_url}:"
                f" HTTP {proof_resp.status}")
        try:
            proof_doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConnectionError(
                f"service identity check failed at {base_url}: unreadable"
                f" proof: {exc}") from exc
        if not isinstance(proof_doc, dict) or proof_doc.get("nonce") != nonce:
            raise ConnectionError(
                f"service identity check failed at {base_url}: nonce mismatch")
        presented = proof_doc.get("proof")
        if not _constant_time_equal(
                presented, identity_proof(credential, nonce)):
            raise ConnectionError(
                f"service identity check failed at {base_url}: bad proof")
        # Same-socket recheck immediately before the secret leaves.
        if conn.sock is not sock:
            raise ConnectionError(
                f"service identity check failed at {base_url}: connection"
                " changed after the handshake; refusing to reconnect")
        auth_headers = base_headers({
            "Authorization": "Bearer " + credential,
            "Content-Type": "application/json"} if data is not None else {
            "Authorization": "Bearer " + credential})
        # The request carrying the secret must not trigger a reconnect: the
        # socket above is open, so http.client reuses it; any transport
        # error aborts with no retry on a new socket.
        try:
            conn.request(method, path, body=data, headers=auth_headers)
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionError(
                f"cannot reach local service at {base_url}: {exc}") from exc
        if conn.sock is not sock:
            raise ConnectionError(
                f"service identity check failed at {base_url}: connection"
                " changed during the authenticated request")
        try:
            response = conn.getresponse()
            return response.status, response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionError(
                f"cannot reach local service at {base_url}: {exc}") from exc
    except ConnectionError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise ConnectionError(
            f"cannot reach local service at {base_url}: {exc}") from exc
    finally:
        conn.close()


def ping_service(base_url, credential, timeout=5):
    try:
        status, body = http_call(base_url, "/api/internal/ping",
                                 credential=credential, payload={"arguments": {}},
                                 timeout=timeout)
    except ConnectionError:
        return False
    if status != 200:
        return False
    try:
        return json.loads(body.decode()).get("ok") is True
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- handler ---
class ServiceHandler(http.server.BaseHTTPRequestHandler):
    server_version = "APRReview/0.6.0"
    # HTTP/1.1 keeps the handshake socket open so the authenticated
    # request can reuse the proven connection. All responses carry
    # Content-Length, so keep-alive is well-defined.
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    # -- guards ------------------------------------------------------
    def expected_host(self):
        return f"127.0.0.1:{self.server.service_port}"

    def loopback_ok(self):
        if self.client_address[0] != "127.0.0.1":
            return False
        if self.headers.get("Host") != self.expected_host():
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://{self.expected_host()}"

    def bearer_ok(self):
        presented = self.headers.get("Authorization") or ""
        expected = "Bearer " + self.server.credential
        return _constant_time_equal(presented, expected)

    def send_bytes(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; connect-src 'self'; style-src 'self';"
                         " script-src 'self'; object-src 'none'; base-uri 'none';"
                         " frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj):
        self.send_bytes(status, "application/json; charset=utf-8",
                        json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def read_json_body(self, limit):
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type != "application/json":
            raise ValidationError("mutations require Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("Content-Length must be a number") from exc
        if length < 1 or length > limit:
            raise ValidationError("request body has an invalid size")
        return parse_json_strict(self.rfile.read(length))

    # -- routing -----------------------------------------------------
    def do_GET(self):
        if not self.loopback_ok():
            self.send_error(403)
            return
        path = self.path.split("?", 1)[0]
        if path == IDENTITY_PATH:
            self._handle_identity()
            return
        if path == "/health":
            self.send_json(200, {"status": "ok", "version": SERVICE_VERSION})
            return
        if path == "/status":
            self.send_json(200, {"status": "running", "version": SERVICE_VERSION})
            return
        if path == "/app/api/tasks":
            self.send_json(200, {"tasks": self.server.store.tasks.list_tasks()})
            return
        if path.startswith("/app/api/tasks/"):
            try:
                task_id = decode_path_segment(path[len("/app/api/tasks/"):])
                task = self.server.store.tasks.get_task(task_id)
                if task is None:
                    self.send_json(404, {"error": "task not found"})
                else:
                    self.send_json(200, task)
            except ValidationError as exc:
                self.send_json(400, {"error": str(exc)})
            return
        if path == "/app/api/setup":
            self.send_json(200, self.server.store.tasks.setup_get())
            return
        if path.startswith("/app/api/shares/"):
            try:
                preview_id = decode_path_segment(path[len("/app/api/shares/"):])
                preview = self.server.store.tasks.get_share_preview(preview_id)
                if preview is None:
                    self.send_json(404, {"error": "share preview not found"})
                else:
                    self.send_json(200, preview)
            except ValidationError as exc:
                self.send_json(400, {"error": str(exc)})
            return
        if path.startswith("/app/"):
            asset = path[len("/app/"):]
            if asset in APP_STATIC_FILES:
                file_name, content_type = APP_STATIC_FILES[asset]
                try:
                    body = (APP_UI_DIR / file_name).read_bytes()
                except OSError:
                    self.send_error(503, "Workspace page files are unavailable")
                    return
                self.send_bytes(200, content_type, body)
                return
        token, suffix = self._review_route(path)
        if token is None:
            self.send_error(404)
            return
        card = self.server.store.get_card(token)
        if card is None:
            self.send_error(404, "This rating link is invalid")
            return
        if suffix == "api/review":
            public = self._public_card(card)
            public["runtime_evidence"] = self.server.store.runtime_review_projection(
                card["review_id"])
            public["runtime_evidence_at_rating"] = self.server.store.runtime_rating_projection(
                card["review_id"])
            self.send_json(200, public)
            return
        if suffix in STATIC_FILES:
            file_name, content_type = STATIC_FILES[suffix]
            try:
                body = (UI_DIR / file_name).read_bytes()
            except OSError:
                self.send_error(503, "Rating page files are unavailable")
                return
            self.send_bytes(200, content_type, body)
            return
        self.send_error(404)

    def do_POST(self):
        if not self.loopback_ok():
            self.send_error(403)
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/internal/"):
            self._handle_internal(path[len("/api/internal/"):])
            return
        if path.startswith("/app/api/"):
            self._handle_app_post(path[len("/app/api/"):])
            return
        token, suffix = self._review_route(path)
        card = self.server.store.get_card(token) if token else None
        if suffix != "api/submit" or card is None:
            self.send_error(404, "This rating link is invalid")
            return
        try:
            payload = self.read_json_body(MAX_BROWSER_BYTES)
            parsed = validate_browser_payload(payload, card)
            if "skip" in parsed:
                result = {"status": "skipped", "review_id": card["review_id"]}
            else:
                receipt = self.server.store.save_rating(
                    parsed["full"], expected_revision=parsed["revision"])
                result = {"status": receipt["status"],
                          "review_id": receipt["review_id"],
                          "recorded_at": receipt["recorded_at"]}
        except (StaleRevisionError, StaleTaskRevisionError) as exc:
            self.send_json(409, {"error": str(exc)})
            return
        except ConflictError as exc:
            self.send_json(409, {"error": str(exc)})
            return
        except ValidationError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        except StoreError as exc:
            log(f"store error: {exc}")
            self.send_json(500, {"error": "local store unavailable"})
            return
        self.send_json(200, result)

    def _handle_app_post(self, route):
        try:
            payload = self.read_json_body(MAX_BROWSER_BYTES)
            if not isinstance(payload, dict):
                raise ValidationError("request payload must be an object")
            if route == "setup":
                self.send_json(200, self.server.store.tasks.setup_save(payload))
                return
            parts = route.split("/")
            if len(parts) >= 2 and parts[0] == "tasks":
                parts[1] = decode_path_segment(parts[1])
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "review":
                self.send_json(200, self.server.store.tasks.save_review(parts[1], payload))
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "share-preview":
                self.send_json(200, self.server.store.tasks.prepare_share(
                    parts[1], payload.get("selected_fields"), payload.get("redactions")))
                return
            self.send_error(404)
        except StaleRevisionError as exc:
            self.send_json(409, {"error": str(exc)})
        except ConflictError as exc:
            self.send_json(409, {"error": str(exc)})
        except ValidationError as exc:
            self.send_json(400, {"error": str(exc)})
        except StoreError as exc:
            log(f"store error: {exc}")
            self.send_json(500, {"error": "local store unavailable"})

    @staticmethod
    def _review_route(path):
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "review":
            return None, None
        return parts[1], "/".join(parts[2:])

    @staticmethod
    def _public_card(card):
        # Saved reviews show the frozen saved snapshot, never later unrated
        # work that arrived under the same review_id.
        snapshot = card["snapshot"]
        if card["status"] == "saved" and card["saved"] \
                and card["saved"].get("snapshot"):
            snapshot = card["saved"]["snapshot"]
        public = {"review_id": card["review_id"], "snapshot": snapshot,
                  "revision": card["revision"], "status": card["status"],
                  "saved": None}
        if card["saved"]:
            public["saved"] = {
                "snapshot": card["saved"]["snapshot"],
                "ratings": card["saved"]["ratings"],
                "completion": card["saved"]["completion"],
                "recorded_at": card["saved"]["recorded_at"]}
        return public

    def _handle_identity(self):
        """Content-free identity proof: no Bearer, no body, no redirect.

        The client sends a fresh hex nonce; the service proves it holds the
        private credential by returning HMAC-SHA256(credential, domain +
        nonce), verified constant-time client-side. The proof is only
        meaningful on the same keep-alive connection as the following
        authenticated request (enforced client-side by the same-socket
        check). A naive signature-echo or raw credential ping is never used.
        """
        from urllib.parse import parse_qs, urlsplit
        nonce = (self.headers.get(IDENTITY_NONCE_HEADER) or "").strip()
        if not nonce:
            query = parse_qs(urlsplit(self.path).query)
            nonce = (query.get("nonce", [""])[0] or "").strip()
        if not _NONCE_RE.match(nonce):
            self.send_json(400, {"error": "identity nonce required"})
            return
        self.send_json(200, {"nonce": nonce,
                             "proof": identity_proof(
                                 self.server.credential, nonce)})

    def _handle_internal(self, name):
        if not self.bearer_ok():
            self.send_error(401)
            return
        try:
            payload = self.read_json_body(MAX_INTERNAL_BYTES)
            if not isinstance(payload, dict):
                raise ValidationError("internal payload must be an object")
            args = payload.get("arguments", {})
            if not isinstance(args, dict):
                raise ValidationError("arguments must be an object")
            if name == "ping":
                self.send_json(200, {"ok": True})
            elif name == "request_rating":
                self.send_json(200, self._internal_request_rating(args))
            elif name == "record_review":
                self.send_json(200, self._internal_record(args))
            elif name == "start_task":
                task = self.server.store.tasks.start_task(args)
                task["task_url"] = (base_url_for(self.server.service_port) +
                                    "/app/#task=" + task["task_id"])
                self.send_json(200, task)
            elif name == "append_evidence":
                self.send_json(200, self.server.store.tasks.append_evidence(
                    args.get("task_id"), args.get("event_id"), args.get("kind"),
                    args.get("source"), args.get("data")))
            elif name == "finish_task":
                self.send_json(200, self.server.store.tasks.finish_task(
                    args.get("task_id"), args.get("event_id"), args.get("result"),
                    args.get("interrupted", False), args.get("verification")))
            elif name == "get_task":
                task = self.server.store.tasks.get_task(args.get("task_id"))
                if task is None:
                    raise ValidationError("task not found")
                self.send_json(200, task)
            elif name == "list_tasks":
                self.send_json(200, {"tasks": self.server.store.tasks.list_tasks(args.get("status"))})
            elif name == "prepare_share":
                self.send_json(200, self.server.store.tasks.prepare_share(
                    args.get("task_id"), args.get("selected_fields"), args.get("redactions")))
            elif name == "get_setup":
                check_keys(args, "arguments", [])
                self.send_json(200, self.server.store.tasks.setup_get())
            elif name == "summary":
                check_keys(args, "arguments", ["job_type"])
                job_type = args.get("job_type")
                if job_type is not None and (
                        not isinstance(job_type, str)
                        or job_type not in JOB_TYPES):
                    raise ValidationError(
                        "job_type must be one of:"
                        f" {', '.join(JOB_TYPES)}")
                self.send_json(200, self.server.store.summary(job_type))
            elif name == "shutdown":
                self.send_json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown,
                                 name="apr-shutdown", daemon=True).start()
            else:
                self.send_error(404)
        except (ConflictError, StaleRevisionError, StaleTaskRevisionError) as exc:
            self.send_json(409, {"error": str(exc)})
        except ValidationError as exc:
            self.send_json(400, {"error": str(exc)})
        except StoreError as exc:
            log(f"store error: {exc}")
            self.send_json(500, {"error": "local store unavailable"})

    def _rating_url(self, job_token):
        return f"{base_url_for(self.server.service_port)}/review/{job_token}/"

    def _internal_request_rating(self, args):
        review = validate_review_context(args)
        token, _revision, _created = self.server.store.request_job(review)
        card = self.server.store.get_card(token)
        base = {"review_id": review["review_id"],
                "rating_url": self._rating_url(token),
                "expires_in_seconds": None, "scope": SCOPE}
        if card["status"] == "saved":
            return {**base, "status": "already_saved",
                    "recorded_at": card["saved"]["recorded_at"]}
        return {**base, "status": "ready"}

    def _internal_record(self, args):
        full = validate_review(args)
        return self.server.store.save_rating(full)


# ------------------------------------------------------------- lifecycle ---
def _lock_path(data_dir, name):
    return os.path.join(str(data_dir), name)


def ensure_service(data_dir=None, timeout=STARTUP_TIMEOUT):
    """Start the detached service on demand; return (base_url, credential).

    Never contacts anything but loopback, never changes a persisted port, and
    verifies the running service identity with the private credential.
    Config/credential creation happens inside the serialized startup lock so
    concurrent cold starters cannot pick different ports or credentials.
    """
    data_dir = Path(resolve_data_dir(data_dir))
    _refuse_symlink(str(data_dir), "data directory")
    os.makedirs(str(data_dir), exist_ok=True)
    startup_lock = _lock_path(data_dir, "startup.lock")
    _refuse_symlink(startup_lock, "startup lock")
    fd = os.open(startup_lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise StoreError(f"cannot serialize service startup: {exc}") from exc
        try:
            # Inside the lock: one winner creates config/credential.
            port, credential = load_or_create_config(data_dir)
            try:
                os.chmod(startup_lock, 0o600)
            except OSError as exc:
                raise StoreError(
                    f"cannot secure startup lock: {exc}") from exc
            base_url = base_url_for(port)
            if ping_service(base_url, credential):
                return base_url, credential
            log_path = os.path.join(str(data_dir), "service.log")
            _refuse_symlink(log_path, "service log")
            log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND,
                             0o600)
            try:
                # The child must NOT take the startup lock (we hold it while
                # waiting): config is preinitialized here, so the child loads
                # it read-only via APR_SPAWNED_SERVE.
                child_env = dict(os.environ, APR_SPAWNED_SERVE="1")
                child = subprocess.Popen(
                    [sys.executable, str(REPO_ROOT / "local_service.py"),
                     "serve", "--data-dir", str(data_dir)],
                    stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                    start_new_session=True, close_fds=True, env=child_env)
            finally:
                os.close(log_fd)
            secure_existing_private(log_path, "service log")
            deadline = time.time() + timeout
            while time.time() < deadline:
                # Authenticated readiness proves OUR service identity; a
                # missing PID file alone never ends the wait.
                if ping_service(base_url, credential):
                    return base_url, credential
                if child.poll() is not None:
                    break  # serve exited fast (e.g. occupied port)
                time.sleep(0.2)
            if ping_service(base_url, credential):
                return base_url, credential
            raise StoreError(
                f"local service did not answer at {base_url} within {timeout}s;"
                f" see {log_path} for diagnostics (no credentials are logged)")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)
    raise StoreError(f"local service did not answer at {base_url}")


def _read_pid(data_dir):
    pid_path = os.path.join(str(data_dir), "service.pid")
    if os.path.islink(pid_path):
        return None
    try:
        with open(pid_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def cmd_start(data_dir):
    data_dir = Path(resolve_data_dir(data_dir))
    try:
        base_url, _credential = ensure_service(data_dir)
    except StoreError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1
    print(json.dumps({"status": "running", "base_url": base_url,
                      "version": SERVICE_VERSION}))
    return 0


def cmd_status(data_dir):
    """Genuinely read-only: never creates port or credential."""
    data_dir = Path(resolve_data_dir(data_dir))
    try:
        port, credential = load_existing_config(data_dir)
    except StoreError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1
    if port is None:
        print(json.dumps({"status": "stopped", "base_url": None,
                          "port": None, "version": SERVICE_VERSION}))
        return 0
    base_url = base_url_for(port)
    running = ping_service(base_url, credential, timeout=3)
    print(json.dumps({"status": "running" if running else "stopped",
                      "base_url": base_url if running else None,
                      "port": port, "version": SERVICE_VERSION}))
    return 0


def cmd_stop(data_dir):
    """Stop via the authenticated local shutdown control.

    The PID file is never trusted for signaling: a stale file could name an
    unrelated reused PID. Only a shutdown acknowledged by the service holding
    our credential stops anything; otherwise the stale file is dropped.
    """
    data_dir = Path(resolve_data_dir(data_dir))
    try:
        port, credential = load_existing_config(data_dir)
    except StoreError:
        _drop_pid_file(data_dir)
        print(json.dumps({"status": "stopped"}))
        return 0
    if port is None:
        _drop_pid_file(data_dir)  # nothing of ours to talk to; never signal
        print(json.dumps({"status": "stopped"}))
        return 0
    base_url = base_url_for(port)
    if not ping_service(base_url, credential, timeout=3):
        _drop_pid_file(data_dir)  # stale file; never signal its PID
        print(json.dumps({"status": "stopped"}))
        return 0
    try:
        status, _ = http_call(base_url, "/api/internal/shutdown",
                              credential=credential,
                              payload={"arguments": {}}, timeout=10)
    except ConnectionError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1
    if status != 200:
        print(json.dumps({"status": "error",
                          "error": "service refused authenticated shutdown"}))
        return 1
    deadline = time.time() + 10
    while time.time() < deadline:
        if not ping_service(base_url, credential, timeout=2):
            break
        time.sleep(0.1)
    if ping_service(base_url, credential, timeout=2):
        print(json.dumps({"status": "error",
                          "error": "service did not stop after shutdown"}))
        return 1
    _drop_pid_file(data_dir)
    print(json.dumps({"status": "stopped"}))
    return 0


def _drop_pid_file(data_dir):
    try:
        os.unlink(os.path.join(str(data_dir), "service.pid"))
    except OSError:
        pass


def _serve_load_config(data_dir):
    """Creation contract for the serve path.

    A child spawned by ensure_service (APR_SPAWNED_SERVE=1) must NOT take
    the startup lock — the parent holds it while waiting for readiness, so
    locking here would deadlock. The parent preinitializes config, and the
    child loads it read-only, failing closed when it is missing. A directly
    invoked `serve` on a cold directory serializes creation through the
    startup lock itself, so concurrent cold serve/start cannot mint
    mismatched ports or credentials.
    """
    if os.environ.get("APR_SPAWNED_SERVE") == "1":
        port, credential = load_existing_config(data_dir)
        if port is None:
            raise StoreError(
                "spawned service found no preinitialized config; refusing"
                " to mint a port outside the startup lock")
        return port, credential
    startup_lock = _lock_path(data_dir, "startup.lock")
    _refuse_symlink(startup_lock, "startup lock")
    fd = os.open(startup_lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise StoreError(
                f"cannot serialize service startup: {exc}") from exc
        return load_or_create_config(data_dir)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def cmd_serve(data_dir):
    data_dir = Path(resolve_data_dir(data_dir))
    ensure_private_dir(data_dir)
    port, credential = _serve_load_config(data_dir)
    daemon_lock_path = _lock_path(data_dir, "service.lock")
    _refuse_symlink(daemon_lock_path, "service lock")
    lock_fd = os.open(daemon_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        secure_existing_private(daemon_lock_path, "service lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another local service instance is already running", file=sys.stderr)
            return 4
        server = None
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                                     ServiceHandler)
        except OSError as exc:
            # Never silently move to another port: old URLs would strand.
            if ping_service(base_url_for(port), credential, timeout=3):
                print("local service is already running", file=sys.stderr)
                return 0
            print(f"cannot bind 127.0.0.1:{port}: {exc}. The persisted port is"
                  " occupied by another process; stop it or remove the data"
                  " directory configuration before retrying.", file=sys.stderr)
            return 3
        server.daemon_threads = True
        server.service_port = port
        server.credential = credential
        server.store = ReviewStore.open(data_dir)
        pid_path = os.path.join(str(data_dir), "service.pid")
        _atomic_write_private(pid_path, json.dumps(
            {"pid": os.getpid(), "port": port,
             "started_at": datetime.now(timezone.utc).isoformat()}))
        try:
            server.serve_forever()
        finally:
            try:
                server.server_close()
            except OSError:
                pass
            server.store.close()
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def cmd_import(data_dir, legacy_path, dry_run=False):
    data_dir = Path(resolve_data_dir(data_dir))
    ensure_private_dir(data_dir)
    store = ReviewStore.open(data_dir)
    try:
        if not dry_run and os.path.exists(store.db_path):
            backup_dir = str(data_dir / "backups")
            _refuse_symlink(backup_dir, "backup directory")
            os.makedirs(backup_dir, mode=0o700, exist_ok=True)
            os.chmod(backup_dir, 0o700)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            backup = os.path.join(
                backup_dir, f"reviews-{stamp}-{os.getpid()}.db")
            backup_database(store.db_path, backup)
            print(json.dumps({"backup": str(backup)}))
        try:
            result = store.import_jsonl(legacy_path, dry_run=dry_run)
        except ImportAbort as exc:
            print(json.dumps({"status": "aborted", "error": str(exc)}))
            return 2
        print(json.dumps({"status": "dry-run" if dry_run else "imported", **result}))
        return 0
    finally:
        store.close()


def main(argv):
    args = list(argv[1:])
    data_dir = None
    if "--data-dir" in args:
        index = args.index("--data-dir")
        try:
            data_dir = args[index + 1]
        except IndexError:
            print("missing value for --data-dir", file=sys.stderr)
            return 2
        del args[index:index + 2]
    if not args:
        print("usage: local_service.py [--data-dir DIR]"
              " (start|stop|status|serve|import FILE [--dry-run])", file=sys.stderr)
        return 2
    command = args[0]
    try:
        if command == "start":
            return cmd_start(data_dir)
        if command == "stop":
            return cmd_stop(data_dir)
        if command == "status":
            return cmd_status(data_dir)
        if command == "serve":
            return cmd_serve(data_dir)
        if command == "import":
            if len(args) < 2:
                print("import needs a legacy JSONL path", file=sys.stderr)
                return 2
            return cmd_import(data_dir, args[1], dry_run="--dry-run" in args)
        print(f"unknown command: {command}", file=sys.stderr)
        return 2
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
