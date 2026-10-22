#!/usr/bin/env python3
"""Synthetic local-alpha smoke check for the APR MCP bridge.

Drives mcp_server.py over stdio JSON-RPC exactly as an MCP host would:
initialize, tools/list, then get_setup -> start_task -> append_evidence ->
finish_task -> get_task. It then stops the detached service, starts a fresh
bridge process, and confirms the same task and stable task_url survive the
restart. A start_task retry with the same request_id must return the same task.

All data lives in a script-owned temporary APR_DATA_DIR that is removed at the
end once the final service stop succeeds. If that stop fails, the script exits
nonzero without PASS and keeps the data so the printed stop command can recover
a possibly live service. Nothing is installed or registered,
and HOME/CODEX_HOME are untouched. Passing this script is source-level protocol
evidence only; it is not acceptance in a real Codex session.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "mcp_server.py"
SERVICE = ROOT / "local_service.py"
REPLY_TIMEOUT = 30
LIFECYCLE = ["get_setup", "start_task", "append_evidence", "finish_task", "get_task"]


class Bridge:
    def __init__(self, env):
        self.proc = subprocess.Popen([sys.executable, str(BRIDGE)], env=env, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id,
                                          "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        # Bound each reply: a hung bridge is killed, so readline returns "" and fails.
        timer = threading.Timer(REPLY_TIMEOUT, self.proc.kill)
        timer.start()
        try:
            line = self.proc.stdout.readline()
        finally:
            timer.cancel()
        if not line:
            raise AssertionError(f"{method}: no reply from bridge within {REPLY_TIMEOUT}s")
        reply = json.loads(line)
        if "error" in reply:
            raise AssertionError(f"{method} failed: {reply['error']}")
        return reply["result"]

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise AssertionError(f"{name} returned an error: {result['content'][0]['text']}")
        return result["structuredContent"]

    def close(self):
        if self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait(timeout=10)


def handshake(bridge):
    init = bridge.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "apr-smoke", "version": "0"}})
    bridge.notify("notifications/initialized")
    tools = {tool["name"]: tool for tool in bridge.request("tools/list")["tools"]}
    return init, tools


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main():
    tmp = tempfile.mkdtemp(prefix="apr-smoke-")
    data_dir = str(Path(tmp) / "data")
    env = dict(os.environ, APR_DATA_DIR=data_dir)
    bridges = []
    try:
        run(env, bridges)
    finally:
        for bridge in bridges:
            bridge.close()
        try:
            stopped = subprocess.run([sys.executable, str(SERVICE), "stop"], env=env,
                                     capture_output=True, text=True, timeout=30)
            stop_ok, stop_detail = stopped.returncode == 0, stopped.stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            stop_ok, stop_detail = False, repr(exc)
        if not stop_ok:
            # The service may still be live; keep its data so the normal stop can recover it.
            print(f"FAIL final service stop: {stop_detail}; retained {tmp}")
            print(f"recover with: APR_DATA_DIR={data_dir} {sys.executable} {SERVICE} stop")
            sys.exit(1)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS synthetic MCP lifecycle and restart smoke (not real-host acceptance)")


def run(env, bridges):
    bridge = Bridge(env)
    bridges.append(bridge)
    init, tools = handshake(bridge)
    check(init["serverInfo"]["name"] == "agent-performance-review",
          f"initialize -> {init['serverInfo']} protocol {init['protocolVersion']}")
    check(all(name in tools for name in LIFECYCLE), f"tools/list exposes {sorted(tools)}")
    start_required = tools["start_task"]["inputSchema"]["required"]

    setup = bridge.call("get_setup", {})
    check(isinstance(setup, dict), "get_setup returns an object")

    baseline = {
        "request_id": "smoke-req-1",
        "title": "Synthetic smoke task",
        "category": "other",
        "goal": "List three acceptance criteria for a synthetic example.",
        "success_criteria": ["Exactly three criteria", "Each criterion is testable"],
        "runtime": "unknown",
        "model": "unknown",
        "baseline_source": "user-stated",
        "harness": {"mode": "newcomer", "version": "unknown", "roles": [], "loops": [],
                    "rules": [], "tools": [], "context_categories": []},
    }
    check(set(start_required) <= set(baseline), f"start_task payload covers required {start_required}")
    task = bridge.call("start_task", baseline)
    task_id, task_url = task["task_id"], task["task_url"]
    check(task_url.startswith("http://127.0.0.1:"), f"start_task -> {task_id} at loopback task_url")
    retry = bridge.call("start_task", baseline)
    check(retry["task_id"] == task_id and retry["task_url"] == task_url,
          "identical start_task retry returns the same task")

    bridge.call("append_evidence", {"task_id": task_id, "event_id": "smoke-ev-1", "kind": "milestone",
                                    "source": "agent-reported", "data": {"note": "drafted criteria"}})
    replay = bridge.call("append_evidence", {"task_id": task_id, "event_id": "smoke-ev-1",
                                             "kind": "milestone", "source": "agent-reported",
                                             "data": {"note": "drafted criteria"}})
    check(replay["status"] == "already_recorded", "identical append_evidence retry is already_recorded")
    finished = bridge.call("finish_task", {"task_id": task_id, "event_id": "smoke-finish",
                                           "result": "Three synthetic criteria listed.",
                                           "verification": {"status": "unverified"}})
    check(isinstance(finished, dict), "finish_task accepted")
    detail = bridge.call("get_task", {"task_id": task_id})
    check(detail.get("status") == "awaiting_review", f"task status is {detail.get('status')!r}")
    check("review" in detail and detail["review"] is None, "no human review was invented")
    check([event["event_id"] for event in detail["evidence"]] == ["smoke-ev-1", "smoke-finish"],
          "evidence holds one copy of the retried event plus finish")
    bridge.close()

    with urllib.request.urlopen(task_url, timeout=10) as response:
        check(response.status == 200, "task_url serves the local workspace")

    stopped = subprocess.run([sys.executable, str(SERVICE), "stop"], env=env,
                             capture_output=True, text=True, timeout=30)
    check(stopped.returncode == 0, f"service stop: {stopped.stdout.strip()}")

    bridge = Bridge(env)
    bridges.append(bridge)
    handshake(bridge)
    again = bridge.call("get_task", {"task_id": task_id})
    check(again.get("status") == "awaiting_review", "task survives service restart")
    check(bridge.call("start_task", baseline)["task_url"] == task_url,
          "task_url is stable after restart")
    bridge.close()


if __name__ == "__main__":
    main()
