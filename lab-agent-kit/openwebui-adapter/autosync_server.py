#!/usr/bin/env python3
"""
Harnessmith -> Open WebUI Auto-Sync Callback Server (Port 8766, loopback only).

Receives Harnessmith reply_url POST callbacks when a goal run finishes:
1. Gate 1: Skip if status != "done" (e.g. "review", "failed", "cancelled") or errors present.
2. Gate 2: Fetch adopted harness + latest LongTermMEMORY.md; skip if identical to target model.
3. Gate 3: Probe candidate harness on isolated testworkspace-probe via Open WebUI
           /api/chat/completions and run regression_suite.json.
4. Gate 4: If all regression tests pass, apply versioned update to target Open WebUI model(s)
           and record full audit log (with previous_version_id for instant rollback).
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter  # noqa: E402

adapter.load_env_file()

_SCRIPT_DIR = Path(__file__).resolve().parent
EVENTS_LOG = Path(os.path.expanduser(os.environ.get("AUTOSYNC_EVENTS_LOG", str(_SCRIPT_DIR / "autosync_events.jsonl"))))
REGRESSION_SUITE = Path(os.path.expanduser(os.environ.get("REGRESSION_SUITE", str(_SCRIPT_DIR / "regression_suite.json"))))
HARNESS_URL = os.environ.get("HARNESS_URL", "http://127.0.0.1:8765").rstrip("/")
SYNC_LOCK = threading.Lock()


def record_event(event: dict[str, Any]) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    print("[autosync]", json.dumps(event, ensure_ascii=False), flush=True)


def fetch_json(url: str, timeout: int = 15) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_effective_harness(project: str, goal_id: str) -> tuple[dict[str, str], bool]:
    """
    Returns (harness_files, had_explicit_edit).
    Combines the goal adopted harness (if present) with the latest project LongTermMEMORY.md.
    """
    proj = fetch_json(f"{HARNESS_URL}/api/projects/{urllib.parse.quote(project)}")
    base_tree = {
        k: v.strip()
        for k, v in (proj.get("base") or {}).items()
        if isinstance(v, str) and v.strip()
    }
    harness = dict(base_tree)
    had_edit = False

    if goal_id:
        try:
            g = fetch_json(
                f"{HARNESS_URL}/api/projects/{urllib.parse.quote(project)}/goals/{urllib.parse.quote(goal_id)}"
            )
            for ns in g.get("namespaces") or []:
                results = ns.get("results") or {}
                for mode_res in results.values():
                    if not isinstance(mode_res, dict):
                        continue
                    h_map = mode_res.get("harness")
                    if isinstance(h_map, dict) and h_map:
                        for k, v in h_map.items():
                            if isinstance(v, str) and v.strip():
                                harness[k] = v.strip()
                    diff = mode_res.get("diff")
                    edits = mode_res.get("edits")
                    if (isinstance(diff, (dict, list)) and len(diff) > 0) or (
                        isinstance(edits, (dict, list)) and len(edits) > 0
                    ):
                        had_edit = True
                    break
        except Exception as e:
            record_event({"stage": "fetch_goal_warn", "project": project, "goal_id": goal_id, "error": str(e)})

    # Always keep the freshest promoted LongTermMEMORY.md from project base
    if base_tree.get("LongTermMEMORY.md"):
        harness["LongTermMEMORY.md"] = base_tree["LongTermMEMORY.md"]
    return harness, had_edit


def run_probe_regression(
    client: adapter.OpenWebUIClient,
    base_config: dict[str, Any],
    candidate_harness: dict[str, str],
    probe_id: str = "testworkspace-probe",
) -> tuple[bool, list[dict[str, Any]]]:
    suite = json.loads(REGRESSION_SUITE.read_text(encoding="utf-8"))
    adapter.prepare_test_model(client, base_config, candidate_harness, probe_model_id=probe_id)
    results = []
    try:
        for case in suite:
            raw = client.run_chat_completion(
                model_id=probe_id,
                task=case["task"],
                features=case.get("features") or {"memory": True, "code_interpreter": False},
                timeout=180,
            )
            ev = adapter.collect_execution_evidence(
                raw,
                model_id=probe_id,
                task=case["task"],
                must_contain=case.get("must_contain"),
                must_not_contain=case.get("must_not_contain"),
            )
            ev["case_id"] = case.get("id")
            results.append(ev)
    finally:
        try:
            client.delete_model(probe_id)
            client.refresh_models()
        except Exception:
            pass
    return all(r["passed"] for r in results), results


def process_callback(payload: dict[str, Any], targets: list[str], override_harness: dict[str, str] | None = None) -> None:
    run_id = str(payload.get("run_id") or "")
    project = str(payload.get("project") or "agent-harness-lab")
    goal_id = str(payload.get("goal_id") or "")
    status = str(payload.get("status") or "").lower()
    errors = payload.get("errors") or []

    # Gate 1: Strict status gate
    if status != "done" or errors:
        record_event(
            {
                "action": "skipped",
                "reason": "status_not_done_or_has_errors",
                "run_id": run_id,
                "goal_id": goal_id,
                "status": status,
                "errors": errors,
            }
        )
        return

    with SYNC_LOCK:
        try:
            client = adapter.OpenWebUIClient()
            if override_harness is not None:
                candidate_harness, had_edit = override_harness, True
            else:
                candidate_harness, had_edit = fetch_effective_harness(project, goal_id)

            if not candidate_harness:
                record_event({"action": "skipped", "reason": "empty_harness", "run_id": run_id, "goal_id": goal_id})
                return

            # Gate 2: Determine which targets actually have a diff
            pending_targets: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
            for tid in targets:
                if not client.model_exists(tid):
                    continue
                current = client.get_model(tid)
                old_sys = (current.get("params") or {}).get("system") or ""
                new_payload = adapter.build_model_payload_with_harness(
                    base_config=current,
                    harness_files=candidate_harness,
                    target_id=current["id"],
                    target_name=current["name"],
                )
                diff = adapter.system_prompt_diff(old_sys, new_payload["params"]["system"])
                if diff.strip():
                    pending_targets.append((tid, current, new_payload, diff))

            if not pending_targets:
                record_event(
                    {
                        "action": "skipped",
                        "reason": "no_harness_or_memory_diff",
                        "run_id": run_id,
                        "goal_id": goal_id,
                        "had_edit": had_edit,
                        "targets": targets,
                    }
                )
                return

            # Gate 3: Run isolated probe regression tests BEFORE modifying any real model
            base_for_probe = pending_targets[0][1]
            all_passed, reg_results = run_probe_regression(client, base_for_probe, candidate_harness)
            if not all_passed:
                record_event(
                    {
                        "action": "rejected",
                        "reason": "regression_failed",
                        "run_id": run_id,
                        "goal_id": goal_id,
                        "passed": sum(1 for r in reg_results if r["passed"]),
                        "total": len(reg_results),
                    }
                )
                return

            # Gate 4: Apply versioned update to every pending target model
            applied_list = []
            for tid, current, new_payload, diff in pending_targets:
                prev_ver = current.get("version_id")
                updated = client.update_model(tid, new_payload)
                applied_list.append(
                    {
                        "target": tid,
                        "previous_version_id": prev_ver,
                        "new_version_id": updated.get("version_id"),
                        "diff_lines": len(diff.splitlines()),
                    }
                )
            client.refresh_models()

            record_event(
                {
                    "action": "applied",
                    "run_id": run_id,
                    "goal_id": goal_id,
                    "project": project,
                    "regression_passed": f"{len(reg_results)}/{len(reg_results)}",
                    "updates": applied_list,
                }
            )
        except Exception as e:
            record_event({"action": "error", "run_id": run_id, "goal_id": goal_id, "error": str(e)})


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"status": "ok", "service": "openwebui-autosync"})
            return
        if parsed.path == "/events":
            lines = []
            if EVENTS_LOG.is_file():
                for l in EVENTS_LOG.read_text(encoding="utf-8").splitlines()[-20:]:
                    try:
                        lines.append(json.loads(l))
                    except Exception:
                        pass
            self._send(200, {"events": lines})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self._send(404, {"error": "not found"})
            return
        qs = urllib.parse.parse_qs(parsed.query)
        want = os.environ.get("HARNESS2_AUTOSYNC_TOKEN", "")
        got = (qs.get("token") or [""])[0] or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not want or not hmac.compare_digest(got, want):
            self._send(403, {"error": "invalid token"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body_raw = self.rfile.read(length)
        try:
            payload = json.loads(body_raw.decode("utf-8"))
        except Exception:
            self._send(400, {"error": "invalid json"})
            return

        raw_targets = (qs.get("targets") or ["testworkspace-lab,testworkspace"])[0]
        targets = [t.strip() for t in raw_targets.split(",") if t.strip()]
        sync_mode = (qs.get("sync") or ["0"])[0] == "1"
        override_harness = payload.get("_override_harness") if isinstance(payload.get("_override_harness"), dict) else None

        if sync_mode:
            process_callback(payload, targets, override_harness=override_harness)
            self._send(200, {"ok": True, "processed": True})
        else:
            t = threading.Thread(
                target=process_callback,
                args=(payload, targets),
                kwargs={"override_harness": override_harness},
                daemon=True,
            )
            t.start()
            self._send(200, {"ok": True, "accepted": True, "targets": targets})


def main() -> None:
    adapter.load_env_file()
    host = os.environ.get("AUTOSYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("AUTOSYNC_PORT", "8766"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Open WebUI Auto-Sync Server listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
