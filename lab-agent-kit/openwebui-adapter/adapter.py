#!/usr/bin/env python3
"""
Open WebUI Executor Adapter for Harnessmith (Harness² Stage 5).

Bridges Harnessmith candidate harnesses and Open WebUI Workspace models so
Probe / Evaluation runs through Open WebUI /api/chat/completions (with native
built-in tools, features, and session context) instead of bypassing Open WebUI,
and supports diff preview, approved apply, version history, rollback, and
regression testing.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


HARNESS_BEGIN = "<!-- HARNESS2:BEGIN -->"
HARNESS_END = "<!-- HARNESS2:END -->"
PRODUCTION_MODEL_IDS = {"testworkspace"}

HARNESS_FILES_ORDER = [
    ("systemprompt.md", "System Prompt"),
    ("guardrails.md", "Guardrails"),
    ("tool_descriptions/tool_guidance.md", "Tool Guidance"),
    ("LongTermMEMORY.md", "Long-Term Memory"),
]


def load_env_file(path: Path | None = None) -> None:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    elif os.environ.get("ENV_FILE"):
        candidates.append(Path(os.path.expanduser(os.environ["ENV_FILE"])))
    else:
        candidates.append(Path(__file__).resolve().parent.parent / ".env")
        candidates.append(Path(os.path.expanduser("~/agent/harnessmith/.env")))
    p = next((c for c in candidates if c.is_file()), None)
    if p is None:
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("\"").strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


class OpenWebUIClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        load_env_file()
        self.base_url = (base_url or os.environ.get("OPENWEBUI_URL", "http://127.0.0.1:13000")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENWEBUI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("Missing OPENWEBUI_API_KEY (set env var or .env)")

    def request(self, method: str, path: str, payload: Any = None, timeout: int = 60) -> Any:
        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Open WebUI {method} {path} -> HTTP {e.code}: {body}") from e

    def get_model(self, model_id: str) -> dict[str, Any]:
        q = urllib.parse.urlencode({"id": model_id})
        try:
            m = self.request("GET", f"/api/v1/models/model?{q}")
            if isinstance(m, dict) and m.get("id") == model_id:
                return m
        except RuntimeError:
            pass
        models = self.request("GET", "/api/v1/models/export")
        for m in models:
            if m.get("id") == model_id:
                return m
        raise KeyError(f"Open WebUI model not found: {model_id}")

    def model_exists(self, model_id: str) -> bool:
        try:
            self.get_model(model_id)
            return True
        except KeyError:
            return False

    def create_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v1/models/create", payload)

    def update_model(self, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        q = urllib.parse.urlencode({"id": model_id})
        return self.request("POST", f"/api/v1/models/model/update?{q}", payload)

    def refresh_models(self) -> Any:
        return self.request("GET", "/api/models?refresh=true")

    def delete_model(self, model_id: str) -> Any:
        q = urllib.parse.urlencode({"id": model_id})
        return self.request("DELETE", f"/api/v1/models/model/delete?{q}")

    def list_history(self, model_id: str) -> list[dict[str, Any]]:
        q = urllib.parse.urlencode({"id": model_id})
        res = self.request("GET", f"/api/v1/models/model/history?{q}")
        return res if isinstance(res, list) else []

    def rollback_model(self, model_id: str, version_id: str) -> dict[str, Any]:
        q = urllib.parse.urlencode({"id": model_id})
        return self.request(
            "POST",
            f"/api/v1/models/model/update/version?{q}",
            {"version_id": version_id},
        )

    def run_chat_completion(
        self,
        model_id: str,
        task: str,
        session_id: str | None = None,
        features: dict[str, Any] | None = None,
        tool_ids: list[str] | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        sid = session_id or f"harness2-probe-{uuid.uuid4().hex[:12]}"
        body: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": task}],
            "stream": False,
            "session_id": sid,
            "metadata": {"session_id": sid, "source": "harness2-adapter"},
        }
        if features:
            body["features"] = features
        if tool_ids:
            body["tool_ids"] = tool_ids
        t0 = time.time()
        resp = self.request("POST", "/api/chat/completions", body, timeout=timeout)
        elapsed = round(time.time() - t0, 3)
        return {
            "session_id": sid,
            "elapsed_s": elapsed,
            "request_features": features or {},
            "response": resp,
        }


def load_harness_from_dir(harness_dir: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for rel, _ in HARNESS_FILES_ORDER:
        p = harness_dir / rel
        if p.is_file():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                files[rel] = txt
    return files


def load_harness_from_project_api(project: str, harness_url: str | None = None) -> dict[str, str]:
    load_env_file()
    base = (harness_url or os.environ.get("HARNESS_URL", "http://127.0.0.1:8765")).rstrip("/")
    with urllib.request.urlopen(f"{base}/api/projects/{urllib.parse.quote(project)}", timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    base_tree = data.get("base") or {}
    return {k: v.strip() for k, v in base_tree.items() if isinstance(v, str) and v.strip()}


def compile_harness_to_system_prompt(existing_system: str | None, harness_files: dict[str, str]) -> str:
    sections: list[str] = []
    for rel, heading in HARNESS_FILES_ORDER:
        txt = (harness_files.get(rel) or "").strip()
        if txt:
            sections.append(f"### {heading}\n{txt}")
    for rel, txt in sorted(harness_files.items()):
        if rel not in {r for r, _ in HARNESS_FILES_ORDER} and txt.strip():
            sections.append(f"### {rel}\n{txt.strip()}")

    block = f"{HARNESS_BEGIN}\n" + "\n\n".join(sections) + f"\n{HARNESS_END}"
    existing = (existing_system or "").strip()
    pattern = re.compile(re.escape(HARNESS_BEGIN) + r".*?" + re.escape(HARNESS_END), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(block, existing).strip()
    if existing:
        return f"{existing}\n\n{block}"
    return block


def build_model_payload_with_harness(
    base_config: dict[str, Any],
    harness_files: dict[str, str],
    target_id: str,
    target_name: str,
    description: str | None = None,
) -> dict[str, Any]:
    params = json.loads(json.dumps(base_config.get("params") or {}))
    params["system"] = compile_harness_to_system_prompt(params.get("system"), harness_files)
    meta = json.loads(json.dumps(base_config.get("meta") or {}))
    if description is not None:
        meta["description"] = description
    return {
        "id": target_id,
        "base_model_id": base_config["base_model_id"],
        "name": target_name,
        "params": params,
        "meta": meta,
        "access_grants": base_config.get("access_grants") or [],
        "is_active": True,
    }


def prepare_test_model(
    client: OpenWebUIClient,
    base_config: dict[str, Any],
    candidate_harness: dict[str, str],
    probe_model_id: str = "testworkspace-probe",
) -> dict[str, Any]:
    if probe_model_id in PRODUCTION_MODEL_IDS:
        raise ValueError(f"Refusing to use production model {probe_model_id!r} as probe model")
    payload = build_model_payload_with_harness(
        base_config=base_config,
        harness_files=candidate_harness,
        target_id=probe_model_id,
        target_name=probe_model_id,
        description=f"Isolated Harness² probe model derived from {base_config.get('id')}.",
    )
    if client.model_exists(probe_model_id):
        res = client.update_model(probe_model_id, payload)
    else:
        res = client.create_model(payload)
    client.refresh_models()
    return res


def collect_execution_evidence(
    run_result: dict[str, Any],
    model_id: str,
    task: str,
    must_contain: list[str] | None = None,
    must_not_contain: list[str] | None = None,
) -> dict[str, Any]:
    resp = run_result.get("response") or {}
    choices = resp.get("choices") or []
    message = choices[0].get("message") if choices else {}
    content = (message or {}).get("content") or ""
    tool_calls = (message or {}).get("tool_calls") or []
    finish_reason = choices[0].get("finish_reason") if choices else None

    checks: list[dict[str, Any]] = []
    checks.append({"check": "non_empty_response", "passed": bool(content.strip())})
    for kw in must_contain or []:
        checks.append({"check": f"contains:{kw}", "passed": kw.lower() in content.lower()})
    for kw in must_not_contain or []:
        checks.append({"check": f"not_contains:{kw}", "passed": kw.lower() not in content.lower()})

    passed_all = all(c["passed"] for c in checks)
    return {
        "model_id": model_id,
        "session_id": run_result.get("session_id"),
        "elapsed_s": run_result.get("elapsed_s"),
        "features": run_result.get("request_features"),
        "task": task,
        "finish_reason": finish_reason,
        "tool_calls": tool_calls,
        "usage": resp.get("usage") or {},
        "assistant_content": content,
        "checks": checks,
        "passed": passed_all,
    }


def evaluate_harness(
    client: OpenWebUIClient,
    task: str,
    candidate_harness: dict[str, str],
    base_model_id: str = "testworkspace",
    probe_model_id: str = "testworkspace-probe",
    features: dict[str, Any] | None = None,
    must_contain: list[str] | None = None,
    must_not_contain: list[str] | None = None,
    keep_probe: bool = False,
) -> dict[str, Any]:
    base_config = client.get_model(base_model_id)
    probe_model = prepare_test_model(client, base_config, candidate_harness, probe_model_id=probe_model_id)
    default_features = features if features is not None else {
        "memory": True,
        "code_interpreter": True,
    }
    try:
        raw = client.run_chat_completion(
            model_id=probe_model["id"],
            task=task,
            features=default_features,
        )
        evidence = collect_execution_evidence(
            raw,
            model_id=probe_model["id"],
            task=task,
            must_contain=must_contain,
            must_not_contain=must_not_contain,
        )
        evidence["base_model_id"] = base_model_id
        evidence["probe_version_id"] = probe_model.get("version_id")
        return evidence
    finally:
        if not keep_probe:
            try:
                client.delete_model(probe_model_id)
            except Exception:
                pass


def system_prompt_diff(old_system: str | None, new_system: str | None) -> str:
    old_lines = (old_system or "").splitlines(keepends=True)
    new_lines = (new_system or "").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile="current/system", tofile="candidate/system")
    )


def resolve_harness(harness_dir: str | None, project: str | None) -> dict[str, str]:
    if harness_dir:
        return load_harness_from_dir(Path(os.path.expanduser(harness_dir)))
    if project:
        return load_harness_from_project_api(project)
    raise ValueError("Provide either --harness-dir or --project")


def cmd_evaluate(args: argparse.Namespace) -> None:
    client = OpenWebUIClient()
    harness = resolve_harness(args.harness_dir, args.project)
    evidence = evaluate_harness(
        client=client,
        task=args.task,
        candidate_harness=harness,
        base_model_id=args.base_model,
        probe_model_id=args.probe_model,
        must_contain=args.must_contain,
        must_not_contain=args.must_not_contain,
        keep_probe=args.keep_probe,
    )
    if args.out:
        out_path = Path(os.path.expanduser(args.out))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    if not evidence["passed"]:
        sys.exit(2)


def cmd_apply(args: argparse.Namespace) -> None:
    if args.target in PRODUCTION_MODEL_IDS and not args.allow_production:
        raise SystemExit(
            f"Refusing to modify production model {args.target!r} without --allow-production. "
            "Test on testworkspace-lab first!"
        )
    client = OpenWebUIClient()
    current = client.get_model(args.target)
    harness = resolve_harness(args.harness_dir, args.project)
    old_sys = (current.get("params") or {}).get("system") or ""
    payload = build_model_payload_with_harness(
        base_config=current,
        harness_files=harness,
        target_id=current["id"],
        target_name=current["name"],
    )
    new_sys = payload["params"]["system"]
    diff = system_prompt_diff(old_sys, new_sys)
    print("=== SYSTEM PROMPT DIFF ===")
    print(diff if diff else "(no changes)")
    if not args.approve:
        print("\n[DRY RUN] Re-run with --approve to apply this update to Open WebUI.")
        return
    before_version = current.get("version_id")
    updated = client.update_model(args.target, payload)
    print(
        json.dumps(
            {
                "applied": True,
                "target": args.target,
                "previous_version_id": before_version,
                "new_version_id": updated.get("version_id"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_history(args: argparse.Namespace) -> None:
    client = OpenWebUIClient()
    current = client.get_model(args.target)
    hist = client.list_history(args.target)
    print(
        json.dumps(
            {
                "model_id": args.target,
                "current_version_id": current.get("version_id"),
                "history": hist,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_rollback(args: argparse.Namespace) -> None:
    if args.target in PRODUCTION_MODEL_IDS and not args.allow_production:
        raise SystemExit(f"Refusing to rollback production model {args.target!r} without --allow-production.")
    client = OpenWebUIClient()
    res = client.rollback_model(args.target, args.version_id)
    after = client.get_model(args.target)
    print(
        json.dumps(
            {
                "rolled_back": True,
                "target": args.target,
                "requested_version_id": args.version_id,
                "current_version_id": after.get("version_id"),
                "api_response_version_id": res.get("version_id") if isinstance(res, dict) else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_regress(args: argparse.Namespace) -> None:
    client = OpenWebUIClient()
    suite = json.loads(Path(os.path.expanduser(args.suite)).read_text(encoding="utf-8"))
    results = []
    for case in suite:
        raw = client.run_chat_completion(
            model_id=args.target,
            task=case["task"],
            features=case.get("features") or {"memory": True, "code_interpreter": True},
        )
        ev = collect_execution_evidence(
            raw,
            model_id=args.target,
            task=case["task"],
            must_contain=case.get("must_contain"),
            must_not_contain=case.get("must_not_contain"),
        )
        ev["case_id"] = case.get("id")
        results.append(ev)
    summary = {
        "model_id": args.target,
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "all_passed": all(r["passed"] for r in results),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["all_passed"]:
        sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Open WebUI Executor Adapter for Harnessmith (Stage 5)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_eval = sub.add_parser("evaluate", help="Probe a candidate harness via isolated Open WebUI model")
    p_eval.add_argument("--task", required=True)
    p_eval.add_argument("--harness-dir")
    p_eval.add_argument("--project", default="agent-harness-lab")
    p_eval.add_argument("--base-model", default="testworkspace")
    p_eval.add_argument("--probe-model", default="testworkspace-probe")
    p_eval.add_argument("--must-contain", action="append", default=[])
    p_eval.add_argument("--must-not-contain", action="append", default=[])
    p_eval.add_argument("--keep-probe", action="store_true")
    p_eval.add_argument("--out")
    p_eval.set_defaults(func=cmd_evaluate)

    p_apply = sub.add_parser("apply", help="Preview diff or apply a Harnessmith harness to an Open WebUI model")
    p_apply.add_argument("--target", default="testworkspace-lab")
    p_apply.add_argument("--harness-dir")
    p_apply.add_argument("--project", default="agent-harness-lab")
    p_apply.add_argument("--approve", action="store_true")
    p_apply.add_argument("--allow-production", action="store_true")
    p_apply.set_defaults(func=cmd_apply)

    p_hist = sub.add_parser("history", help="Show Open WebUI model version history")
    p_hist.add_argument("--target", default="testworkspace-lab")
    p_hist.set_defaults(func=cmd_history)

    p_rb = sub.add_parser("rollback", help="Rollback an Open WebUI model to a previous version_id")
    p_rb.add_argument("--target", default="testworkspace-lab")
    p_rb.add_argument("--version-id", required=True)
    p_rb.add_argument("--allow-production", action="store_true")
    p_rb.set_defaults(func=cmd_rollback)

    p_reg = sub.add_parser("regress", help="Run regression test suite against an Open WebUI model")
    p_reg.add_argument("--target", default="testworkspace-lab")
    p_reg.add_argument("--suite", default=str(Path(__file__).resolve().parent / "regression_suite.json"))
    p_reg.set_defaults(func=cmd_regress)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
