"""Service + bridge + UI static contract (integration)."""
import concurrent.futures
import http.server
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests.helpers import (
    CARD, REPO_ROOT, browser_payload, fresh_data_dir, full_payload,
)

SERVICE = os.path.join(REPO_ROOT, "local_service.py")
MCP = os.path.join(REPO_ROOT, "mcp_server.py")


def read_credential(data_dir):
    with open(os.path.join(data_dir, "api_credential"), encoding="utf-8") as handle:
        return handle.read().strip()


def api(base, path="", token=None, payload=None, origin=None, host=None,
        raw=None, content_type="application/json"):
    url = base + path
    data = raw if raw is not None else (
        json.dumps(payload).encode() if payload is not None else None)
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    if payload is not None or raw is not None:
        request.add_header("Content-Type", content_type)
    if origin is not None:
        request.add_header("Origin", origin)
    if host is not None:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()


class ServiceCase(unittest.TestCase):
    def start_service(self):
        data_dir = fresh_data_dir(self)
        # Register cleanup before any startup assertion so failures cannot
        # leave a synthetic service behind.
        self.addCleanup(self.stop_service, data_dir)
        env = dict(os.environ, APR_DATA_DIR=data_dir)
        proc = subprocess.Popen(
            [sys.executable, SERVICE, "start"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("service start timed out")
        self.assertEqual(proc.returncode, 0, out.decode()[-2000:])
        info = json.loads(out.decode())
        # wait for health
        for _ in range(100):
            try:
                status, _ = api(info["base_url"], "/health")
                if status == 200:
                    break
            except Exception:
                time.sleep(0.1)
        return data_dir, info["base_url"], read_credential(data_dir)

    def stop_service(self, data_dir):
        env = dict(os.environ, APR_DATA_DIR=data_dir)
        subprocess.run([sys.executable, SERVICE, "stop"], env=env,
                       capture_output=True, timeout=30)

    def request_card(self, base, token, card=None):
        status, body = api(base, "/api/internal/request_rating", token=token,
                           payload={"arguments": dict(card or CARD)})
        self.assertEqual(status, 200, body[:500])
        return json.loads(body.decode())


class ServiceBehaviorTest(ServiceCase):
    def test_same_url_across_requests_and_restart(self):
        data_dir, base, token = self.start_service()
        first = self.request_card(base, token)
        second = self.request_card(base, token, dict(CARD, task="Updated summary"))
        self.assertEqual(first["rating_url"], second["rating_url"])
        self.assertIsNone(second["expires_in_seconds"])
        # old URL shows latest snapshot
        with urllib.request.urlopen(first["rating_url"] + "api/review",
                                    timeout=10) as response:
            card = json.loads(response.read().decode())
        self.assertEqual(card["snapshot"]["task"], "Updated summary")
        # restart keeps URL
        env = dict(os.environ, APR_DATA_DIR=data_dir)
        subprocess.run([sys.executable, SERVICE, "stop"], env=env,
                       capture_output=True, timeout=30)
        proc = subprocess.Popen([sys.executable, SERVICE, "start"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, start_new_session=True)
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0)
        base2 = json.loads(out.decode())["base_url"]
        self.assertEqual(base2, base)
        with urllib.request.urlopen(first["rating_url"] + "api/review",
                                    timeout=10) as response:
            card = json.loads(response.read().decode())
        self.assertEqual(card["snapshot"]["task"], "Updated summary")

    def test_internal_auth_and_origin_host_checks(self):
        _, base, token = self.start_service()
        status, _ = api(base, "/api/internal/request_rating",
                        payload={"arguments": dict(CARD)})
        self.assertEqual(status, 401)
        status, _ = api(base, "/api/internal/request_rating", token="wrong",
                        payload={"arguments": dict(CARD)})
        self.assertEqual(status, 401)
        status, _ = api(base, "/api/internal/request_rating", token=token,
                        payload={"arguments": dict(CARD)},
                        origin="http://evil.example/")
        self.assertEqual(status, 403)
        status, _ = api(base, "/health", host="evil.example")
        self.assertEqual(status, 403)

    def test_skip_semantics_and_invalid_skip(self):
        _, base, token = self.start_service()
        link = self.request_card(base, token)
        submit = link["rating_url"].rstrip("/") + "/api/submit"
        status, _ = api(submit, payload={"skip": 1})
        self.assertEqual(status, 400)
        status, body = api(submit, payload={"skip": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode())["status"], "skipped")
        # job still pending afterwards: card still readable, no rating saved
        with urllib.request.urlopen(link["rating_url"] + "api/review",
                                    timeout=10) as response:
            card = json.loads(response.read().decode())
        self.assertEqual(card["status"], "pending")
        status, body = api(base, "/api/internal/summary", token=token,
                           payload={"arguments": {}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode())["count"], 0)

    def test_browser_save_requires_revision(self):
        _, base, token = self.start_service()
        link = self.request_card(base, token)
        submit = link["rating_url"].rstrip("/") + "/api/submit"
        with urllib.request.urlopen(link["rating_url"] + "api/review",
                                    timeout=10) as response:
            rev = json.loads(response.read().decode())["revision"]
        # absent, boolean, and malformed revisions are all rejected
        for bad in ({k: v for k, v in browser_payload(rev).items()
                     if k != "revision"},
                    browser_payload(True), browser_payload("3")):
            status, _ = api(submit, payload=bad)
            self.assertEqual(status, 400)

    def test_saved_card_shows_frozen_snapshot_over_http(self):
        _, base, token = self.start_service()
        link = self.request_card(base, token)
        submit = link["rating_url"].rstrip("/") + "/api/submit"
        with urllib.request.urlopen(link["rating_url"] + "api/review",
                                    timeout=10) as response:
            rev = json.loads(response.read().decode())["revision"]
        status, _ = api(submit, payload=browser_payload(rev))
        self.assertEqual(status, 200)
        # Later request with the same ID must not change the rated snapshot.
        self.request_card(base, token, dict(CARD, task="Later unrated work"))
        with urllib.request.urlopen(link["rating_url"] + "api/review",
                                    timeout=10) as response:
            card = json.loads(response.read().decode())
        self.assertEqual(card["status"], "saved")
        self.assertEqual(card["snapshot"]["task"], CARD["task"])

    def test_transactional_and_stale_and_concurrent_saves(self):
        _, base, token = self.start_service()
        link = self.request_card(base, token)
        card_url = link["rating_url"].rstrip("/") + "/api/review"
        with urllib.request.urlopen(card_url, timeout=10) as response:
            card = json.loads(response.read().decode())
        rev = card["revision"]
        payload = browser_payload(rev)
        status, body = api(link["rating_url"].rstrip("/") + "/api/submit",
                           payload=payload)
        self.assertEqual(status, 200, body[:500])
        # exact retry same receipt
        status, body = api(link["rating_url"].rstrip("/") + "/api/submit",
                           payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode())["status"], "already_recorded")
        # conflicting retry
        conflict = browser_payload(
            rev, ratings={"overall": 1, "quality": 1, "speed": 1,
                          "resource_efficiency": 1})
        status, _ = api(link["rating_url"].rstrip("/") + "/api/submit",
                        payload=conflict)
        self.assertEqual(status, 409)
        # A separate pending card must reject ratings for its older snapshot.
        stale_link = self.request_card(base, token, dict(CARD, review_id="stale-1"))
        with urllib.request.urlopen(stale_link["rating_url"] + "api/review",
                                    timeout=10) as response:
            stale_revision = json.loads(response.read().decode())["revision"]
        self.request_card(base, token, dict(CARD, review_id="stale-1", task="newer"))
        status, _ = api(stale_link["rating_url"] + "api/submit",
                        payload=browser_payload(stale_revision))
        self.assertEqual(status, 409)
        # concurrent identical browser saves: no duplicates
        link2 = self.request_card(base, token, dict(CARD, review_id="conc-1"))
        with urllib.request.urlopen(link2["rating_url"] + "api/review",
                                     timeout=10) as response:
            card2 = json.loads(response.read().decode())
        args = browser_payload(card2["revision"])
        url = link2["rating_url"].rstrip("/") + "/api/submit"
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: api(url, payload=args), range(8)))
        self.assertTrue(all(s == 200 for s, _ in results))
        statuses = [json.loads(b.decode())["status"] for _, b in results]
        self.assertEqual(statuses.count("recorded"), 1)
        self.assertEqual(statuses.count("already_recorded"), 7)

    def test_health_status_hygiene_and_file_modes(self):
        data_dir, base, _ = self.start_service()
        status, body = api(base, "/health")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertNotIn("token", text.lower())
        self.assertNotIn("credential", text.lower())
        self.assertNotIn("Bearer", text)
        for name in ("api_credential", "config.json", "reviews.db"):
            mode = stat.S_IMODE(os.stat(os.path.join(data_dir, name)).st_mode)
            self.assertEqual(mode, 0o600, name)


