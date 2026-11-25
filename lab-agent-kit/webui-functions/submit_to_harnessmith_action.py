"""
title: Submit to Harnessmith
author: local
version: 0.1.0
requirements: httpx
"""

import httpx
from pydantic import BaseModel


class Action:
    class Valves(BaseModel):
        harness_url: str = "http://127.0.0.1:8765"
        hook_token: str = ""
        project: str = "agent-harness-lab"
        k: int = 1
        mode: str = "parallel"

    def __init__(self):
        self.valves = self.Valves()

    async def action(
        self,
        body: dict,
        __user__=None,
        __event_emitter__=None,
    ):
        messages = body.get("messages", [])
        goal = next(
            (
                m.get("content")
                for m in reversed(messages)
                if m.get("role") == "user" and isinstance(m.get("content"), str) and m.get("content").strip()
            ),
            None,
        )
        if not goal:
            raise ValueError("No text goal found")
        if not self.valves.hook_token:
            raise ValueError("Missing webhook token (set Valves.hook_token)")

        url = self.valves.harness_url.rstrip("/") + "/api/hooks/goal"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.valves.hook_token}"},
                    json={
                        "project": self.valves.project,
                        "goal": goal,
                        "k": self.valves.k,
                        "mode": self.valves.mode,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except Exception as e:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "notification",
                        "data": {"type": "error", "content": f"Harnessmith submission failed: {e}"},
                    }
                )
            raise

        run_id = data.get("run_id", "?")
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "notification",
                    "data": {
                        "type": "info",
                        "content": f"Goal submitted to Harnessmith (run_id={run_id}). Check http://127.0.0.1:8765 History.",
                    },
                }
            )
        return data
