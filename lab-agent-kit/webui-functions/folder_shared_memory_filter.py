"""
title: Folder Shared Memory (Project Cross-Chat Context)
author: local
version: 0.1.0
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Optional
from pydantic import BaseModel


MEM_BEGIN = "<!-- FOLDER_SHARED_MEMORY:BEGIN -->"
MEM_END = "<!-- FOLDER_SHARED_MEMORY:END -->"


class Filter:
    class Valves(BaseModel):
        priority: int = -10
        db_path: str = "/app/backend/data/webui.db"
        snapshot_dir: str = "/app/backend/data/folder_memories"
        max_sibling_chats: int = 6
        max_turns_per_chat: int = 4
        max_chars_per_message: int = 260
        max_total_chars: int = 4200
        save_markdown_snapshot: bool = True

    def __init__(self):
        self.valves = self.Valves()

    def _extract_text(self, content) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                item.get("text", "").strip()
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
            ]
            return "\n".join(p for p in parts if p).strip()
        return ""

    def _extract_branch_messages(self, chat_json_str: str) -> list[tuple[str, str]]:
        try:
            data = json.loads(chat_json_str)
        except Exception:
            return []

        ordered: list[dict] = []
        history = data.get("history") or {}
        hist_msgs = history.get("messages") or {}
        cur_id = history.get("currentId")
        if isinstance(hist_msgs, dict) and cur_id and cur_id in hist_msgs:
            seen = set()
            node_id = cur_id
            chain = []
            while node_id and node_id not in seen and node_id in hist_msgs:
                seen.add(node_id)
                node = hist_msgs[node_id]
                if isinstance(node, dict):
                    chain.append(node)
                    node_id = node.get("parentId")
                else:
                    break
            ordered = list(reversed(chain))
        else:
            raw_msgs = data.get("messages") or []
            if isinstance(raw_msgs, list):
                ordered = [m for m in raw_msgs if isinstance(m, dict)]

        pairs: list[tuple[str, str]] = []
        last_sig = None
        for m in ordered:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            txt = self._extract_text(m.get("content"))
            if not txt:
                continue
            # Collapse excessive whitespace for compact memory injection
            compact = re.sub(r"\s+", " ", txt).strip()
            sig = (role, compact[:120])
            if sig == last_sig:
                continue
            last_sig = sig
            if len(compact) > self.valves.max_chars_per_message:
                compact = compact[: self.valves.max_chars_per_message] + "…"
            pairs.append((role, compact))

        return pairs[-self.valves.max_turns_per_chat :]

    def _load_folder_context(
        self,
        chat_id: Optional[str],
        user_id: Optional[str],
        explicit_folder_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], str]:
        db_file = self.valves.db_path
        if not db_file or not os.path.exists(db_file):
            return None, None, ""

        uri = f"file:{db_file}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            cur = conn.cursor()
            folder_id = explicit_folder_id
            owner_id = user_id

            if chat_id:
                row = cur.execute(
                    "SELECT folder_id, user_id FROM chat WHERE id = ?",
                    (chat_id,),
                ).fetchone()
                if row:
                    folder_id = folder_id or row[0]
                    owner_id = owner_id or row[1]

            if not folder_id or not owner_id:
                return None, None, ""

            frow = cur.execute("SELECT name FROM folder WHERE id = ?", (folder_id,)).fetchone()
            folder_name = frow[0] if frow and frow[0] else folder_id

            rows = cur.execute(
                """
                SELECT id, title, updated_at, chat
                FROM chat
                WHERE user_id = ?
                  AND folder_id = ?
                  AND (archived IS NULL OR archived = 0)
                  AND id != ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (owner_id, folder_id, chat_id or "", int(self.valves.max_sibling_chats)),
            ).fetchall()

            if not rows:
                return folder_id, folder_name, ""

            blocks = [
                f"[同專案資料夾「{folder_name}」跨對話共享記憶]",
                "以下是同一個專案資料夾內、其他最近對話的真實紀錄（由新到舊）。",
                "當使用者詢問先前在其他對話做過什麼、要求寫過什麼功能或程式碼時，請直接根據以下紀錄準確回答，不可說不記得或捏造：",
                "",
            ]
            total_len = sum(len(x) for x in blocks)

            for idx, (_cid, title, updated_at, chat_str) in enumerate(rows, start=1):
                turns = self._extract_branch_messages(chat_str or "")
                if not turns:
                    continue
                ts_str = ""
                if updated_at:
                    try:
                        ts_str = datetime.fromtimestamp(int(updated_at)).strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        ts_str = str(updated_at)
                header = f"### 對話 {idx}：《{title or 未命名對話}》" + (f"（{ts_str}）" if ts_str else "")
                lines = [header]
                for role, txt in turns:
                    label = "使用者" if role == "user" else "助理"
                    lines.append(f"- {label}：{txt}")
                lines.append("")
                chunk = "\n".join(lines)
                if total_len + len(chunk) > int(self.valves.max_total_chars):
                    break
                blocks.append(chunk)
                total_len += len(chunk)

            if len(blocks) <= 4:
                return folder_id, folder_name, ""

            ctx = "\n".join(blocks).strip()

            if self.valves.save_markdown_snapshot and self.valves.snapshot_dir:
                try:
                    snap_dir = Path(self.valves.snapshot_dir)
                    snap_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", folder_name).strip("_") or "folder"
                    snap_file = snap_dir / f"{safe_name}_{folder_id[:8]}.md"
                    snap_file.write_text(ctx + "\n", encoding="utf-8")
                except Exception:
                    pass

            return folder_id, folder_name, ctx
        finally:
            conn.close()

    def _inject_system_memory(self, messages: list, mem_text: str) -> list:
        wrapped = f"{MEM_BEGIN}\n{mem_text}\n{MEM_END}"
        pattern = re.compile(re.escape(MEM_BEGIN) + r".*?" + re.escape(MEM_END), re.DOTALL)

        for m in messages:
            if m.get("role") == "system" and isinstance(m.get("content"), str):
                existing = m["content"]
                if pattern.search(existing):
                    m["content"] = pattern.sub(wrapped, existing)
                else:
                    m["content"] = f"{existing.rstrip()}\n\n{wrapped}" if existing.strip() else wrapped
                return messages

        return [{"role": "system", "content": wrapped}] + messages

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        meta = {}
        if isinstance(__metadata__, dict):
            meta.update(__metadata__)
        if isinstance(body.get("metadata"), dict):
            meta.update(body["metadata"])

        # Skip internal tasks & adapter probes
        if meta.get("task") or meta.get("source") == "harness2-adapter":
            return body
        msgs = body.get("messages") or []
        if msgs:
            last_txt = self._extract_text(msgs[-1].get("content"))
            if last_txt.startswith("### Task:"):
                return body

        chat_id = meta.get("chat_id") or body.get("chat_id")
        folder_id = meta.get("folder_id") or body.get("folder_id")
        user_id = (__user__ or {}).get("id") or meta.get("user_id")

        try:
            _fid, _fname, ctx = self._load_folder_context(chat_id, user_id, explicit_folder_id=folder_id)
            print(f"[folder_shared_memory] chat_id={chat_id!r} folder_id={_fid!r} folder={_fname!r} user_id={user_id!r} ctx_len={len(ctx)} meta_keys={list(meta.keys())}", flush=True)
            if ctx:
                body["messages"] = self._inject_system_memory(list(msgs), ctx)
        except Exception as e:
            print(f"[folder_shared_memory] ERROR: {e!r}", flush=True)
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body