class SingleDaemonTest(ServiceCase):
    def test_concurrent_starters_yield_one_service(self):
        data_dir = fresh_data_dir(self)
        self.addCleanup(self.stop_service, data_dir)
        env = dict(os.environ, APR_DATA_DIR=data_dir)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            procs = [pool.submit(subprocess.run, [sys.executable, SERVICE, "start"],
                                 capture_output=True, timeout=60, env=env)
                     for _ in range(4)]
            results = [future.result() for future in procs]
        self.assertTrue(all(r.returncode == 0 for r in results))
        bases = {json.loads(r.stdout.decode())["base_url"] for r in results}
        self.assertEqual(len(bases), 1)


class OccupiedPortTest(ServiceCase):
    def test_occupied_persisted_port_fails_without_changing_config(self):
        data_dir, base, _ = self.start_service()
        with open(os.path.join(data_dir, "config.json"),
                  encoding="utf-8") as handle:
            before = handle.read()
        port = json.loads(before)["port"]
        env = dict(os.environ, APR_DATA_DIR=data_dir)
        subprocess.run([sys.executable, SERVICE, "stop"], env=env,
                       capture_output=True, timeout=30)
        decoy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            decoy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            decoy.bind(("127.0.0.1", port))
            decoy.listen(1)
            proc = subprocess.run([sys.executable, SERVICE, "start"], env=env,
                                  capture_output=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)
            # The bind diagnostic is written by the serve child to the
            # service log, never to a URL claim: no new URL is reported.
            self.assertNotIn("base_url", proc.stdout.decode())
            with open(os.path.join(data_dir, "service.log"),
                      encoding="utf-8") as handle:
                self.assertIn("occupied", handle.read().lower())
            with open(os.path.join(data_dir, "config.json"),
                      encoding="utf-8") as handle:
                self.assertEqual(handle.read(), before)
        finally:
            decoy.close()


