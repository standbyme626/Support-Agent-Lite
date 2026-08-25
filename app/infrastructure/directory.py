"""Enterprise directory & asset registry (C9).

Loads fictitious seed data (30 employees / 30 assets) into memory —
same pattern as the Retriever's seed loading. These are L4 precise
entities: they are NEVER embedded or vector-searched; lookup is exact
or substring matching, with role-aware PII masking on output.
"""
from __future__ import annotations

import json
from pathlib import Path

_PHONE_MASK = lambda p: p[:3] + "****" + p[-4:] if len(p) >= 7 else p


class DirectoryService:
    def __init__(self, seed_dir: str | Path) -> None:
        seed_dir = Path(seed_dir)
        self.employees: list[dict] = self._load(seed_dir / "employees.json")
        self.assets: list[dict] = self._load(seed_dir / "assets.json")
        self._emp_by_id = {e["employee_id"]: e for e in self.employees}

    @staticmethod
    def _load(path: Path) -> list[dict]:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data["items"])

    # --- contacts -----------------------------------------------------------

    def lookup_contact(self, query: str, *, viewer_role: str) -> str:
        q = (query or "").strip()
        if not q:
            return "查询为空"
        matches = [
            e for e in self.employees
            if e["employee_id"] == q
            or e["name"] == q
            or e["extension"] == q
            or q in e["name"]
            or e["dept"] == q
        ]
        if not matches:
            return f"未找到与「{q}」相关的联系人"
        lines = []
        for e in matches[:8]:
            line = (
                f"{e['employee_id']} {e['name']}（{e['dept']}） 分机 {e['extension']}"
                f" 邮箱 {e['email']} 工位 {e['location']}"
            )
            if viewer_role in ("operator", "approver"):
                line += f" 手机 {_PHONE_MASK(e['phone'])}"
            else:
                line += " 手机 138****（仅运维可见）"
            lines.append(line)
        return "\n".join(lines)

    # --- assets ---------------------------------------------------------------

    def lookup_asset(self, query: str, *, viewer_role: str = "requester") -> str:
        q = (query or "").strip()
        if not q:
            return "查询为空"
        matches = []
        for a in self.assets:
            emp = self._emp_by_id.get(a.get("assigned_to") or "", {})
            if (
                a["asset_id"] == q
                or a["type"] == q
                or a["model"] == q
                or q in a["type"]
                or q in a["model"]
                or emp.get("name") == q
            ):
                matches.append((a, emp))
        if not matches:
            return f"未找到与「{q}」相关的资产"
        lines = []
        for a, emp in matches[:8]:
            owner = f"{emp.get('name', '-')}（{a.get('assigned_to', '-')}）"
            lines.append(
                f"{a['asset_id']} {a['type']} {a['model']} 领用人 {owner}"
                f" 状态 {a['status']} 购入 {a.get('purchase_date', '-')}"
            )
        return "\n".join(lines)
