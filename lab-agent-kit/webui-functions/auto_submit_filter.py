"""
title: Auto Submit to Harnessmith
author: local
version: 0.2.0
requirements: httpx
"""

from typing import Optional
import urllib.parse
import httpx
from pydantic import BaseModel


class Filter:
    class Valves(BaseModel):
        priority: int = 0
        harness_url: str = "http://127.0.0.1:8765"
        hook_token: str = ""
        project: str = "agent-harness-lab"
        k: int = 1
        mode: str = "parallel"
        min_chars: int = 1
        auto_sync: bool = True
        autosync_url: str = "http://127.0.0.1:8766/callback"
        autosync_token: str = ""
        sync_targets: str = "testworkspace-lab,testworkspace"
        notify_user: bool = True

    def __init__(self):
        self.valves = self.Valves()

    def _extract_last_user_text(self, messages: list) -> Optional[str]:
        for m in reversed(messages):
            if m.get("role") != "user":
                continue
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = [
                    item.get("text", "").strip()
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
                ]
                joined = "\n".join(p for p in parts if p)
                if joined:
                    return joined
        return None

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> dict:
        meta = body.get("metadata") or {}
        model_id = str(body.get("model") or "")

        # Prevent infinite recursion from Adapter probes or Open WebUI internal tasks
        if meta.get("source") == "harness2-adapter" or model_id.endswith("-probe"):
            return body
        if meta.get("task"):
            return body

        goal = self._extract_last_user_text(body.get("messages") or [])
        if not goal or goal.startswith("### Task:"):
            return body
        if len(goal) < max(1, int(self.valves.min_chars)):
            return body
        if not self.valves.hook_token:
            return body

        payload = {
            "project": self.valves.project,
            "goal": goal,
            "k": self.valves.k,
            "mode": self.valves.mode,
        }
        if self.valves.auto_sync and self.valves.autosync_url and self.valves.autosync_token:
            qs = urllib.parse.urlencode(
                {
                    "token": self.valves.autosync_token,
                    "targets": self.valves.sync_targets,
                }
            )
            payload["reply_url"] = f"{self.valves.autosync_url}?{qs}"

        url = self.valves.harness_url.rstrip("/") + "/api/hooks/goal"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.valves.hook_token}"},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
            if self.valves.notify_user and __event_emitter__:
                run_id = data.get("run_id", "?")
                sync_hint = "（跑完驗證通過將自動回寫）" if self.valves.auto_sync else ""
                await __event_emitter__(
                    {
                        "type": "notification",
                        "data": {
                            "type": "info",
                            "content": f"[Harnessmith] 已自動送入改善佇列 (run_id={run_id}){sync_hint}",
                        },
                    }
                )
        except Exception as e:
            if self.valves.notify_user and __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "notification",
                        "data": {
                            "type": "warning",
                            "content": f"[Harnessmith] 自動提交失敗（不影響對話）: {e}",
                        },
                    }
                )
        return body