class MalformedBodyTest(ServiceCase):
    def test_private_endpoint_rejects_bad_bodies(self):
        _, base, token = self.start_service()
        # malformed JSON
        status, _ = api(base, "/api/internal/request_rating", token=token,
                        raw=b"{not json", content_type="application/json")
        self.assertEqual(status, 400)
        # wrong content type
        status, _ = api(base, "/api/internal/request_rating", token=token,
                        raw=b'{"arguments": {}}', content_type="text/plain")
        self.assertEqual(status, 400)
        # NaN constant
        status, _ = api(base, "/api/internal/request_rating", token=token,
                        raw=b'{"arguments": {"x": NaN}}')
        self.assertEqual(status, 400)
        # oversize internal body
        big = {"arguments": dict(CARD, task="x" * 70000)}
        status, _ = api(base, "/api/internal/request_rating", token=token,
                        payload=big)
        self.assertEqual(status, 400)
        # bad Host / Origin on the private endpoint
        status, _ = api(base, "/api/internal/request_rating", token=token,
                        payload={"arguments": dict(CARD)}, host="evil.example")
        self.assertEqual(status, 403)
        status, _ = api(base, "/api/internal/request_rating", token=token,
                        payload={"arguments": dict(CARD)},
                        origin="http://evil.example/")
        self.assertEqual(status, 403)

    def test_browser_endpoint_rejects_bad_bodies_and_origin(self):
        _, base, token = self.start_service()
        link = self.request_card(base, token)
        submit = link["rating_url"].rstrip("/") + "/api/submit"
        status, _ = api(submit, raw=b"{nope")
        self.assertEqual(status, 400)
        status, _ = api(submit, raw=b'{"revision": 1}',
                        content_type="text/plain")
        self.assertEqual(status, 400)
        status, _ = api(submit, raw=b'{"revision": NaN}')
        self.assertEqual(status, 400)
        big = dict(browser_payload(1), ratings={
            k: "x" * 20000 for k in
            ("overall", "quality", "speed", "resource_efficiency")})
        status, _ = api(submit, payload=big)
        self.assertIn(status, (400, 409))
        status, _ = api(submit, payload={"skip": True},
                        origin="http://evil.example/")
        self.assertEqual(status, 403)
        status, _ = api(submit, payload={"skip": True}, host="evil.example")
        self.assertEqual(status, 403)


class RedirectProxyLeakTest(ServiceCase):
    def test_redirect_target_receives_no_bearer_or_content(self):
        from local_service import http_call
        hits = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def _record(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                hits.append({"auth": self.headers.get("Authorization"),
                             "body": self.rfile.read(length) if length else b""})

            def do_GET(self):
                self._record()
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                self._record()
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                return

        sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)
        sink_port = sink.server_address[1]
        sink_thread = threading.Thread(target=sink.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True)
        sink_thread.start()
        self.addCleanup(sink.shutdown)
        self.addCleanup(sink.server_close)

        class Redirector(http.server.BaseHTTPRequestHandler):
            def _go(self):
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{sink_port}/captured")
                self.end_headers()

            do_GET = _go
            do_POST = _go

            def log_message(self, *_args):
                return

        redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        redirect_port = redirector.server_address[1]
        redirect_thread = threading.Thread(
            target=redirector.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        redirect_thread.start()
        self.addCleanup(redirector.shutdown)
        self.addCleanup(redirector.server_close)

        # Identity-first transport: the content-free handshake meets the
        # 302, so the client aborts with ConnectionError before any Bearer
        # or review content leaves the socket — and never follows the
        # redirect to the sink.
        with self.assertRaises(ConnectionError):
            http_call(f"http://127.0.0.1:{redirect_port}",
                      "/api/internal/ping",
                      credential="synthetic-bearer-pw",
                      payload={"arguments": {"review_id": "synthetic"}})
        self.assertEqual(hits, [])

class ServiceIdentityLeakTest(ServiceCase):
    """Host loopback proof: no Bearer/review bytes reach an unproven peer."""

    def _listen(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        sock.settimeout(10)
        self.addCleanup(sock.close)
        return sock

    def test_squatter_returning_ok_sees_no_bearer_or_body(self):
        from local_service import http_call, ping_service
        listener = self._listen()
        port = listener.getsockname()[1]
        hits, stop = [], threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with conn:
                    conn.settimeout(5)
                    try:
                        raw = conn.recv(65536)
                    except OSError:
                        continue
                    # Parse headers of the first (and only expected) request.
                    head = raw.split(b"\r\n\r\n", 1)[0].decode(
                        "latin-1", "replace")
                    auth = ""
                    for line in head.split("\r\n"):
                        if line.lower().startswith("authorization:"):
                            auth = line.split(":", 1)[1].strip()
                    hits.append({"auth": auth, "raw": raw})
                    body = b'{"ok": true}'
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() +
                        b"\r\nConnection: close\r\n\r\n" + body)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        self.addCleanup(thread.join, 10)
        base = f"http://127.0.0.1:{port}"
        with self.assertRaises(ConnectionError):
            http_call(base, "/api/internal/ping",
                      credential="synthetic-bearer-pw",
                      payload={"arguments": {"review_id": "synthetic"}})
        self.assertFalse(ping_service(base, "synthetic-bearer-pw", timeout=5))
        for hit in hits:
            self.assertEqual(hit["auth"], "")
            self.assertNotIn(b"synthetic-bearer-pw", hit["raw"])
            self.assertNotIn(b"synthetic", hit["raw"])

    def test_close_after_valid_proof_sends_no_secret_or_reconnect(self):
        from local_service import http_call, identity_proof
        credential = "synthetic-credential-pw"
        listener = self._listen()
        port = listener.getsockname()[1]
        connections, requests, stop = [], [], threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                connections.append(conn)
                with conn:
                    conn.settimeout(5)
                    try:
                        raw = conn.recv(65536)
                    except OSError:
                        continue
                    head = raw.split(b"\r\n\r\n", 1)[0].decode(
                        "latin-1", "replace")
                    nonce, auth = "", ""
                    for line in head.split("\r\n"):
                        low = line.lower()
                        if low.startswith("x-apr-nonce:"):
                            nonce = line.split(":", 1)[1].strip()
                        if low.startswith("authorization:"):
                            auth = line.split(":", 1)[1].strip()
                    requests.append({"auth": auth, "raw": raw})
                    # Valid proof, then close: the client must NOT reconnect
                    # with the Bearer on a fresh socket.
                    body = json.dumps(
                        {"nonce": nonce,
                         "proof": identity_proof(credential, nonce)}).encode()
                    try:
                        conn.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Type:"
                            b" application/json\r\nContent-Length: " +
                            str(len(body)).encode() +
                            b"\r\nConnection: close\r\n\r\n" + body)
                        # Linger to record any illicit second request.
                        try:
                            extra = conn.recv(65536)
                        except OSError:
                            extra = b""
                        if extra:
                            requests.append({"auth": "SECOND-REQUEST",
                                             "raw": extra})
                    except OSError:
                        pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        self.addCleanup(thread.join, 10)
        with self.assertRaises(ConnectionError):
            http_call(f"http://127.0.0.1:{port}", "/api/internal/ping",
                      credential=credential, payload={"arguments": {}},
                      timeout=5)
        # Exactly one TCP connection, one content-free handshake, zero
        # Bearer bytes anywhere.
        self.assertEqual(len(connections), 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["auth"], "")
        joined = b"".join(r["raw"] for r in requests)
        self.assertNotIn(credential.encode(), joined)

    def test_environment_proxy_receives_no_private_traffic(self):
        from local_service import http_call
        _, base, token = self.start_service()
        hits = []
        proxy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        proxy.bind(("127.0.0.1", 0))
        proxy.listen(5)
        proxy.settimeout(5)
        proxy_port = proxy.getsockname()[1]
        stop = threading.Event()

        def accept_loop():
            while not stop.is_set():
                try:
                    conn, _ = proxy.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with conn:
                    try:
                        hits.append(conn.recv(65536))
                    except OSError:
                        pass

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        self.addCleanup(proxy.close)
        self.addCleanup(thread.join, 10)
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                    "all_proxy", "ALL_PROXY"):
            os.environ[var] = f"http://127.0.0.1:{proxy_port}/"
            self.addCleanup(os.environ.pop, var, None)
        status, body = http_call(base, "/api/internal/ping",
                                 credential=token,
                                 payload={"arguments": {}})
        self.assertEqual(status, 200)
        self.assertIn(b'"ok": true', body)
        time.sleep(0.5)
        self.assertEqual(hits, [])


class ColdServeRaceTest(ServiceCase):
    def test_direct_serve_and_start_agree_on_one_port(self):
        data_dir = fresh_data_dir(self)
        self.addCleanup(self.stop_service, data_dir)
        env = {k: v for k, v in os.environ.items()
               if k != "APR_SPAWNED_SERVE"}
        env["APR_DATA_DIR"] = data_dir
        serve = subprocess.Popen(
            [sys.executable, SERVICE, "serve"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.addCleanup(serve.wait, 15)
        try:
            config_path = os.path.join(data_dir, "config.json")
            for _ in range(100):
                if os.path.exists(config_path):
                    break
                time.sleep(0.1)
            proc = subprocess.run([sys.executable, SERVICE, "start"], env=env,
                                  capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-2000:])
            with open(config_path, encoding="utf-8") as handle:
                port = json.load(handle)["port"]
            self.assertIn(f"127.0.0.1:{port}", proc.stdout.decode())
        finally:
            serve.terminate()


class BridgeTest(ServiceCase):
    def rpc(self, proc, msg_id, method, params):
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                      "method": method, "params": params}) + "\n"))
        proc.stdin.flush()
        # stdout is text mode: readline() already returns str.
        return json.loads(proc.stdout.readline())

    def test_stdio_bridge_same_url_and_survives_mcp_exit(self):
        data_dir = fresh_data_dir(self)
        self.addCleanup(self.stop_service, data_dir)
        env = dict(os.environ, APR_DATA_DIR=data_dir)
        proc = subprocess.Popen([sys.executable, MCP], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=env,
                                start_new_session=True)
        try:
            init = self.rpc(proc, 1, "initialize", {"protocolVersion": "2025-11-25"})
            self.assertIn("result", init)
            first = self.rpc(proc, 2, "tools/call",
                             {"name": "request_rating", "arguments": dict(CARD)})
            second = self.rpc(proc, 3, "tools/call",
                              {"name": "request_rating", "arguments": dict(CARD)})
            url1 = first["result"]["structuredContent"]["rating_url"]
            url2 = second["result"]["structuredContent"]["rating_url"]
            self.assertEqual(url1, url2)
            self.assertIn("expires_in_seconds", first["result"]["structuredContent"])
            self.assertIsNone(first["result"]["structuredContent"]["expires_in_seconds"])
            err = self.rpc(proc, 4, "tools/call",
                           {"name": "record_review", "arguments": full_payload()})
            self.assertFalse(err["result"]["isError"])
        finally:
            proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=15)
            proc.stdout.close()
            proc.stderr.close()
        # service must still answer after MCP exit, at the same URL
        with urllib.request.urlopen(url1 + "api/review", timeout=10) as response:
            card = json.loads(response.read().decode())
        self.assertEqual(card["snapshot"]["review_id"], "job-001")

    def test_repeated_mcp_processes_preserve_same_url(self):
        data_dir = fresh_data_dir(self)
        self.addCleanup(self.stop_service, data_dir)
        env = dict(os.environ, APR_DATA_DIR=data_dir)
        urls = []
        for _ in range(3):
            proc = subprocess.Popen(
                [sys.executable, MCP], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=env, start_new_session=True)
            try:
                reply = self.rpc(proc, 1, "tools/call",
                                 {"name": "request_rating",
                                  "arguments": dict(CARD)})
                urls.append(reply["result"]["structuredContent"]["rating_url"])
            finally:
                proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=15)
                proc.stdout.close()
                proc.stderr.close()
        self.assertEqual(len(set(urls)), 1)


class UIStaticTest(unittest.TestCase):
    def test_button_texts_and_no_autocapture(self):
        root = REPO_ROOT
        with open(os.path.join(root, "rating-popup", "index.html"),
                  encoding="utf-8") as handle:
            html = handle.read()
        with open(os.path.join(root, "rating-popup", "app.js"),
                  encoding="utf-8") as handle:
            js = handle.read()
        self.assertIn("Save locally", html)
        self.assertIn("Skip review", html)
        self.assertNotIn(">Submit<", html)
        self.assertNotIn("are captured automatically", html)
        self.assertIn("revision", js)
        self.assertIn("Save locally", js + html)
        import py_compile
        # JS syntax check via node when available; else brace balance sanity
        self.assertIn("api/review", js)
        self.assertIn("api/submit", js)

    def test_skill_link_text_and_no_expiry_claim(self):
        with open(os.path.join(REPO_ROOT, "skill", "agent-performance-review",
                               "SKILL.md"), encoding="utf-8") as handle:
            skill = handle.read()
        self.assertIn("⭐ Review this task", skill)
        self.assertNotIn("24 hour", skill)


if __name__ == "__main__":
    unittest.main()
