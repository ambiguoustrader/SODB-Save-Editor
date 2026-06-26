#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shadows of Doubt Save Editor / Viewer
------------------------------------
Минимальный GUI-редактор .sodb сохранений:
- открывает сжатые Brotli .sodb и несжатые JSON-сейвы;
- показывает кейсы, убийц/криминалов, людей, связи, raw JSON;
- позволяет менять деньги, отмычки, соц. кредит и несколько статов игрока;
- есть очистка состояния, сброс правок и встроенная справка по диапазонам;
- вкладка паролей показывает найденные/сохранённые passcodes и умеет добавлять личный пароль человеку;
- умеет экспортировать JSON и сохранять обратно .sodb;
- v6: валидатор, автобэкапы, полные карточки людей/кейсов, поиск предметов, адресная книга RoomID → address.

Зависимости: Python 3.10+, tkinter, brotli.
Установка brotli:  python -m pip install brotli
"""

from __future__ import annotations

import copy
import csv
import json
import math
import os
import re
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import brotli  # type: ignore
except Exception:  # pragma: no cover
    brotli = None

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from tkinter import ttk

APP_TITLE = "Shadows of Doubt — SODB Save Editor"
HUMAN_RE = re.compile(r"^Human(\d+)$", re.IGNORECASE)
HUMAN_ANY_RE = re.compile(r"Human(\d+)", re.IGNORECASE)
ROLE_SEP = " — "
JSON_PREVIEW_LIMIT = 1_500_000
APP_ICON_FILE = "icon.ico"


def app_resource_path(relative_path: str) -> Path:
    """Return a path that works both from source and from a PyInstaller one-file EXE."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative_path
    return Path(__file__).resolve().parent / relative_path


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------

@dataclass
class DecodeResult:
    obj: Any
    raw_json: str
    compressed: bool
    codec_note: str
    expected_size: Optional[int] = None


class SodbCodec:
    @staticmethod
    def decode(path: Path) -> DecodeResult:
        data = path.read_bytes()

        # 1) Несжатый JSON, если в игре отключили Save Game Compression.
        try:
            raw = data.decode("utf-8-sig")
            obj = json.loads(raw)
            return DecodeResult(obj=obj, raw_json=raw, compressed=False, codec_note="plain JSON")
        except Exception:
            pass

        if brotli is None:
            raise RuntimeError(
                "Модуль brotli не установлен. Выполни: python -m pip install brotli"
            )

        errors: List[str] = []
        attempts: List[Tuple[str, bytes, Optional[int]]] = []
        if len(data) > 4:
            expected = int.from_bytes(data[-4:], "little", signed=False)
            attempts.append(("brotli + 4-byte size trailer", data[:-4], expected))
        attempts.append(("brotli whole file", data, None))

        for note, payload, expected in attempts:
            try:
                decoded = brotli.decompress(payload)
                if expected is not None and expected != len(decoded):
                    # Не фейлим жёстко: некоторые версии/моды могут писать иначе.
                    note += f"; warning: expected {expected}, decoded {len(decoded)}"
                raw = decoded.decode("utf-8-sig")
                obj = json.loads(raw)
                return DecodeResult(obj=obj, raw_json=raw, compressed=True, codec_note=note, expected_size=expected)
            except Exception as e:
                errors.append(f"{note}: {e}")

        raise RuntimeError("Не удалось распаковать файл. Попытки:\n" + "\n".join(errors))

    @staticmethod
    def encode(obj: Any, compressed: bool = True, pretty: bool = False) -> bytes:
        if pretty:
            raw = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        else:
            raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if not compressed:
            return raw
        if brotli is None:
            raise RuntimeError("Модуль brotli не установлен. Выполни: python -m pip install brotli")
        packed = brotli.compress(raw, quality=5)
        return packed + len(raw).to_bytes(4, "little", signed=False)


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

@dataclass
class PersonInfo:
    cid: int
    name: str = ""
    role: str = ""
    flags: List[str] = field(default_factory=list)
    position: str = ""
    goal: str = ""
    location_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    seen_ids: List[int] = field(default_factory=list)
    ties: List[str] = field(default_factory=list)


@dataclass
class CaseInfo:
    cid: int
    name: str
    case_type: str
    status: str
    solved: bool
    active: bool
    job_id: int
    target_id: Optional[int]
    target_name: str
    reward: str
    preset: str
    short: str
    raw: Dict[str, Any]




@dataclass
class PasswordInfo:
    index: int
    code: str
    pc_type: int
    target_kind: str
    target_id: int
    target_name: str
    owner_hint: str
    used: bool
    known: bool
    notes: str
    raw: Dict[str, Any]


@dataclass
class CompanyInfo:
    cid: int
    roster_id: str
    workers: List[Tuple[int, str, str]]
    sales_count: int
    sales_total: float
    roles: str
    workers_sample: str
    raw_company: Dict[str, Any]
    raw_evidence: Dict[str, Any]


@dataclass
class SearchResultInfo:
    section: str
    rid: str
    title: str
    owner: str
    location: str
    snippet: str
    raw: Dict[str, Any]


@dataclass
class RelationInfo:
    src: int
    dst: int
    kind: str
    detail: str


class SodbAnalyzer:
    JOB_SECTIONS = [
        "basicJobs", "affairJobs", "sabotageJobs", "stolenItemJobs", "missingPersonJobs",
        "revengeJobs", "briefcaseJobs",
    ]

    CASE_TYPE = {
        0: "Unknown/Story",
        1: "Murder / Main case",
        2: "Side job",
        3: "Notebook / custom",
    }

    CASE_STATUS = {
        0: "New/Inactive",
        1: "Active",
        2: "Submitted?",
        3: "Closed/Archived?",
    }

    def __init__(self, obj: Dict[str, Any]):
        self.obj = obj
        self.evidence_by_id: Dict[str, Dict[str, Any]] = {
            str(e.get("id")): e for e in obj.get("evidence", []) if isinstance(e, dict) and e.get("id") is not None
        }
        self.room_by_id: Dict[int, Dict[str, Any]] = {
            r.get("id"): r for r in obj.get("rooms", []) if isinstance(r, dict) and isinstance(r.get("id"), int)
        }
        self.address_by_id: Dict[int, Dict[str, Any]] = {
            a.get("id"): a for a in obj.get("addresses", []) if isinstance(a, dict) and isinstance(a.get("id"), int)
        }
        self.name_map: Dict[int, str] = {}
        self.role_map: Dict[int, str] = {}
        self.roster_source: Dict[int, str] = {}
        self._build_people_names()
        self.job_by_id = self._build_jobs()
        self.room_address_map: Dict[int, int] = self._build_room_address_map()

    def human_name(self, cid: Any) -> str:
        try:
            i = int(cid)
        except Exception:
            return str(cid)
        name = self.name_map.get(i, "")
        return "—" if i <= 0 else (f"{name} (Human{i})" if name else f"Human{i}")

    @staticmethod
    def _fmt_pos(pos: Any) -> str:
        if isinstance(pos, dict) and all(isinstance(pos.get(k), (int, float)) for k in ("x", "y", "z")):
            return f"x={pos.get('x'):.1f}, y={pos.get('y'):.1f}, z={pos.get('z'):.1f}"
        return ""

    def address_label(self, aid: Any) -> str:
        if not isinstance(aid, int) or aid < 0:
            return "—"
        a = self.address_by_id.get(aid)
        if not a:
            return f"Address{aid}"
        extra = []
        for key in ("name", "n", "preset", "residence", "buildingID", "building", "floor"):
            if a.get(key) not in (None, "", -1):
                extra.append(f"{key}={a.get(key)}")
        return f"Address{aid}" + (" (" + ", ".join(extra[:4]) + ")" if extra else "")

    def _build_room_address_map(self) -> Dict[int, int]:
        out: Dict[int, int] = {}

        def remember(room_id: Any, address_id: Any) -> None:
            if isinstance(room_id, int) and room_id >= 0 and isinstance(address_id, int) and address_id >= 0:
                out.setdefault(room_id, address_id)

        # Самый надёжный источник: текущие AI goals/actions, где рядом есть room и gameLocation/isAddress.
        for c in self.obj.get("citizens", []) or []:
            if not isinstance(c, dict):
                continue
            goal = c.get("currentGoal") if isinstance(c.get("currentGoal"), dict) else {}
            if goal.get("isAddress") and isinstance(goal.get("gameLocation"), int):
                remember(goal.get("room"), goal.get("gameLocation"))
                for act in goal.get("actions", []) or []:
                    if isinstance(act, dict):
                        remember(act.get("passedRoom"), goal.get("gameLocation"))
        # Jobs can point to a secret location/address and sometimes to a room/furniture node.
        for job in self.job_by_id.values():
            addr = job.get("secretLocation") or job.get("secretLocationAddress") or job.get("handIn")
            room = job.get("secretLocationRoom") or job.get("secretLocationNode")
            remember(room, addr)
        return out

    def room_label(self, rid: Any) -> str:
        if not isinstance(rid, int) or rid < 0:
            return "—"
        room = self.room_by_id.get(rid)
        if not room:
            return f"Room{rid}"
        bits = [f"Room{rid}"]
        addr = self.room_address_map.get(rid)
        if addr is not None:
            bits.append(self.address_label(addr))
        if room.get("fID") is not None:
            bits.append(f"fID={room.get('fID')}")
        if room.get("iID") is not None:
            bits.append(f"iID={room.get('iID')}")
        if room.get("ml") is not None:
            bits.append(f"main={room.get('ml')}")
        return bits[0] + (" (" + ", ".join(bits[1:]) + ")" if len(bits) > 1 else "")

    def location_label(self, loc_id: Any, room_id: Any = None) -> str:
        bits = []
        if isinstance(loc_id, int) and loc_id >= 0:
            bits.append(self.address_label(loc_id))
        elif loc_id not in (None, "", -1, "-1"):
            bits.append(str(loc_id))
        if isinstance(room_id, int) and room_id >= 0:
            bits.append(self.room_label(room_id))
        return " / ".join(bits) if bits else "—"

    def _split_name_role(self, text: str) -> Tuple[str, str]:
        text = (text or "").strip()
        if ROLE_SEP in text:
            left, right = text.split(ROLE_SEP, 1)
            return left.strip(), right.strip()
        # В некоторых локалях/шрифтах может быть обычный дефис.
        if " - " in text:
            left, right = text.split(" - ", 1)
            return left.strip(), right.strip()
        return text, ""

    def _build_people_names(self) -> None:
        # Главный источник имён — ResidentRoster*/CompanyRoster*: строка с именем,
        # затем mpContent с discEvID=HumanID на той же странице.
        for evid, ev in self.evidence_by_id.items():
            content = ev.get("mpContent") or []
            if not isinstance(content, list):
                continue
            by_page: Dict[Any, List[Dict[str, Any]]] = {}
            for item in content:
                if isinstance(item, dict):
                    by_page.setdefault(item.get("page", 0), []).append(item)
            for page, items in by_page.items():
                last_text = ""
                for item in sorted(items, key=lambda x: x.get("order", -1)):
                    text = str(item.get("str") or "").strip()
                    disc = str(item.get("discEvID") or "")
                    m = HUMAN_RE.match(disc)
                    if m and last_text:
                        cid = int(m.group(1))
                        name, role = self._split_name_role(last_text)
                        if name and (cid not in self.name_map or evid.startswith("CompanyRoster")):
                            self.name_map[cid] = name
                            self.roster_source[cid] = evid
                        if role:
                            self.role_map[cid] = role
                    if text:
                        # Birthday lines sometimes contain a name too; less reliable, use only as fallback.
                        bday = re.match(r"\d+(?:st|nd|rd|th)?\s+[—-]\s+(.+?)\s+birthday", text, re.I)
                        last_text = bday.group(1).strip() if bday else text

        # Fallback: если есть caseElements вида "Thomas Muller(Human229)".
        for case in self.obj.get("activeCases", []) + self.obj.get("archivedCases", []):
            if not isinstance(case, dict):
                continue
            for el in case.get("caseElements", []) or []:
                if not isinstance(el, dict):
                    continue
                hid = str(el.get("id") or "")
                m = HUMAN_RE.match(hid)
                if not m:
                    continue
                cid = int(m.group(1))
                n = str(el.get("n") or "")
                n = re.sub(r"\(Human\d+\)$", "", n).strip()
                if n and cid not in self.name_map:
                    self.name_map[cid] = n

    def _build_jobs(self) -> Dict[int, Dict[str, Any]]:
        jobs = {}
        for section in self.JOB_SECTIONS:
            for j in self.obj.get(section, []) or []:
                if isinstance(j, dict):
                    jid = j.get("jobID", j.get("id"))
                    if isinstance(jid, int):
                        j = dict(j)
                        j["__section"] = section
                        jobs[jid] = j
        return jobs

    def case_infos(self) -> List[CaseInfo]:
        out: List[CaseInfo] = []
        cases = list(self.obj.get("activeCases", []) or []) + list(self.obj.get("archivedCases", []) or [])
        for c in cases:
            if not isinstance(c, dict):
                continue
            cid = int(c.get("id", -1)) if str(c.get("id", "")).lstrip("-").isdigit() else -1
            job_id = c.get("jobReference", -1)
            job = self.job_by_id.get(job_id, {}) if isinstance(job_id, int) else {}
            target_id = job.get("purpID") if isinstance(job.get("purpID"), int) else None
            target_name = self.human_name(target_id) if target_id is not None else ""
            reward = str(job.get("reward", "")) if job else ""
            preset = str(job.get("presetStr", "")) if job else ""
            qlines = []
            for q in c.get("resolveQuestions", []) or []:
                if isinstance(q, dict):
                    ans = q.get("correctAnswers") or q.get("automaticAnswers") or []
                    qlines.append(f"{q.get('name', '')} => {ans or q.get('input', '')}")
            short_parts = []
            if target_name:
                short_parts.append(f"target: {target_name}")
            if preset:
                short_parts.append(preset)
            if qlines:
                short_parts.append("; ".join(qlines[:2]))
            out.append(CaseInfo(
                cid=cid,
                name=str(c.get("name", "")),
                case_type=self.CASE_TYPE.get(c.get("caseType"), str(c.get("caseType"))),
                status=self.CASE_STATUS.get(c.get("caseStatus"), str(c.get("caseStatus"))),
                solved=bool(c.get("isSolved")),
                active=bool(c.get("isActive")),
                job_id=job_id if isinstance(job_id, int) else -1,
                target_id=target_id,
                target_name=target_name,
                reward=reward,
                preset=preset,
                short=" | ".join(short_parts),
                raw=c,
            ))
        return out

    def person_infos(self) -> List[PersonInfo]:
        current_murderer = self.obj.get("currentMurderer")
        current_victim = self.obj.get("currentVictim")
        previous = set(x for x in self.obj.get("previousMurderers", []) if isinstance(x, int))
        murderers = {m.get("murdererID") for m in self.obj.get("iaMurders", []) if isinstance(m, dict)}
        victims = {m.get("victimID") for m in self.obj.get("iaMurders", []) if isinstance(m, dict)}
        job_targets = {j.get("purpID") for j in self.job_by_id.values() if isinstance(j.get("purpID"), int)}

        out: List[PersonInfo] = []
        for c in self.obj.get("citizens", []) or []:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if not isinstance(cid, int):
                continue
            flags: List[str] = []
            death = c.get("death") if isinstance(c.get("death"), dict) else {}
            if cid == current_murderer:
                flags.append("CURRENT_MURDERER")
            if cid in murderers:
                flags.append("MURDERER_IN_IA")
            if cid in previous:
                flags.append("PREVIOUS_MURDERER")
            if cid == current_victim:
                flags.append("CURRENT_VICTIM")
            if cid in victims:
                flags.append("VICTIM_IN_IA")
            if c.get("convicted"):
                flags.append("CONVICTED")
            if death.get("isDead"):
                flags.append("DEAD")
            if isinstance(death.get("killer"), int) and death.get("killer"):
                flags.append(f"KILLED_BY_{death.get('killer')}")
            if isinstance(c.get("kidnapper"), int) and c.get("kidnapper", -1) >= 0:
                flags.append(f"KIDNAPPER_{c.get('kidnapper')}")
            if isinstance(c.get("poisoner"), int) and c.get("poisoner", -1) >= 0:
                flags.append(f"POISONER_{c.get('poisoner')}")
            if cid in job_targets:
                flags.append("JOB_TARGET")

            pos = c.get("pos") if isinstance(c.get("pos"), dict) else {}
            position = f"x={pos.get('x','?'):.1f}, y={pos.get('y','?'):.1f}, z={pos.get('z','?'):.1f}" if all(isinstance(pos.get(k), (int, float)) for k in ("x", "y", "z")) else ""
            goal = c.get("currentGoal") if isinstance(c.get("currentGoal"), dict) else {}
            goal_name = str(goal.get("preset", ""))
            loc_id = str(goal.get("gameLocation", ""))
            seen = [x for x in c.get("sightingCit", []) or [] if isinstance(x, int)]

            ties = []
            ev = self.evidence_by_id.get(f"Human{cid}", {})
            for kt in ev.get("keyTies", []) or []:
                if isinstance(kt, dict) and kt.get("tied"):
                    ties.append(f"key {kt.get('key')}: {kt.get('tied')}")
                    if len(ties) >= 8:
                        break

            out.append(PersonInfo(
                cid=cid,
                name=self.name_map.get(cid, ""),
                role=self.role_map.get(cid, ""),
                flags=flags,
                position=position,
                goal=goal_name,
                location_id=loc_id,
                raw=c,
                seen_ids=seen[:250],
                ties=ties,
            ))
        return sorted(out, key=lambda p: (p.name.lower() if p.name else "zzzz", p.cid))

    def criminals(self) -> List[Tuple[int, str, str, str]]:
        people = {p.cid: p for p in self.person_infos()}
        rows: Dict[Tuple[int, str], Tuple[int, str, str, str]] = {}

        def add(cid: Any, kind: str, details: str = ""):
            if not isinstance(cid, int) or cid <= 0:
                return
            p = people.get(cid)
            name = p.name if p and p.name else f"Human{cid}"
            flags = ", ".join(p.flags) if p else ""
            rows[(cid, kind)] = (cid, name, kind, details or flags)

        add(self.obj.get("currentMurderer"), "current murderer", f"victim: {self.human_name(self.obj.get('currentVictim'))}")
        for cid in self.obj.get("previousMurderers", []) or []:
            add(cid, "previous murderer")
        for m in self.obj.get("iaMurders", []) or []:
            if not isinstance(m, dict):
                continue
            details = f"murderID={m.get('murderID')}, victim={self.human_name(m.get('victimID'))}, preset={m.get('presetStr')}, weapon={m.get('weaponStr')}, state={m.get('state')}"
            add(m.get("murdererID"), "iaMurder murderer", details)
        for c in self.obj.get("citizens", []) or []:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if c.get("convicted"):
                add(cid, "convicted")
            death = c.get("death") if isinstance(c.get("death"), dict) else {}
            killer = death.get("killer")
            if isinstance(killer, int) and killer:
                add(killer, "death.killer", f"killed {self.human_name(cid)}")
            for field_name in ("kidnapper", "poisoner"):
                val = c.get(field_name)
                if isinstance(val, int) and val >= 0:
                    add(val, field_name, f"target {self.human_name(cid)}")
        return sorted(rows.values(), key=lambda x: (x[2], x[0]))

    def _room_label(self, rid: int) -> str:
        return self.room_label(rid)

    def _known_password_keys(self) -> set:
        out = set()
        for item in self.obj.get("knowsPasswords", []) or []:
            if isinstance(item, int):
                out.add((None, item))
            elif isinstance(item, dict):
                t = item.get("type")
                i = item.get("id")
                if isinstance(i, int):
                    out.add((t if isinstance(t, int) else None, i))
        return out

    def personal_passcode_record(self, cid: int) -> Optional[Tuple[int, Dict[str, Any], str]]:
        """Return saved personal passcode for a HumanID, if it exists in top-level passcodes."""
        for idx, pc in enumerate(self.obj.get("passcodes", []) or []):
            if not isinstance(pc, dict):
                continue
            if pc.get("type") == 0 and pc.get("id") == cid:
                digits = pc.get("digits") or []
                code = "".join(str(x) for x in digits) if isinstance(digits, list) else str(digits)
                return idx, pc, code
        return None

    def password_status_for_human(self, cid: int) -> str:
        rec = self.personal_passcode_record(cid)
        if rec:
            idx, pc, code = rec
            return f"{code} (idx {idx}, used={bool(pc.get('used'))})"
        return "— not in passcodes"

    def add_or_update_personal_passcode(self, cid: int, code: str) -> Tuple[int, bool]:
        """Add/update a type=0 personal passcode. Returns (index, created)."""
        code = str(code).strip()
        if not re.fullmatch(r"\d{4}", code):
            raise ValueError("Код должен быть ровно 4 цифры, например 1234.")
        if not isinstance(cid, int) or cid <= 0:
            raise ValueError("HumanID должен быть положительным числом.")
        digits = [int(ch) for ch in code]
        if not isinstance(self.obj.get("passcodes"), list):
            self.obj["passcodes"] = []
        rec = self.personal_passcode_record(cid)
        if rec:
            idx, pc, _old = rec
            pc["digits"] = digits
            pc["type"] = 0
            pc["id"] = cid
            pc["used"] = True
            pc.setdefault("notes", [])
            return idx, False
        pc = {"digits": digits, "type": 0, "id": cid, "used": True, "notes": []}
        self.obj["passcodes"].append(pc)
        return len(self.obj["passcodes"]) - 1, True

    def password_infos(self) -> List[PasswordInfo]:
        passcodes = [p for p in self.obj.get("passcodes", []) or [] if isinstance(p, dict)]
        same_code_humans: Dict[str, List[int]] = {}
        for p in passcodes:
            if p.get("type") == 0 and isinstance(p.get("id"), int):
                code = "".join(str(x) for x in (p.get("digits") or []))
                if code:
                    same_code_humans.setdefault(code, []).append(p.get("id"))

        known = self._known_password_keys()
        rows: List[PasswordInfo] = []
        for idx, pc in enumerate(passcodes):
            digits = pc.get("digits") or []
            code = "".join(str(x) for x in digits) if isinstance(digits, list) else str(digits)
            pc_type = pc.get("type") if isinstance(pc.get("type"), int) else -1
            target_id = pc.get("id") if isinstance(pc.get("id"), int) else -1
            used = bool(pc.get("used"))
            is_known = used or (pc_type, target_id) in known or (None, target_id) in known
            notes_list = pc.get("notes") or []
            notes = ", ".join(str(x) for x in notes_list[:12])
            if isinstance(notes_list, list) and len(notes_list) > 12:
                notes += f" … +{len(notes_list) - 12}"

            if pc_type == 0:
                target_kind = "Human / personal"
                target_name = self.human_name(target_id)
                owner_hint = target_name
            elif pc_type == 1:
                target_kind = "Room / location"
                target_name = self._room_label(target_id)
                owners = [self.human_name(x) for x in same_code_humans.get(code, [])]
                owner_hint = ", ".join(owners) if owners else "—"
            else:
                target_kind = f"type={pc_type}"
                target_name = str(target_id)
                owners = [self.human_name(x) for x in same_code_humans.get(code, [])]
                owner_hint = ", ".join(owners) if owners else "—"

            rows.append(PasswordInfo(
                index=idx,
                code=code,
                pc_type=pc_type,
                target_kind=target_kind,
                target_id=target_id,
                target_name=target_name,
                owner_hint=owner_hint,
                used=used,
                known=is_known,
                notes=notes,
                raw=pc,
            ))
        return rows

    def pretty_password_details(self, info: PasswordInfo) -> str:
        lines = [
            f"Passcode #{info.index}",
            f"Code: {info.code}",
            f"Type: {info.pc_type} — {info.target_kind}",
            f"Target: {info.target_name}",
            f"Owner hint: {info.owner_hint}",
            f"used: {info.used}, known-ish: {info.known}",
            f"notes: {info.notes or '—'}",
        ]
        if info.pc_type == 0 and isinstance(info.target_id, int):
            person = next((p for p in self.person_infos() if p.cid == info.target_id), None)
            if person:
                lines += ["", "=== PERSON ===", f"Human{person.cid}: {person.name or '(unknown)'}", f"Role: {person.role or '—'}", f"Flags: {', '.join(person.flags) or '—'}", f"Position: {person.position or '—'}"]
        if info.pc_type == 1:
            same = [x for x in self.password_infos() if x.code == info.code and x.pc_type == 0]
            if same:
                lines += ["", "=== SAME CODE PERSONAL PASSWORDS ==="]
                for x in same:
                    lines.append(f"- {x.target_name}: {x.code}")
        lines += ["", "Raw passcode JSON:", json.dumps(info.raw, ensure_ascii=False, indent=2)]
        return "\n".join(lines)

    def dashboard_text(self) -> str:
        lines = []
        lines.append("=== SAVE SUMMARY ===")
        for k in ["build", "cityShare", "saveTime", "gameTime", "playerFirstName", "playerSurname", "money", "lockpicks", "socCredit"]:
            lines.append(f"{k}: {self.obj.get(k)}")
        lines.append("")
        lines.append("=== CURRENT MURDER ===")
        lines.append(f"currentMurderer: {self.human_name(self.obj.get('currentMurderer'))}")
        lines.append(f"currentVictim:   {self.human_name(self.obj.get('currentVictim'))}")
        lines.append(f"murderPreset:    {self.obj.get('murderPreset')}")
        lines.append(f"chosenMO:        {self.obj.get('chosenMO')}")
        lines.append("")
        for m in self.obj.get("iaMurders", []) or []:
            if isinstance(m, dict):
                lines.append(
                    f"IA murder {m.get('murderID')}: killer={self.human_name(m.get('murdererID'))}, "
                    f"victim={self.human_name(m.get('victimID'))}, preset={m.get('presetStr')}, "
                    f"mo={m.get('moStr')}, weapon={m.get('weaponStr')}, address={m.get('addressID')}, state={m.get('state')}"
                )
        lines.append("")
        lines.append("=== COUNTS ===")
        for k in ["activeCases", "archivedCases", "citizens", "evidence", "interactables", "addresses", "rooms", "companies", "messageThreads"]:
            v = self.obj.get(k)
            lines.append(f"{k}: {len(v) if isinstance(v, list) else '—'}")
        return "\n".join(lines)

    def pretty_case_details(self, cinfo: CaseInfo) -> str:
        c = cinfo.raw
        lines = [
            f"Case #{cinfo.cid}: {cinfo.name}",
            f"Type/status: {cinfo.case_type} / {cinfo.status}",
            f"Active: {cinfo.active}, solved: {cinfo.solved}, jobReference: {cinfo.job_id}",
        ]
        job = self.job_by_id.get(cinfo.job_id, {})
        if job:
            lines += [
                "",
                "=== JOB ===",
                f"section: {job.get('__section')}",
                f"preset: {job.get('presetStr')}",
                f"target/purpID: {self.human_name(job.get('purpID'))}",
                f"posterID: {self.human_name(job.get('posterID'))}",
                f"reward: {job.get('reward')}",
                f"motive: {job.get('motiveStr')}",
                f"handIn: {job.get('handIn')}",
                f"phase/state: {job.get('phase')} / {job.get('state')}",
                f"fakeNumber: {job.get('fakeNumberStr')}",
                f"secretLocationNode: {job.get('secretLocationNode')}",
                f"secretLocationFurniture: {job.get('secretLocationFurniture')}",
            ]
        lines.append("")
        lines.append("=== RESOLVE QUESTIONS ===")
        for q in c.get("resolveQuestions", []) or []:
            if not isinstance(q, dict):
                continue
            correct = q.get("correctAnswers") or []
            auto = q.get("automaticAnswers") or []
            mapped = []
            for ans in correct:
                if isinstance(ans, str) and ans.isdigit():
                    mapped.append(self.human_name(int(ans)))
                elif isinstance(ans, str) and HUMAN_RE.match(ans):
                    mapped.append(self.human_name(int(HUMAN_RE.match(ans).group(1))))
                else:
                    mapped.append(str(ans))
            lines.append(f"- {q.get('name')}")
            lines.append(f"  input={q.get('input')}, correct={correct}, mapped={mapped}, auto={auto}, valid={q.get('isValid')}, reward={q.get('reward')}")
        lines.append("")
        lines.append("=== CASE ELEMENTS ===")
        for el in c.get("caseElements", []) or []:
            if isinstance(el, dict):
                lines.append(f"- {el.get('id')}: {el.get('n')}")
        return "\n".join(lines)

    def pretty_person_details(self, p: PersonInfo) -> str:
        lines = [
            f"Human{p.cid}: {p.name or '(unknown name)'}",
            f"Role/job: {p.role or '—'}",
            f"Flags: {', '.join(p.flags) or '—'}",
            f"Position: {p.position or '—'}",
            f"Current goal: {p.goal or '—'} at location/address ID {p.location_id or '—'}",
            f"Name source: {self.roster_source.get(p.cid, '—')}",
            f"Personal passcode: {self.password_status_for_human(p.cid)}",
        ]
        rec = self.personal_passcode_record(p.cid)
        if not rec:
            lines += [
                "",
                "=== PASSWORD NOTE ===",
                "Для этого HumanID личный пароль не найден в top-level passcodes.",
                "Это не значит, что у NPC его нет в игре: сейв часто хранит здесь только уже сгенерированные/известные коды.",
                "Можно найти его игровым способом или вручную задать код через вкладку «Пароли»."
            ]
        if p.seen_ids:
            named = [self.human_name(x) for x in p.seen_ids[:80]]
            lines.append("")
            lines.append(f"Seen / sightingCit ({len(p.seen_ids)} shown up to 80):")
            lines.extend([f"- {x}" for x in named])
        if p.ties:
            lines.append("")
            lines.append("Evidence keyTies sample:")
            lines.extend([f"- {x}" for x in p.ties])
        lines.append("")
        lines.append("Raw citizen JSON:")
        lines.append(json.dumps(p.raw, ensure_ascii=False, indent=2)[:100_000])
        return "\n".join(lines)

    def company_infos(self) -> List[CompanyInfo]:
        company_by_id = {
            c.get("id"): c for c in self.obj.get("companies", []) or []
            if isinstance(c, dict) and isinstance(c.get("id"), int)
        }
        ids = set(company_by_id.keys())
        for evid in self.evidence_by_id:
            m = re.match(r"CompanyRoster(\d+)$", evid, re.I)
            if m:
                ids.add(int(m.group(1)))

        rows: List[CompanyInfo] = []
        for cid in sorted(ids):
            evid = f"CompanyRoster{cid}"
            ev = self.evidence_by_id.get(evid, {})
            workers: List[Tuple[int, str, str]] = []
            if ev:
                by_page: Dict[Any, List[Dict[str, Any]]] = {}
                for item in ev.get("mpContent", []) or []:
                    if isinstance(item, dict):
                        by_page.setdefault(item.get("page", 0), []).append(item)
                for _page, items in by_page.items():
                    last_text = ""
                    for item in sorted(items, key=lambda x: x.get("order", -1)):
                        text = str(item.get("str") or "").strip()
                        disc = str(item.get("discEvID") or "")
                        m = HUMAN_RE.match(disc)
                        if m:
                            hid = int(m.group(1))
                            name, role = self._split_name_role(last_text)
                            workers.append((hid, name or self.name_map.get(hid, ""), role or self.role_map.get(hid, "")))
                        if text:
                            last_text = text
            comp = company_by_id.get(cid, {})
            sales = [x for x in comp.get("sales", []) or [] if isinstance(x, dict)] if comp else []
            sales_total = sum(float(x.get("cost") or 0) for x in sales)
            role_counts: Dict[str, int] = {}
            for _hid, _name, role in workers:
                if role:
                    role_counts[role] = role_counts.get(role, 0) + 1
            roles = ", ".join(f"{k}×{v}" for k, v in sorted(role_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8])
            sample = ", ".join(f"{name or self.human_name(hid)}" + (f" — {role}" if role else "") for hid, name, role in workers[:6])
            rows.append(CompanyInfo(
                cid=cid,
                roster_id=evid if ev else "—",
                workers=workers,
                sales_count=len(sales),
                sales_total=sales_total,
                roles=roles,
                workers_sample=sample,
                raw_company=comp,
                raw_evidence=ev,
            ))
        return rows

    def pretty_company_details(self, info: CompanyInfo) -> str:
        lines = [
            f"Company{info.cid}",
            f"Roster evidence: {info.roster_id}",
            f"Workers: {len(info.workers)}",
            f"Sales: {info.sales_count}, total cost: {info.sales_total:g}",
            f"Roles: {info.roles or '—'}",
        ]
        if info.workers:
            lines += ["", "=== WORKERS ==="]
            for hid, name, role in info.workers:
                lines.append(f"- {self.human_name(hid)}" + (f" — {role}" if role else ""))
        sales = [x for x in info.raw_company.get("sales", []) or [] if isinstance(x, dict)] if info.raw_company else []
        if sales:
            lines += ["", "=== SALES SAMPLE ==="]
            for sale in sales[:80]:
                items = ", ".join(str(x) for x in sale.get("items", [])[:8]) if isinstance(sale.get("items"), list) else str(sale.get("items"))
                lines.append(f"- punter={self.human_name(sale.get('punterID'))}, cost={sale.get('cost')}, time={sale.get('time')}, items={items}")
        lines += ["", "=== RAW COMPANY ===", json.dumps(info.raw_company, ensure_ascii=False, indent=2)[:80_000]]
        if info.raw_evidence:
            lines += ["", "=== RAW ROSTER EVIDENCE ===", json.dumps(info.raw_evidence, ensure_ascii=False, indent=2)[:80_000]]
        return "\n".join(lines)

    def person_relations(self, cid: int, limit: int = 36) -> List[RelationInfo]:
        person = next((p for p in self.person_infos() if p.cid == cid), None)
        if not person:
            return []
        scores: Dict[int, Tuple[int, List[str]]] = {}

        def add(dst: Any, kind: str, detail: str = "", weight: int = 1) -> None:
            if not isinstance(dst, int) or dst <= 0 or dst == cid:
                return
            score, details = scores.get(dst, (0, []))
            label = kind + (f": {detail}" if detail else "")
            if label not in details:
                details.append(label)
            scores[dst] = (score + weight, details)

        for sid in person.seen_ids:
            add(sid, "seen", weight=1)

        ev = self.evidence_by_id.get(f"Human{cid}", {})
        for kt in ev.get("keyTies", []) or []:
            if not isinstance(kt, dict):
                continue
            tied = kt.get("tied")
            if isinstance(tied, str):
                m = HUMAN_ANY_RE.search(tied)
                if m:
                    add(int(m.group(1)), "keyTie", f"key={kt.get('key')}", weight=4)

        # Same CompanyRoster = colleagues.
        src_roster = self.roster_source.get(cid)
        if src_roster:
            roster = self.evidence_by_id.get(src_roster, {})
            for item in roster.get("mpContent", []) or []:
                if isinstance(item, dict):
                    m = HUMAN_RE.match(str(item.get("discEvID") or ""))
                    if m:
                        add(int(m.group(1)), "same roster", src_roster, weight=3)

        # Murder/case links.
        for m in self.obj.get("murders", []) or []:
            if isinstance(m, dict):
                if m.get("murdererID") == cid:
                    add(m.get("victimID"), "murder victim", str(m.get("presetStr") or m.get("weaponStr") or ""), weight=10)
                if m.get("victimID") == cid:
                    add(m.get("murdererID"), "murderer", str(m.get("presetStr") or m.get("weaponStr") or ""), weight=10)
        for m in self.obj.get("iaMurders", []) or []:
            if isinstance(m, dict):
                if m.get("murdererID") == cid:
                    add(m.get("victimID"), "ia murder victim", str(m.get("weaponStr") or ""), weight=10)
                if m.get("victimID") == cid:
                    add(m.get("murdererID"), "ia murderer", str(m.get("weaponStr") or ""), weight=10)

        rows = []
        for dst, (score, details) in sorted(scores.items(), key=lambda kv: (-kv[1][0], self.human_name(kv[0]))):
            rows.append(RelationInfo(src=cid, dst=dst, kind=", ".join(details[:3]), detail=f"score={score}"))
            if len(rows) >= limit:
                break
        return rows

    def search_records(self, query: str, section: str = "both", limit: int = 500) -> List[SearchResultInfo]:
        q = (query or "").strip().lower()
        if not q:
            return []
        results: List[SearchResultInfo] = []

        def hit(text: str) -> bool:
            return q in text.lower()

        def compact_json(d: Dict[str, Any], max_len: int = 1800) -> str:
            try:
                raw = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                raw = str(d)
            return raw[:max_len]

        if section in ("both", "evidence"):
            for ev in self.obj.get("evidence", []) or []:
                if not isinstance(ev, dict):
                    continue
                parts = [str(ev.get("id", "")), str(ev.get("n", "")), str(ev.get("dds", "")), str(ev.get("found", ""))]
                for item in ev.get("mpContent", []) or []:
                    if isinstance(item, dict):
                        parts.extend([str(item.get("str", "")), str(item.get("discEvID", "")), str(item.get("evID", ""))])
                for kt in ev.get("keyTies", []) or []:
                    if isinstance(kt, dict):
                        parts.extend([str(kt.get("key", "")), str(kt.get("tied", ""))])
                text = " | ".join(parts)
                if hit(text):
                    eid = str(ev.get("id", ""))
                    title = str(ev.get("n") or eid)
                    owner = ""
                    m = HUMAN_ANY_RE.search(eid + " " + text)
                    if m:
                        owner = self.human_name(int(m.group(1)))
                    results.append(SearchResultInfo("evidence", eid, title, owner or "—", "—", text[:500], ev))
                    if len(results) >= limit:
                        return results

        if section in ("both", "interactables"):
            for it in self.obj.get("interactables", []) or []:
                if not isinstance(it, dict):
                    continue
                keys = ["id", "p", "lp", "dds", "nEvKey", "w", "r", "b", "inv", "val", "locked", "print", "cap", "sCap"]
                text = " | ".join(str(it.get(k, "")) for k in keys) + " | " + compact_json({k: it.get(k) for k in ("pv", "passcode", "wPos", "spWPos") if k in it}, 1000)
                if hit(text):
                    iid = str(it.get("id", ""))
                    title = str(it.get("p") or it.get("lp") or iid)
                    owner_id = it.get("w") if isinstance(it.get("w"), int) and it.get("w") > 0 else it.get("inv")
                    owner = self.human_name(owner_id) if isinstance(owner_id, int) and owner_id > 0 else "—"
                    room_id = it.get("r") if isinstance(it.get("r"), int) and it.get("r") >= 0 else None
                    loc = self.room_label(room_id) if room_id is not None else self._fmt_pos(it.get("wPos"))
                    results.append(SearchResultInfo("interactable", iid, title, owner, loc, text[:500], it))
                    if len(results) >= limit:
                        return results
        return results


# ---------------------------------------------------------------------------
# Small JSON path editor
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?")


def get_path(root: Any, path: str) -> Any:
    cur = root
    for part in path.strip().split("."):
        if not part:
            continue
        m = _TOKEN_RE.fullmatch(part)
        if not m:
            raise KeyError(f"Bad path part: {part}")
        key, idx = m.group(1), m.group(2)
        if not isinstance(cur, dict):
            raise KeyError(f"Expected dict before {key}")
        cur = cur[key]
        if idx is not None:
            cur = cur[int(idx)]
    return cur


def set_path(root: Any, path: str, value: Any) -> None:
    parts = path.strip().split(".")
    cur = root
    for part in parts[:-1]:
        m = _TOKEN_RE.fullmatch(part)
        if not m:
            raise KeyError(f"Bad path part: {part}")
        key, idx = m.group(1), m.group(2)
        cur = cur[key]
        if idx is not None:
            cur = cur[int(idx)]
    m = _TOKEN_RE.fullmatch(parts[-1])
    if not m:
        raise KeyError(f"Bad path part: {parts[-1]}")
    key, idx = m.group(1), m.group(2)
    if idx is None:
        cur[key] = value
    else:
        cur[key][int(idx)] = value


def parse_value(text: str) -> Any:
    text = text.strip()
    if text == "":
        return ""
    try:
        return json.loads(text)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class SodbEditorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1480x900")
        self.minsize(1150, 720)
        self._set_app_icon()

        self.path: Optional[Path] = None
        self.obj: Optional[Dict[str, Any]] = None
        self.initial_obj: Optional[Dict[str, Any]] = None  # чистая копия сразу после открытия файла; нужна для сброса правок
        self.codec_note = ""
        self.compressed = True
        self.analyzer: Optional[SodbAnalyzer] = None
        self.dirty = False
        self.raw_loaded_full = False

        self._setup_style()
        self._build_ui()

    def _set_app_icon(self) -> None:
        icon_path = app_resource_path(APP_ICON_FILE)
        if not icon_path.exists():
            return

        # Window title bar icon. This handles the normal .py run and the
        # PyInstaller EXE when icon.ico is bundled with --add-data.
        try:
            self.iconbitmap(default=str(icon_path))
        except Exception:
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        # Windows taskbar icon grouping. Harmless on non-Windows systems.
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("sodb.save.editor")
            except Exception:
                pass

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        self.configure(bg="#10131a")
        style.configure("TFrame", background="#10131a")
        style.configure("Panel.TFrame", background="#151a24")
        style.configure("TLabel", background="#10131a", foreground="#d8dde8", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#10131a", foreground="#ff87a7", font=("Segoe UI", 15, "bold"))
        style.configure("Hint.TLabel", background="#10131a", foreground="#9aa5b5", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 6))
        style.configure("TNotebook", background="#10131a", borderwidth=0)
        style.configure("TNotebook.Tab", background="#20283a", foreground="#d8dde8", padding=(14, 8), font=("Segoe UI", 10))
        style.map("TNotebook.Tab", background=[("selected", "#7b2d46")], foreground=[("selected", "#ffffff")])
        style.configure("Treeview", background="#151a24", foreground="#d8dde8", fieldbackground="#151a24", rowheight=26, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#20283a", foreground="#ffffff", font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#8a3554")], foreground=[("selected", "#ffffff")])
        style.configure("TEntry", fieldbackground="#0f1320", foreground="#ffffff", insertcolor="#ffffff")

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Label(top, text="Shadows of Doubt Save Editor", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  .sodb → JSON → анализ/редактирование → .sodb", style="Hint.TLabel").pack(side="left", padx=8)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=14, pady=6)
        self.path_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(bar, text="Открыть файл", command=self.open_file).pack(side="left", padx=3)
        ttk.Button(bar, text="Расшифровать", style="Accent.TButton", command=self.decode_current).pack(side="left", padx=3)
        ttk.Button(bar, text="Очистить", command=self.clear_state).pack(side="left", padx=3)
        ttk.Button(bar, text="Сбросить правки", command=self.reset_to_loaded).pack(side="left", padx=3)
        ttk.Button(bar, text="?", width=3, command=self.show_help).pack(side="left", padx=3)
        ttk.Button(bar, text="Сохранить как .sodb", command=self.save_as).pack(side="left", padx=3)
        ttk.Button(bar, text="Backup + overwrite", command=self.backup_and_overwrite).pack(side="left", padx=3)
        ttk.Button(bar, text="Экспорт JSON", command=self.export_json).pack(side="left", padx=3)
        ttk.Button(bar, text="Экспорт CSV", command=self.export_current_table_csv).pack(side="left", padx=3)

        self.status_var = tk.StringVar(value="Открой .sodb файл. Перед overwrite лучше закрыть игру и сделать бэкап.")
        ttk.Label(self, textvariable=self.status_var, style="Hint.TLabel").pack(fill="x", padx=14, pady=(0, 8))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        self._build_dashboard_tab()
        self._build_cases_tab()
        self._build_people_tab()
        self._build_passwords_tab()
        self._build_criminals_tab()
        self._build_relation_graph_tab()
        self._build_search_tab()
        self._build_companies_tab()
        self._build_raw_tab()
        self._build_path_editor_tab()

    def _text(self, parent: tk.Widget, wrap: str = "none") -> tk.Text:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap=wrap, bg="#0f1320", fg="#d8dde8", insertbackground="#ffffff",
                       selectbackground="#8a3554", font=("Consolas", 10), relief="flat")
        y = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        x = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        text.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return text

    def _build_dashboard_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="Обзор / деньги")

        editor = ttk.Frame(tab, style="Panel.TFrame")
        editor.pack(fill="x", padx=12, pady=10)
        self.stat_vars: Dict[str, tk.StringVar] = {}
        fields = ["money", "lockpicks", "socCredit", "health", "nourishment", "hydration", "energy", "hygiene"]
        for i, key in enumerate(fields):
            ttk.Label(editor, text=key).grid(row=i // 4, column=(i % 4) * 2, sticky="e", padx=(0, 5), pady=4)
            var = tk.StringVar()
            self.stat_vars[key] = var
            ttk.Entry(editor, textvariable=var, width=14).grid(row=i // 4, column=(i % 4) * 2 + 1, sticky="w", padx=(0, 14), pady=4)
        ttk.Button(editor, text="Применить статы", command=self.apply_stats).grid(row=0, column=8, padx=8, pady=4)
        ttk.Button(editor, text="Чит-пресет", command=self.cheat_preset).grid(row=1, column=8, padx=8, pady=4)
        ttk.Button(editor, text="?", width=3, command=self.show_help).grid(row=0, column=9, rowspan=2, padx=(0, 8), pady=4)

        self.dashboard_text = self._text(tab)

    def _build_cases_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="Кейсы")
        paned = ttk.Panedwindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=10)
        left = ttk.Frame(paned, style="Panel.TFrame")
        right = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(left, weight=2)
        paned.add(right, weight=3)
        cols = ("id", "name", "type", "status", "target", "reward", "short")
        self.cases_tree = ttk.Treeview(left, columns=cols, show="headings")
        for col, width in [("id", 50), ("name", 160), ("type", 130), ("status", 110), ("target", 210), ("reward", 70), ("short", 420)]:
            self.cases_tree.heading(col, text=col)
            self.cases_tree.column(col, width=width, anchor="w")
        self.cases_tree.pack(fill="both", expand=True)
        self.cases_tree.bind("<<TreeviewSelect>>", self.on_case_select)
        self.case_details = self._text(right)

    def _build_people_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="Люди / связи")
        toolbar = ttk.Frame(tab, style="Panel.TFrame")
        toolbar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(toolbar, text="Фильтр:").pack(side="left")
        self.people_filter = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.people_filter, width=40).pack(side="left", padx=6)
        self.people_filter.trace_add("write", lambda *_: self.populate_people())
        self.only_criminals = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="только подозрительные/криминалы", variable=self.only_criminals, command=self.populate_people).pack(side="left", padx=10)

        paned = ttk.Panedwindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        left = ttk.Frame(paned, style="Panel.TFrame")
        right = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(left, weight=2)
        paned.add(right, weight=3)
        cols = ("id", "name", "role", "password", "flags", "goal", "loc", "pos")
        self.people_tree = ttk.Treeview(left, columns=cols, show="headings")
        for col, width in [("id", 70), ("name", 170), ("role", 150), ("password", 150), ("flags", 230), ("goal", 120), ("loc", 70), ("pos", 180)]:
            self.people_tree.heading(col, text=col)
            self.people_tree.column(col, width=width, anchor="w")
        self.people_tree.pack(fill="both", expand=True)
        self.people_tree.bind("<<TreeviewSelect>>", self.on_person_select)
        self.person_details = self._text(right)

    def _build_passwords_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="Пароли")
        toolbar = ttk.Frame(tab, style="Panel.TFrame")
        toolbar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(toolbar, text="Поиск по имени / HumanID / коду / комнате:").pack(side="left")
        self.password_filter = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.password_filter, width=44).pack(side="left", padx=6)
        self.password_filter.trace_add("write", lambda *_: self.populate_passwords())
        ttk.Button(toolbar, text="Скопировать код", command=self.copy_selected_password).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Найти HumanID", command=self.find_password_for_human_dialog).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Задать пароль HumanID", command=self.set_password_for_human_dialog).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Изменить выбранный", command=self.change_selected_password).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Пометить все used=True", command=self.mark_all_passcodes_used).pack(side="left", padx=4)

        hint = ttk.Label(tab, text="Вкладка показывает top-level passcodes из сейва: это обычно уже известные/сгенерированные коды, а не гарантированно все NPC города. Если HumanID не найден — точного кода в этом списке нет.", style="Hint.TLabel")
        hint.pack(anchor="w", padx=12, pady=(0, 6))

        paned = ttk.Panedwindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        left = ttk.Frame(paned, style="Panel.TFrame")
        right = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(left, weight=3)
        paned.add(right, weight=2)
        cols = ("idx", "code", "type", "target", "owner", "used", "notes")
        self.passwords_tree = ttk.Treeview(left, columns=cols, show="headings")
        for col, width in [("idx", 55), ("code", 80), ("type", 135), ("target", 300), ("owner", 300), ("used", 60), ("notes", 220)]:
            self.passwords_tree.heading(col, text=col)
            self.passwords_tree.column(col, width=width, anchor="w")
        self.passwords_tree.pack(fill="both", expand=True)
        self.passwords_tree.bind("<<TreeviewSelect>>", self.on_password_select)
        self.password_details = self._text(right)

    def _build_criminals_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="Убийцы / криминалы")
        cols = ("id", "name", "kind", "details")
        self.criminals_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for col, width in [("id", 70), ("name", 230), ("kind", 180), ("details", 700)]:
            self.criminals_tree.heading(col, text=col)
            self.criminals_tree.column(col, width=width, anchor="w")
        self.criminals_tree.pack(fill="both", expand=True, padx=10, pady=10)

    def _build_relation_graph_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="Граф связей")
        toolbar = ttk.Frame(tab, style="Panel.TFrame")
        toolbar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(toolbar, text="HumanID / имя:").pack(side="left")
        self.graph_query = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.graph_query, width=36).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Нарисовать", command=self.draw_relation_graph).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Из выбранного человека", command=self.graph_from_selected_person).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Экспорт связей CSV", command=self.export_relation_graph_csv).pack(side="left", padx=4)
        ttk.Label(toolbar, text="Граф использует sightingCit, keyTies, общий roster и murder links.", style="Hint.TLabel").pack(side="left", padx=12)

        paned = ttk.Panedwindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        left = ttk.Frame(paned, style="Panel.TFrame")
        right = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(left, weight=4)
        paned.add(right, weight=2)
        self.graph_canvas = tk.Canvas(left, bg="#0f1320", highlightthickness=0)
        self.graph_canvas.pack(fill="both", expand=True)
        cols = ("src", "dst", "name", "kind", "detail")
        self.graph_tree = ttk.Treeview(right, columns=cols, show="headings")
        for col, width in [("src", 70), ("dst", 70), ("name", 160), ("kind", 230), ("detail", 100)]:
            self.graph_tree.heading(col, text=col)
            self.graph_tree.column(col, width=width, anchor="w")
        self.graph_tree.pack(fill="both", expand=True)

    def _build_search_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="Поиск evidence/interactables")
        toolbar = ttk.Frame(tab, style="Panel.TFrame")
        toolbar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(toolbar, text="Запрос:").pack(side="left")
        self.deep_search_var = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.deep_search_var, width=45).pack(side="left", padx=6)
        self.deep_search_scope = tk.StringVar(value="both")
        ttk.Combobox(toolbar, textvariable=self.deep_search_scope, width=16, state="readonly", values=("both", "evidence", "interactables")).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Искать", command=self.run_deep_search).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Экспорт результатов CSV", command=self.export_search_results_csv).pack(side="left", padx=4)

        paned = ttk.Panedwindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        left = ttk.Frame(paned, style="Panel.TFrame")
        right = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(left, weight=3)
        paned.add(right, weight=2)
        cols = ("section", "id", "title", "owner", "location", "snippet")
        self.search_tree = ttk.Treeview(left, columns=cols, show="headings")
        for col, width in [("section", 90), ("id", 120), ("title", 220), ("owner", 170), ("location", 260), ("snippet", 520)]:
            self.search_tree.heading(col, text=col)
            self.search_tree.column(col, width=width, anchor="w")
        self.search_tree.pack(fill="both", expand=True)
        self.search_tree.bind("<<TreeviewSelect>>", self.on_search_select)
        self.search_details = self._text(right)
        self._search_results_cache: List[SearchResultInfo] = []

    def _build_companies_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="Компании / работы")
        toolbar = ttk.Frame(tab, style="Panel.TFrame")
        toolbar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(toolbar, text="Фильтр:").pack(side="left")
        self.companies_filter = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.companies_filter, width=42).pack(side="left", padx=6)
        self.companies_filter.trace_add("write", lambda *_: self.populate_companies())
        ttk.Button(toolbar, text="Экспорт CSV", command=self.export_companies_csv).pack(side="left", padx=4)

        paned = ttk.Panedwindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        left = ttk.Frame(paned, style="Panel.TFrame")
        right = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(left, weight=3)
        paned.add(right, weight=2)
        cols = ("id", "roster", "workers", "sales", "total", "roles", "sample")
        self.companies_tree = ttk.Treeview(left, columns=cols, show="headings")
        for col, width in [("id", 80), ("roster", 130), ("workers", 80), ("sales", 70), ("total", 80), ("roles", 330), ("sample", 520)]:
            self.companies_tree.heading(col, text=col)
            self.companies_tree.column(col, width=width, anchor="w")
        self.companies_tree.pack(fill="both", expand=True)
        self.companies_tree.bind("<<TreeviewSelect>>", self.on_company_select)
        self.company_details = self._text(right)

    def _build_raw_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="Raw JSON")
        toolbar = ttk.Frame(tab, style="Panel.TFrame")
        toolbar.pack(fill="x", padx=10, pady=10)
        ttk.Button(toolbar, text="Показать preview", command=self.show_raw_preview).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Показать полный JSON", command=self.show_full_raw).pack(side="left", padx=3)
        ttk.Label(toolbar, text="Поиск:").pack(side="left", padx=(20, 3))
        self.raw_search_var = tk.StringVar()
        ttk.Entry(toolbar, textvariable=self.raw_search_var, width=42).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Найти", command=self.search_raw).pack(side="left", padx=3)
        self.raw_text = self._text(tab)

    def _build_path_editor_tab(self) -> None:
        tab = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(tab, text="JSON path editor")
        info = ttk.Label(tab, text="Продвинутый режим. Пример path: money, citizens[0].convicted, activeCases[2].name. Значение можно писать как JSON: 123, true, \"text\".", style="Hint.TLabel")
        info.pack(anchor="w", padx=12, pady=(12, 4))
        form = ttk.Frame(tab, style="Panel.TFrame")
        form.pack(fill="x", padx=12, pady=8)
        self.path_edit_var = tk.StringVar(value="money")
        self.path_value_var = tk.StringVar()
        ttk.Label(form, text="Path:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(form, textvariable=self.path_edit_var, width=70).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(form, text="Get", command=self.path_get).grid(row=0, column=2, padx=4, pady=4)
        ttk.Label(form, text="Value:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(form, textvariable=self.path_value_var, width=70).grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(form, text="Set", command=self.path_set).grid(row=1, column=2, padx=4, pady=4)
        form.columnconfigure(1, weight=1)
        self.path_output = self._text(tab)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def clear_state(self) -> None:
        """Полностью очищает приложение до стартового состояния: без открытого сейва и без данных в таблицах."""
        if self.dirty:
            if not messagebox.askyesno("Очистить", "Есть несохранённые изменения. Очистить окно без сохранения?"):
                return
        self.path = None
        self.obj = None
        self.initial_obj = None
        self.analyzer = None
        self.codec_note = ""
        self.compressed = True
        self.dirty = False
        self.raw_loaded_full = False
        self.path_var.set("")
        for var in getattr(self, "stat_vars", {}).values():
            var.set("")
        for tree_name in ("cases_tree", "people_tree", "passwords_tree", "criminals_tree", "graph_tree", "search_tree", "companies_tree"):
            tree = getattr(self, tree_name, None)
            if tree is not None:
                try:
                    tree.delete(*tree.get_children())
                except Exception:
                    pass
        for text_name in ("dashboard_text", "case_details", "person_details", "password_details", "search_details", "company_details", "raw_text", "path_output"):
            txt = getattr(self, text_name, None)
            if txt is not None:
                try:
                    txt.delete("1.0", "end")
                except Exception:
                    pass
        if hasattr(self, "people_filter"):
            self.people_filter.set("")
        if hasattr(self, "password_filter"):
            self.password_filter.set("")
        if hasattr(self, "graph_query"):
            self.graph_query.set("")
        if hasattr(self, "graph_canvas"):
            self.graph_canvas.delete("all")
        if hasattr(self, "deep_search_var"):
            self.deep_search_var.set("")
        if hasattr(self, "companies_filter"):
            self.companies_filter.set("")
        if hasattr(self, "raw_search_var"):
            self.raw_search_var.set("")
        if hasattr(self, "path_edit_var"):
            self.path_edit_var.set("money")
        if hasattr(self, "path_value_var"):
            self.path_value_var.set("")
        self.status_var.set("Окно очищено. Открой .sodb файл.")

    def reset_to_loaded(self) -> None:
        """Откатывает все правки к состоянию сразу после последней расшифровки/открытия файла."""
        if not self.initial_obj:
            messagebox.showinfo("Сброс", "Нет открытого сейва, к которому можно сброситься.")
            return
        if self.dirty:
            if not messagebox.askyesno("Сбросить правки", "Откатить все несохранённые изменения к состоянию сразу после открытия файла?"):
                return
        self.obj = copy.deepcopy(self.initial_obj)
        self.analyzer = SodbAnalyzer(self.obj)
        self.dirty = False
        self.refresh_all()
        self.status_var.set("Правки сброшены к состоянию после открытия файла.")

    def show_help(self) -> None:
        text = """SODB Save Editor — справка

1) Основной поток
- Открыть файл: выбрать .sodb или несжатый .json-сейв.
- Расшифровать: распаковать Brotli-сжатый .sodb и разобрать JSON.
- Сохранить как .sodb: создать новый отредактированный файл.
- Backup + overwrite: создать .bak рядом с исходником и заменить исходный сейв.
- Очистить: полностью очистить окно и вернуться к стартовому состоянию.
- Сбросить правки: откатить текущие изменения к состоянию сразу после открытия файла.

Перед Backup + overwrite лучше закрыть игру. Если игра запишет сейв поверх, изменения могут потеряться.

2) Диапазоны основных статов
Эти значения в сейве обычно хранятся как нормализованные числа.
Безопасный диапазон для ручного редактирования: 0.0–1.0.
Игра технически может принять значения вне диапазона, но это риск поломать состояние персонажа.

money: целое число, деньги игрока. Обычно можно ставить 0 и выше.
lockpicks: целое число, количество отмычек. Обычно можно ставить 0 и выше.
socCredit: целое число, социальный кредит. Безопасно ставить положительные значения.
health: 0.0–1.0. Обычно 1.0 = полное здоровье, 0.0 = критическое/нулевое здоровье.
nourishment: 0.0–1.0. Параметр сытости/голода. В разных версиях игры смысл может быть инвертирован; если сомневаешься, меняй маленькими шагами или используй чит-пресет.
hydration: 0.0–1.0. Параметр жажды/воды. Аналогично: лучше держать в 0.0–1.0.
energy: 0.0–1.0. Параметр усталости/энергии. Обычно 1.0 выглядит как максимум энергии.
hygiene: 0.0–1.0. Параметр чистоты/гигиены. Обычно 1.0 выглядит как чистое состояние.

3) Пароли
Вкладка «Пароли» показывает top-level passcodes из сейва. Важно: это обычно уже известные/сгенерированные коды, а не гарантированно все пароли всех 300+ NPC.
type=0 обычно личный пароль человека.
type=1 обычно пароль комнаты/локации.
Можно искать по имени, фамилии, HumanID, коду, RoomID.
«Найти HumanID» проверяет, есть ли личный type=0 пароль для конкретного человека.
«Задать пароль HumanID» добавляет или меняет личный type=0 пароль человеку прямо в save JSON. Делай бэкап перед overwrite.
Кнопка «Изменить выбранный» меняет digits у выбранного passcode и ставит used=True.

4) Кейсы и убийцы
Вкладка «Кейсы» показывает activeCases и пытается найти цель задания.
Вкладка «Убийцы / криминалы» ищет currentMurderer, iaMurders, convicted и death.killer.
Для серийного убийцы чаще всего важны строки IA murder: там есть killer, victim, weapon, preset и state.

5) Люди / связи
Вкладка «Люди / связи» показывает граждан из citizens и пытается подтянуть имена из evidence.
Фильтр ищет по имени, HumanID, работе, флагам и локации.
Связи выводятся из известных массивов сейва, поэтому это не идеальный граф, а удобная выжимка из JSON.

6) Raw JSON и JSON path editor
Raw JSON — просмотр расшифрованного сейва внутри программы. Preview ограничен, полный JSON может подвисать.
JSON path editor — продвинутый режим ручного изменения поля.
Примеры:
  money
  lockpicks
  citizens[0].convicted
  activeCases[2].name
Значение можно писать как JSON: 123, true, false, "text", [1,2,3].
"""
        win = tk.Toplevel(self)
        win.title("Справка / ?")
        win.geometry("820x720")
        win.configure(bg="#10131a")
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, style="Panel.TFrame")
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        txt = tk.Text(frame, wrap="word", bg="#0f1320", fg="#d8dde8", insertbackground="#ffffff",
                      selectbackground="#8a3554", font=("Segoe UI", 10), relief="flat")
        y = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=y.set)
        txt.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        ttk.Button(win, text="Закрыть", command=win.destroy).pack(pady=(0, 12))

    def open_file(self) -> None:
        start = os.path.expanduser(r"~\AppData\LocalLow\ColePowered Games\Shadows of Doubt\Save") if os.name == "nt" else os.path.expanduser("~")
        filename = filedialog.askopenfilename(
            title="Выбери .sodb / JSON save",
            initialdir=start if os.path.exists(start) else os.path.expanduser("~"),
            filetypes=[("Shadows of Doubt saves", "*.sodb *.json"), ("All files", "*.*")],
        )
        if filename:
            self.path = Path(filename)
            self.path_var.set(str(self.path))
            self.decode_current()

    def decode_current(self) -> None:
        p = Path(self.path_var.get().strip()) if self.path_var.get().strip() else self.path
        if not p or not p.exists():
            messagebox.showerror("Файл не найден", "Сначала выбери существующий .sodb файл.")
            return
        try:
            self.status_var.set("Распаковываю/читаю сейв...")
            self.update_idletasks()
            result = SodbCodec.decode(p)
            if not isinstance(result.obj, dict):
                raise RuntimeError("Корень JSON не object/dict; это неожиданный формат сейва.")
            self.path = p
            self.obj = result.obj
            self.initial_obj = copy.deepcopy(result.obj)
            self.compressed = result.compressed
            self.codec_note = result.codec_note
            self.analyzer = SodbAnalyzer(self.obj)
            self.dirty = False
            self.refresh_all()
            self.status_var.set(f"Открыто: {p.name} | codec: {self.codec_note} | people: {len(self.obj.get('citizens', []))} | evidence: {len(self.obj.get('evidence', []))}")
        except Exception as e:
            self.status_var.set("Ошибка чтения файла")
            messagebox.showerror("Ошибка", f"Не удалось расшифровать/прочитать файл:\n\n{e}\n\n{traceback.format_exc(limit=2)}")

    def refresh_all(self) -> None:
        if not self.obj or not self.analyzer:
            return
        self.fill_stats()
        self.dashboard_text.delete("1.0", "end")
        self.dashboard_text.insert("1.0", self.analyzer.dashboard_text())
        self.populate_cases()
        self.populate_people()
        self.populate_passwords()
        self.populate_criminals()
        self.populate_companies()
        if hasattr(self, "search_tree"):
            self.search_tree.delete(*self.search_tree.get_children())
            self._search_results_cache = []
        if hasattr(self, "search_details"):
            self.search_details.delete("1.0", "end")
        if hasattr(self, "graph_canvas"):
            self.graph_canvas.delete("all")
        if hasattr(self, "graph_tree"):
            self.graph_tree.delete(*self.graph_tree.get_children())
        self.show_raw_preview()

    def fill_stats(self) -> None:
        if not self.obj:
            return
        for k, var in self.stat_vars.items():
            var.set(str(self.obj.get(k, "")))

    def apply_stats(self) -> None:
        if not self.obj:
            return
        try:
            for k, var in self.stat_vars.items():
                txt = var.get().strip()
                if txt == "":
                    continue
                old = self.obj.get(k)
                if isinstance(old, int):
                    self.obj[k] = int(float(txt))
                elif isinstance(old, float):
                    self.obj[k] = float(txt)
                else:
                    self.obj[k] = parse_value(txt)
            self.mark_dirty("Статы применены. Не забудь сохранить .sodb.")
            self.refresh_all()
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не смог применить значения: {e}")

    def cheat_preset(self) -> None:
        if not self.obj:
            return
        preset = {
            "money": 999999,
            "lockpicks": 999,
            "socCredit": max(10, int(self.obj.get("socCredit", 0) or 0)),
            "health": 1.0,
            "nourishment": 0.0,
            "hydration": 0.0,
            "energy": 0.0,
            "hygiene": 1.0,
        }
        for k, v in preset.items():
            if k in self.obj:
                self.obj[k] = v
        self.fill_stats()
        self.mark_dirty("Чит-пресет применён. Сохрани .sodb, чтобы изменения попали в игру.")
        self.refresh_all()

    def mark_dirty(self, msg: str = "Есть несохранённые изменения") -> None:
        self.dirty = True
        self.status_var.set(msg + " *")

    def populate_cases(self) -> None:
        if not self.analyzer:
            return
        self.cases_tree.delete(*self.cases_tree.get_children())
        for ci in self.analyzer.case_infos():
            tags = ()
            self.cases_tree.insert("", "end", iid=str(ci.cid), values=(
                ci.cid, ci.name, ci.case_type, ci.status, ci.target_name, ci.reward, ci.short
            ), tags=tags)

    def on_case_select(self, _event=None) -> None:
        if not self.analyzer:
            return
        sel = self.cases_tree.selection()
        if not sel:
            return
        cid = int(sel[0])
        ci = next((x for x in self.analyzer.case_infos() if x.cid == cid), None)
        if not ci:
            return
        self.case_details.delete("1.0", "end")
        self.case_details.insert("1.0", self.analyzer.pretty_case_details(ci))

    def populate_people(self) -> None:
        if not self.analyzer:
            return
        text = self.people_filter.get().lower().strip() if hasattr(self, "people_filter") else ""
        only = self.only_criminals.get() if hasattr(self, "only_criminals") else False
        self.people_tree.delete(*self.people_tree.get_children())
        for p in self.analyzer.person_infos():
            pw_status = self.analyzer.password_status_for_human(p.cid)
            hay = f"{p.cid} Human{p.cid} {p.name} {p.role} {pw_status} {' '.join(p.flags)} {p.goal} {p.location_id}".lower()
            if text and text not in hay:
                continue
            if only and not p.flags:
                continue
            self.people_tree.insert("", "end", iid=str(p.cid), values=(
                f"Human{p.cid}", p.name, p.role, pw_status, ", ".join(p.flags), p.goal, p.location_id, p.position
            ))

    def on_person_select(self, _event=None) -> None:
        if not self.analyzer:
            return
        sel = self.people_tree.selection()
        if not sel:
            return
        cid = int(sel[0])
        p = next((x for x in self.analyzer.person_infos() if x.cid == cid), None)
        if not p:
            return
        self.person_details.delete("1.0", "end")
        self.person_details.insert("1.0", self.analyzer.pretty_person_details(p))

    def populate_passwords(self) -> None:
        if not self.analyzer or not hasattr(self, "passwords_tree"):
            return
        text = self.password_filter.get().lower().strip() if hasattr(self, "password_filter") else ""
        self.passwords_tree.delete(*self.passwords_tree.get_children())
        for p in self.analyzer.password_infos():
            hay = f"{p.index} {p.code} {p.pc_type} {p.target_kind} {p.target_id} {p.target_name} {p.owner_hint} {p.notes}".lower()
            if text and text not in hay:
                continue
            self.passwords_tree.insert("", "end", iid=str(p.index), values=(
                p.index, p.code, p.target_kind, p.target_name, p.owner_hint, str(p.used), p.notes
            ))

    def on_password_select(self, _event=None) -> None:
        if not self.analyzer or not hasattr(self, "password_details"):
            return
        sel = self.passwords_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        info = next((x for x in self.analyzer.password_infos() if x.index == idx), None)
        if not info:
            return
        self.password_details.delete("1.0", "end")
        self.password_details.insert("1.0", self.analyzer.pretty_password_details(info))

    def copy_selected_password(self) -> None:
        if not self.analyzer or not hasattr(self, "passwords_tree"):
            return
        sel = self.passwords_tree.selection()
        if not sel:
            messagebox.showinfo("Пароли", "Сначала выбери строку с паролем.")
            return
        idx = int(sel[0])
        info = next((x for x in self.analyzer.password_infos() if x.index == idx), None)
        if not info:
            return
        self.clipboard_clear()
        self.clipboard_append(info.code)
        self.status_var.set(f"Скопирован пароль: {info.code}")

    def _parse_human_query(self, query: str) -> Optional[int]:
        if not self.analyzer:
            return None
        q = str(query or "").strip()
        if not q:
            return None
        m = HUMAN_ANY_RE.search(q)
        if m:
            return int(m.group(1))
        if q.isdigit():
            return int(q)
        ql = q.lower()
        matches = [p for p in self.analyzer.person_infos() if ql in (p.name or "").lower()]
        if len(matches) == 1:
            return matches[0].cid
        if len(matches) > 1:
            sample = "\n".join(f"Human{x.cid}: {x.name}" for x in matches[:20])
            messagebox.showinfo("Найдено несколько людей", "Уточни HumanID. Совпадения:\n" + sample)
            return None
        return None

    def find_password_for_human_dialog(self) -> None:
        if not self.analyzer:
            return
        q = simpledialog.askstring("Найти пароль", "Введи HumanID или имя, например Human57 / 57 / Aaron Correa:", parent=self)
        if q is None:
            return
        cid = self._parse_human_query(q)
        if not cid:
            messagebox.showinfo("Пароли", "Не смог определить HumanID. Попробуй ввести точный вид Human57.")
            return
        rec = self.analyzer.personal_passcode_record(cid)
        if not rec:
            messagebox.showinfo(
                "Пароли",
                f"Для {self.analyzer.human_name(cid)} нет личного type=0 passcode в списке passcodes.\n\n"
                "То есть точный пароль не сохранён/не сгенерирован в этом месте сейва. "
                "Можно искать его игровым способом или задать код вручную кнопкой «Задать пароль HumanID»."
            )
            if hasattr(self, "password_filter"):
                self.password_filter.set(f"Human{cid}")
            return
        idx, pc, code = rec
        if hasattr(self, "password_filter"):
            self.password_filter.set(f"Human{cid}")
        self.populate_passwords()
        try:
            self.passwords_tree.selection_set(str(idx))
            self.passwords_tree.see(str(idx))
            self.on_password_select()
        except Exception:
            pass
        messagebox.showinfo("Пароли", f"{self.analyzer.human_name(cid)}: {code}")

    def set_password_for_human_dialog(self) -> None:
        if not self.obj or not self.analyzer:
            return
        q = simpledialog.askstring("Задать пароль человеку", "HumanID или имя, например Human57 / 57 / Aaron Correa:", parent=self)
        if q is None:
            return
        cid = self._parse_human_query(q)
        if not cid:
            messagebox.showinfo("Пароли", "Не смог определить HumanID. Попробуй ввести точный вид Human57.")
            return
        old = self.analyzer.personal_passcode_record(cid)
        old_code = old[2] if old else ""
        code = simpledialog.askstring(
            "Новый пароль",
            f"Код для {self.analyzer.human_name(cid)}. Ровно 4 цифры.\n"
            f"Старый код: {old_code or 'не найден в passcodes'}",
            initialvalue=old_code or "1234",
            parent=self,
        )
        if code is None:
            return
        try:
            idx, created = self.analyzer.add_or_update_personal_passcode(cid, code)
        except Exception as e:
            messagebox.showerror("Пароли", str(e))
            return
        self.mark_dirty(("Добавлен" if created else "Изменён") + f" пароль для {self.analyzer.human_name(cid)}. Не забудь сохранить .sodb.")
        self.populate_people()
        self.populate_passwords()
        if hasattr(self, "password_filter"):
            self.password_filter.set(f"Human{cid}")
        try:
            self.passwords_tree.selection_set(str(idx))
            self.passwords_tree.see(str(idx))
            self.on_password_select()
        except Exception:
            pass

    def change_selected_password(self) -> None:
        if not self.obj or not self.analyzer or not hasattr(self, "passwords_tree"):
            return
        sel = self.passwords_tree.selection()
        if not sel:
            messagebox.showinfo("Пароли", "Сначала выбери строку с паролем.")
            return
        idx = int(sel[0])
        infos = self.analyzer.password_infos()
        info = next((x for x in infos if x.index == idx), None)
        if not info:
            return
        new_code = simpledialog.askstring("Изменить пароль", f"Новый 4-значный пароль для {info.target_name}:", initialvalue=info.code, parent=self)
        if new_code is None:
            return
        new_code = new_code.strip()
        if not re.fullmatch(r"\d{4}", new_code):
            messagebox.showerror("Неверный пароль", "Нужны ровно 4 цифры, например 1234.")
            return
        self.obj["passcodes"][idx]["digits"] = [int(ch) for ch in new_code]
        self.obj["passcodes"][idx]["used"] = True
        self.mark_dirty(f"Пароль #{idx} изменён на {new_code}. Не забудь сохранить .sodb.")
        self.refresh_all()

    def mark_all_passcodes_used(self) -> None:
        if not self.obj:
            return
        if not messagebox.askyesno("Пароли", "Поставить used=True для всех passcodes? Это не гарантирует, что игра добавит их в UI как найденные, но сами коды останутся видны в редакторе."):
            return
        changed = 0
        for pc in self.obj.get("passcodes", []) or []:
            if isinstance(pc, dict) and not pc.get("used"):
                pc["used"] = True
                changed += 1
        self.mark_dirty(f"Помечено used=True: {changed}. Не забудь сохранить .sodb.")
        self.refresh_all()

    def populate_criminals(self) -> None:
        if not self.analyzer:
            return
        self.criminals_tree.delete(*self.criminals_tree.get_children())
        for cid, name, kind, details in self.analyzer.criminals():
            self.criminals_tree.insert("", "end", values=(f"Human{cid}", name, kind, details))

    def populate_companies(self) -> None:
        if not self.analyzer or not hasattr(self, "companies_tree"):
            return
        text = self.companies_filter.get().lower().strip() if hasattr(self, "companies_filter") else ""
        self.companies_tree.delete(*self.companies_tree.get_children())
        for c in self.analyzer.company_infos():
            hay = f"Company{c.cid} {c.roster_id} {c.roles} {c.workers_sample}".lower()
            if text and text not in hay:
                continue
            self.companies_tree.insert("", "end", iid=str(c.cid), values=(
                f"Company{c.cid}", c.roster_id, len(c.workers), c.sales_count, f"{c.sales_total:g}", c.roles, c.workers_sample
            ))

    def on_company_select(self, _event=None) -> None:
        if not self.analyzer or not hasattr(self, "companies_tree"):
            return
        sel = self.companies_tree.selection()
        if not sel:
            return
        cid_text = sel[0]
        try:
            cid = int(cid_text)
        except Exception:
            cid = int(cid_text.replace("Company", ""))
        info = next((x for x in self.analyzer.company_infos() if x.cid == cid), None)
        if not info:
            return
        self.company_details.delete("1.0", "end")
        self.company_details.insert("1.0", self.analyzer.pretty_company_details(info))

    def run_deep_search(self) -> None:
        if not self.analyzer or not hasattr(self, "search_tree"):
            return
        q = self.deep_search_var.get().strip()
        if not q:
            messagebox.showinfo("Поиск", "Введи строку поиска.")
            return
        scope = self.deep_search_scope.get() if hasattr(self, "deep_search_scope") else "both"
        self.status_var.set("Ищу по evidence/interactables...")
        self.update_idletasks()
        self._search_results_cache = self.analyzer.search_records(q, scope, limit=800)
        self.search_tree.delete(*self.search_tree.get_children())
        self.search_details.delete("1.0", "end")
        for idx, r in enumerate(self._search_results_cache):
            self.search_tree.insert("", "end", iid=str(idx), values=(r.section, r.rid, r.title, r.owner, r.location, r.snippet))
        self.status_var.set(f"Найдено: {len(self._search_results_cache)}")

    def on_search_select(self, _event=None) -> None:
        if not hasattr(self, "search_tree"):
            return
        sel = self.search_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(getattr(self, "_search_results_cache", [])):
            return
        r = self._search_results_cache[idx]
        self.search_details.delete("1.0", "end")
        self.search_details.insert("1.0",
            f"{r.section} {r.rid}\nTitle: {r.title}\nOwner: {r.owner}\nLocation: {r.location}\n\n" +
            json.dumps(r.raw, ensure_ascii=False, indent=2)[:160_000]
        )

    def graph_from_selected_person(self) -> None:
        if not hasattr(self, "people_tree"):
            return
        sel = self.people_tree.selection()
        if not sel:
            messagebox.showinfo("Граф", "Сначала выбери человека на вкладке «Люди / связи».")
            return
        self.graph_query.set(f"Human{sel[0]}")
        self.draw_relation_graph()

    def draw_relation_graph(self) -> None:
        if not self.analyzer or not hasattr(self, "graph_canvas"):
            return
        cid = self._parse_human_query(self.graph_query.get())
        if not cid:
            messagebox.showinfo("Граф", "Не смог определить HumanID. Введи Human57, 57 или точное имя.")
            return
        edges = self.analyzer.person_relations(cid, limit=32)
        self.graph_tree.delete(*self.graph_tree.get_children())
        for e in edges:
            self.graph_tree.insert("", "end", values=(f"Human{e.src}", f"Human{e.dst}", self.analyzer.human_name(e.dst), e.kind, e.detail))
        self._draw_graph_canvas(cid, edges)

    def _draw_graph_canvas(self, cid: int, edges: List[RelationInfo]) -> None:
        canvas = self.graph_canvas
        canvas.delete("all")
        canvas.update_idletasks()
        w = max(canvas.winfo_width(), 900)
        h = max(canvas.winfo_height(), 600)
        cx, cy = w // 2, h // 2
        radius = max(170, min(w, h) // 3)

        def node(x: float, y: float, label: str, fill: str, outline: str = "#ff87a7") -> None:
            canvas.create_oval(x - 52, y - 32, x + 52, y + 32, fill=fill, outline=outline, width=2)
            canvas.create_text(x, y - 6, text=label[:18], fill="#ffffff", font=("Segoe UI", 9, "bold"), width=96)

        # Background grid
        for gx in range(0, w, 48):
            canvas.create_line(gx, 0, gx, h, fill="#151b2a")
        for gy in range(0, h, 48):
            canvas.create_line(0, gy, w, gy, fill="#151b2a")

        node(cx, cy, self.analyzer.human_name(cid), "#7b2d46", "#ffd1dc")
        if not edges:
            canvas.create_text(cx, cy + 70, text="Связи не найдены", fill="#d8dde8", font=("Segoe UI", 12))
            return

        for i, e in enumerate(edges):
            angle = 2 * math.pi * i / max(len(edges), 1) - math.pi / 2
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle)
            canvas.create_line(cx, cy, x, y, fill="#b85c7b", width=2)
            midx, midy = (cx + x) / 2, (cy + y) / 2
            canvas.create_text(midx, midy, text=e.kind.split(",")[0][:22], fill="#ffb5c9", font=("Segoe UI", 8), width=140)
            node(x, y, self.analyzer.human_name(e.dst), "#20283a")

    def _tree_to_csv(self, tree: ttk.Treeview, default_name: str) -> None:
        if not self.obj:
            return
        out = filedialog.asksaveasfilename(
            title="Экспорт CSV", defaultextension=".csv", initialfile=default_name,
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")]
        )
        if not out:
            return
        cols = list(tree["columns"])
        try:
            with open(out, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(cols)
                for iid in tree.get_children(""):
                    vals = tree.item(iid, "values")
                    writer.writerow(list(vals))
            self.status_var.set(f"CSV сохранён: {out}")
        except Exception as e:
            messagebox.showerror("CSV", str(e))

    def export_current_table_csv(self) -> None:
        current = self.nb.select()
        tab_text = self.nb.tab(current, "text") if current else "table"
        mapping = {
            "Кейсы": (getattr(self, "cases_tree", None), "cases.csv"),
            "Люди / связи": (getattr(self, "people_tree", None), "people.csv"),
            "Пароли": (getattr(self, "passwords_tree", None), "passcodes.csv"),
            "Убийцы / криминалы": (getattr(self, "criminals_tree", None), "criminals.csv"),
            "Граф связей": (getattr(self, "graph_tree", None), "relations.csv"),
            "Поиск evidence/interactables": (getattr(self, "search_tree", None), "search_results.csv"),
            "Компании / работы": (getattr(self, "companies_tree", None), "companies.csv"),
        }
        tree, default_name = mapping.get(tab_text, (None, "table.csv"))
        if tree is None:
            messagebox.showinfo("CSV", "На текущей вкладке нет таблицы для экспорта.")
            return
        self._tree_to_csv(tree, default_name)

    def export_relation_graph_csv(self) -> None:
        if hasattr(self, "graph_tree"):
            self._tree_to_csv(self.graph_tree, "relations.csv")

    def export_search_results_csv(self) -> None:
        if hasattr(self, "search_tree"):
            self._tree_to_csv(self.search_tree, "search_results.csv")

    def export_companies_csv(self) -> None:
        if hasattr(self, "companies_tree"):
            self._tree_to_csv(self.companies_tree, "companies.csv")

    def show_raw_preview(self) -> None:
        if not self.obj:
            return
        self.raw_text.delete("1.0", "end")
        raw = json.dumps(self.obj, ensure_ascii=False, indent=2)
        suffix = ""
        if len(raw) > JSON_PREVIEW_LIMIT:
            suffix = f"\n\n... preview only: {JSON_PREVIEW_LIMIT:,} / {len(raw):,} chars. Нажми 'Показать полный JSON', если точно надо весь файл."
            raw = raw[:JSON_PREVIEW_LIMIT] + suffix
        self.raw_text.insert("1.0", raw)
        self.raw_loaded_full = False

    def show_full_raw(self) -> None:
        if not self.obj:
            return
        if not messagebox.askyesno("Полный JSON", "Полный JSON может быть десятки/сотни МБ и интерфейс может подвиснуть. Показать?"):
            return
        self.raw_text.delete("1.0", "end")
        self.status_var.set("Генерирую полный JSON...")
        self.update_idletasks()
        self.raw_text.insert("1.0", json.dumps(self.obj, ensure_ascii=False, indent=2))
        self.raw_loaded_full = True
        self.status_var.set("Полный JSON загружен в окно.")

    def search_raw(self) -> None:
        needle = self.raw_search_var.get()
        if not needle:
            return
        start = self.raw_text.search(needle, "insert", stopindex="end", nocase=True)
        if not start:
            start = self.raw_text.search(needle, "1.0", stopindex="end", nocase=True)
        if not start:
            messagebox.showinfo("Поиск", "Не найдено в текущем окне Raw JSON. Возможно, нужен полный JSON или экспорт.")
            return
        end = f"{start}+{len(needle)}c"
        self.raw_text.tag_remove("hit", "1.0", "end")
        self.raw_text.tag_add("hit", start, end)
        self.raw_text.tag_config("hit", background="#ff87a7", foreground="#10131a")
        self.raw_text.mark_set("insert", end)
        self.raw_text.see(start)

    def path_get(self) -> None:
        if not self.obj:
            return
        try:
            val = get_path(self.obj, self.path_edit_var.get())
            pretty = json.dumps(val, ensure_ascii=False, indent=2)
            self.path_value_var.set(pretty if len(pretty) < 1000 else "")
            self.path_output.delete("1.0", "end")
            self.path_output.insert("1.0", pretty)
        except Exception as e:
            messagebox.showerror("Path error", str(e))

    def path_set(self) -> None:
        if not self.obj:
            return
        try:
            value = parse_value(self.path_value_var.get())
            set_path(self.obj, self.path_edit_var.get(), value)
            self.mark_dirty(f"Изменено: {self.path_edit_var.get()}")
            self.refresh_all()
        except Exception as e:
            messagebox.showerror("Path error", str(e))

    def export_json(self) -> None:
        if not self.obj:
            return
        default = (self.path.stem if self.path else "save") + "_decoded.json"
        out = filedialog.asksaveasfilename(title="Сохранить JSON", defaultextension=".json", initialfile=default,
                                           filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not out:
            return
        try:
            Path(out).write_text(json.dumps(self.obj, ensure_ascii=False, indent=2), encoding="utf-8")
            self.status_var.set(f"JSON сохранён: {out}")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def save_as(self) -> None:
        if not self.obj:
            return
        default = (self.path.stem if self.path else "save") + "_edited.sodb"
        out = filedialog.asksaveasfilename(title="Сохранить .sodb", defaultextension=".sodb", initialfile=default,
                                           filetypes=[("SODB", "*.sodb"), ("JSON", "*.json"), ("All files", "*.*")])
        if not out:
            return
        try:
            compressed = not out.lower().endswith(".json")
            Path(out).write_bytes(SodbCodec.encode(self.obj, compressed=compressed, pretty=not compressed))
            self.dirty = False
            self.status_var.set(f"Сохранено: {out}")
        except Exception as e:
            messagebox.showerror("Ошибка сохранения", str(e))

    def backup_and_overwrite(self) -> None:
        if not self.obj or not self.path:
            return
        if not messagebox.askyesno("Overwrite", "Закрой игру перед заменой сейва. Создать .bak и перезаписать исходный файл?"):
            return
        try:
            bak = self.path.with_suffix(self.path.suffix + ".bak")
            i = 1
            while bak.exists():
                bak = self.path.with_suffix(self.path.suffix + f".bak{i}")
                i += 1
            shutil.copy2(self.path, bak)
            self.path.write_bytes(SodbCodec.encode(self.obj, compressed=self.compressed, pretty=not self.compressed))
            self.dirty = False
            self.status_var.set(f"Перезаписано: {self.path.name}; бэкап: {bak.name}")
        except Exception as e:
            messagebox.showerror("Ошибка overwrite", str(e))

    def on_closing(self) -> None:
        if self.dirty:
            if not messagebox.askyesno("Выход", "Есть несохранённые изменения. Выйти без сохранения?"):
                return
        self.destroy()



# ---------------------------------------------------------------------------
# v6 feature extension: validator, autobackups, full cards, item locator,
# address book and column auto-fit.
# Kept as a compact patch layer so existing v5 logic remains stable.
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    level: str
    path: str
    message: str


@dataclass
class ItemInfo:
    iid: str
    preset: str
    name: str
    owner: str
    room: str
    address: str
    position: str
    locked: str
    passcode: str
    evidence: str
    raw: Dict[str, Any]


@dataclass
class AddressRoomInfo:
    room_id: str
    address_id: str
    address: str
    room: str
    occupants: str
    company: str
    password: str
    item_count: int
    hint: str
    raw: Dict[str, Any]


def _v6_compact_json(value: Any, limit: int = 1600) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        raw = str(value)
    return raw[:limit]


def _v6_human_id_from_text(text: Any) -> Optional[int]:
    m = HUMAN_ANY_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def _v6_analyzer_init(self: SodbAnalyzer, obj: Dict[str, Any]):
    # Original __init__ with extra indexes.
    self.obj = obj
    self.evidence_by_id = {
        str(e.get("id")): e for e in obj.get("evidence", []) if isinstance(e, dict) and e.get("id") is not None
    }
    self.room_by_id = {
        r.get("id"): r for r in obj.get("rooms", []) if isinstance(r, dict) and isinstance(r.get("id"), int)
    }
    self.address_by_id = {
        a.get("id"): a for a in obj.get("addresses", []) if isinstance(a, dict) and isinstance(a.get("id"), int)
    }
    self.location_name_map: Dict[int, str] = {}
    self.name_map = {}
    self.role_map = {}
    self.roster_source = {}
    self._build_location_names()
    self._build_people_names()
    self.job_by_id = self._build_jobs()
    self.room_address_map = self._build_room_address_map()
    self.company_workers_map: Dict[int, List[Tuple[int, str, str]]] = {}
    for comp in self.company_infos():
        self.company_workers_map[comp.cid] = comp.workers


def _v6_build_location_names(self: SodbAnalyzer) -> None:
    # Case board cards sometimes preserve readable address labels like
    # "203 Lovelace View(Location694)" even when Location evidence has empty n/mpContent.
    for case in list(self.obj.get("activeCases", []) or []) + list(self.obj.get("archivedCases", []) or []):
        if not isinstance(case, dict):
            continue
        for el in case.get("caseElements", []) or []:
            if not isinstance(el, dict):
                continue
            ident = str(el.get("id") or "")
            name = str(el.get("n") or "").strip()
            m = re.match(r"Location(\d+)$", ident, re.I)
            if not m:
                m = re.search(r"\(Location(\d+)\)$", name, re.I)
            if m:
                clean = re.sub(r"\(Location\d+\)$", "", name).strip()
                if clean:
                    self.location_name_map[int(m.group(1))] = clean
    for evid, ev in self.evidence_by_id.items():
        m = re.match(r"Location(\d+)$", evid, re.I)
        if not m:
            continue
        clean = str(ev.get("n") or "").strip()
        if not clean and isinstance(ev.get("customName"), list):
            clean = " ".join(str(x) for x in ev.get("customName") if x).strip()
        if clean:
            self.location_name_map.setdefault(int(m.group(1)), clean)


def _v6_address_label(self: SodbAnalyzer, aid: Any) -> str:
    if not isinstance(aid, int) or aid < 0:
        return "—"
    name = getattr(self, "location_name_map", {}).get(aid)
    a = self.address_by_id.get(aid)
    if name:
        return f"{name} (Location{aid})"
    if not a:
        return f"Location{aid}"
    extra = []
    for key in ("name", "n", "preset", "residence", "buildingID", "building", "floor", "sale"):
        if a.get(key) not in (None, "", -1):
            extra.append(f"{key}={a.get(key)}")
    return f"Location{aid}" + (" (" + ", ".join(extra[:4]) + ")" if extra else "")


def _v6_room_label(self: SodbAnalyzer, rid: Any) -> str:
    if not isinstance(rid, int) or rid < 0:
        return "—"
    room = self.room_by_id.get(rid)
    bits = [f"Room{rid}"]
    addr = self.room_address_map.get(rid)
    if addr is not None:
        bits.append(self.address_label(addr))
    if room:
        if room.get("fID") is not None:
            bits.append(f"fID={room.get('fID')}")
        if room.get("iID") is not None:
            bits.append(f"iID={room.get('iID')}")
        if room.get("ml") is not None:
            bits.append(f"main={room.get('ml')}")
    return bits[0] + (" (" + ", ".join(bits[1:]) + ")" if len(bits) > 1 else "")


def _v6_build_room_address_map(self: SodbAnalyzer) -> Dict[int, int]:
    out: Dict[int, int] = {}

    def remember(room_id: Any, address_id: Any) -> None:
        if isinstance(room_id, int) and room_id >= 0 and isinstance(address_id, int) and address_id >= 0:
            out.setdefault(room_id, address_id)

    for c in self.obj.get("citizens", []) or []:
        if not isinstance(c, dict):
            continue
        goal = c.get("currentGoal") if isinstance(c.get("currentGoal"), dict) else {}
        if goal.get("isAddress") and isinstance(goal.get("gameLocation"), int):
            remember(goal.get("room"), goal.get("gameLocation"))
            for act in goal.get("actions", []) or []:
                if isinstance(act, dict):
                    remember(act.get("passedRoom"), goal.get("gameLocation"))
    for it in self.obj.get("interactables", []) or []:
        if not isinstance(it, dict):
            continue
        room = it.get("r")
        # Many interactables do not store address directly, but some pv/passcode/nEvKey strings contain Location ids.
        raw = _v6_compact_json(it, 700)
        m = re.search(r"Location(\d+)", raw, re.I)
        if m:
            remember(room, int(m.group(1)))
    for job in self.job_by_id.values():
        addr = job.get("secretLocation") or job.get("secretLocationAddress") or job.get("handIn")
        room = job.get("secretLocationRoom") or job.get("secretLocationNode")
        remember(room, addr)
    return out


def _v6_case_answer_value(self: SodbAnalyzer, value: Any) -> str:
    if isinstance(value, int):
        if value in self.name_map:
            return self.human_name(value)
        if value in self.room_by_id:
            return self.room_label(value)
        if value in self.address_by_id or value in getattr(self, "location_name_map", {}):
            return self.address_label(value)
        return str(value)
    if isinstance(value, str):
        mh = HUMAN_RE.match(value)
        if mh:
            return self.human_name(int(mh.group(1)))
        ml = re.match(r"Location(\d+)$", value, re.I)
        if ml:
            return self.address_label(int(ml.group(1)))
        mr = re.match(r"Room(\d+)$", value, re.I)
        if mr:
            return self.room_label(int(mr.group(1)))
    return str(value)


def _v6_pretty_case_details(self: SodbAnalyzer, cinfo: CaseInfo) -> str:
    c = cinfo.raw
    job = self.job_by_id.get(cinfo.job_id, {})
    lines = [
        f"Case #{cinfo.cid}: {cinfo.name}",
        f"Type/status: {cinfo.case_type} / {cinfo.status}",
        f"Active: {cinfo.active}, solved: {cinfo.solved}, handInValid: {c.get('handInValid')}, jobReference: {cinfo.job_id}",
        "",
        "=== READY ANSWERS / ВЫЖИМКА ===",
    ]
    if self.obj.get("currentMurderer"):
        lines.append(f"currentMurderer: {self.human_name(self.obj.get('currentMurderer'))}")
    if self.obj.get("currentVictim"):
        lines.append(f"currentVictim:   {self.human_name(self.obj.get('currentVictim'))}")
    if job:
        addr = job.get("secretLocation") or job.get("secretLocationAddress") or job.get("handIn")
        room = job.get("secretLocationRoom") or job.get("secretLocationNode")
        lines += [
            f"job preset: {job.get('presetStr') or '—'}",
            f"target/purpID: {self.human_name(job.get('purpID'))}",
            f"posterID: {self.human_name(job.get('posterID'))}",
            f"reward: {job.get('reward')}",
            f"motive: {job.get('motiveStr') or '—'}",
            f"location: {self.location_label(addr, room)}",
            f"secretLocationFurniture: {job.get('secretLocationFurniture')}",
            f"fakeNumber: {job.get('fakeNumberStr')}",
        ]
    else:
        lines.append("job: —")

    if self.obj.get("iaMurders"):
        lines += ["", "=== IA MURDERS ==="]
        for m in self.obj.get("iaMurders", []) or []:
            if isinstance(m, dict):
                lines.append(
                    f"- murderID={m.get('murderID')}; killer={self.human_name(m.get('murdererID'))}; "
                    f"victim={self.human_name(m.get('victimID'))}; weapon={m.get('weaponStr')}; "
                    f"preset={m.get('presetStr')}; MO={m.get('moStr')}; address={self.address_label(m.get('addressID'))}; state={m.get('state')}"
                )

    lines += ["", "=== RESOLVE QUESTIONS ==="]
    for q in c.get("resolveQuestions", []) or []:
        if not isinstance(q, dict):
            continue
        answers = q.get("correctAnswers") or q.get("automaticAnswers") or []
        mapped = [self._case_answer_value(a) for a in answers]
        lines.append(f"- {q.get('name') or q.get('question') or 'question'}")
        lines.append(f"  correct: {answers or '—'}")
        lines.append(f"  mapped:  {', '.join(mapped) if mapped else '—'}")
        lines.append(f"  input={q.get('input')}, valid={q.get('isValid')}, reward={q.get('reward')}")

    lines += ["", "=== CASE ELEMENTS ==="]
    for el in c.get("caseElements", []) or []:
        if isinstance(el, dict):
            lines.append(f"- {el.get('id')}: {el.get('n')}")

    if job:
        lines += ["", "=== RAW JOB ===", json.dumps(job, ensure_ascii=False, indent=2)[:100_000]]
    lines += ["", "=== RAW CASE ===", json.dumps(c, ensure_ascii=False, indent=2)[:120_000]]
    return "\n".join(lines)


def _v6_pretty_person_details(self: SodbAnalyzer, p: PersonInfo) -> str:
    goal = p.raw.get("currentGoal") if isinstance(p.raw.get("currentGoal"), dict) else {}
    home_rooms = p.raw.get("atHome") if isinstance(p.raw.get("atHome"), list) else []
    home_hint = ", ".join(self.room_label(x) for x in home_rooms[:8]) if home_rooms else "—"
    company_ids = []
    for cid, workers in getattr(self, "company_workers_map", {}).items():
        if any(hid == p.cid for hid, _n, _r in workers):
            company_ids.append(cid)
    related_cases = []
    for ci in self.case_infos():
        if ci.target_id == p.cid or f"Human{p.cid}" in _v6_compact_json(ci.raw, 5000):
            related_cases.append(f"Case{ci.cid}: {ci.name or ci.preset or ci.case_type}")
    murder_lines = []
    for m in list(self.obj.get("murders", []) or []) + list(self.obj.get("iaMurders", []) or []):
        if not isinstance(m, dict):
            continue
        if m.get("murdererID") == p.cid or m.get("victimID") == p.cid:
            murder_lines.append(
                f"murderID={m.get('murderID')}; killer={self.human_name(m.get('murdererID'))}; "
                f"victim={self.human_name(m.get('victimID'))}; weapon={m.get('weaponStr')}; preset={m.get('presetStr')}; state={m.get('state')}"
            )
    rels = self.person_relations(p.cid, limit=30)
    lines = [
        f"Human{p.cid}: {p.name or '(unknown name)'}",
        f"Role/job: {p.role or '—'}",
        f"Flags: {', '.join(p.flags) or '—'}",
        f"Personal passcode: {self.password_status_for_human(p.cid)}",
        f"Name source: {self.roster_source.get(p.cid, '—')}",
        f"Companies: {', '.join('Company'+str(x) for x in company_ids) if company_ids else '—'}",
        f"Home rooms: {home_hint}",
        f"Current position: {p.position or self._fmt_pos(p.raw.get('pos')) or '—'}",
        f"Current goal: {goal.get('preset') or p.goal or '—'}",
        f"Current location: {self.location_label(goal.get('gameLocation'), goal.get('room'))}",
        f"Investigate position: {self._fmt_pos(p.raw.get('investigatePosition')) or '—'}",
        f"Health/nerve: {p.raw.get('currentHealth')} / {p.raw.get('currentNerve')}",
        f"Wallet items: {len(p.raw.get('wallet') or []) if isinstance(p.raw.get('wallet'), list) else '—'}",
    ]
    if not self.personal_passcode_record(p.cid):
        lines += ["", "=== PASSWORD NOTE ===", "Личного type=0 passcode для этого HumanID нет в top-level passcodes. Можно задать его через вкладку «Пароли»." ]
    if related_cases:
        lines += ["", "=== RELATED CASES ==="] + [f"- {x}" for x in related_cases[:40]]
    if murder_lines:
        lines += ["", "=== MURDER / CRIME LINKS ==="] + [f"- {x}" for x in murder_lines[:40]]
    if rels:
        lines += ["", "=== RELATIONS TOP ==="]
        for r in rels:
            lines.append(f"- {self.human_name(r.dst)} | {r.kind} | {r.detail}")
    if p.seen_ids:
        lines += ["", f"=== SIGHTING CITIZENS ({len(p.seen_ids)} total, first 80) ==="]
        lines.extend([f"- {self.human_name(x)}" for x in p.seen_ids[:80]])
    if p.ties:
        lines += ["", "=== EVIDENCE KEYTIES SAMPLE ==="] + [f"- {x}" for x in p.ties]
    lines += ["", "=== RAW CITIZEN JSON ===", json.dumps(p.raw, ensure_ascii=False, indent=2)[:140_000]]
    return "\n".join(lines)


def _v6_item_infos(self: SodbAnalyzer, query: str = "", limit: int = 3000) -> List[ItemInfo]:
    q = (query or "").strip().lower()
    rows: List[ItemInfo] = []
    for it in self.obj.get("interactables", []) or []:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id", ""))
        preset = str(it.get("p") or it.get("lp") or "")
        name = str(it.get("nEvKey") or it.get("dds") or it.get("bo") or it.get("sd") or "")
        owner_id = it.get("w") if isinstance(it.get("w"), int) and it.get("w") > 0 else it.get("inv")
        owner = self.human_name(owner_id) if isinstance(owner_id, int) and owner_id > 0 else "—"
        room_id = it.get("r") if isinstance(it.get("r"), int) and it.get("r") >= 0 else None
        room = self.room_label(room_id) if room_id is not None else "—"
        addr_id = self.room_address_map.get(room_id) if isinstance(room_id, int) else None
        address = self.address_label(addr_id) if isinstance(addr_id, int) else "—"
        pos = self._fmt_pos(it.get("wPos")) or self._fmt_pos(it.get("spWPos"))
        passcode = ""
        if it.get("passcode") not in (None, "", -1):
            passcode = str(it.get("passcode"))
        elif isinstance(it.get("pv"), list):
            pcs = [str(x.get("str") or int(x.get("value"))) for x in it.get("pv", []) if isinstance(x, dict) and (x.get("str") or x.get("value"))]
            passcode = ", ".join(pcs[:4])
        ev = str(it.get("nEvKey") or "")
        rawtxt = _v6_compact_json(it, 3500)
        hay = f"{iid} {preset} {name} {owner} {room} {address} {pos} {passcode} {ev} {rawtxt}".lower()
        if q and q not in hay:
            continue
        rows.append(ItemInfo(
            iid=iid,
            preset=preset or "—",
            name=name or "—",
            owner=owner,
            room=room,
            address=address,
            position=pos or "—",
            locked=str(bool(it.get("locked"))),
            passcode=passcode or "—",
            evidence=ev or "—",
            raw=it,
        ))
        if len(rows) >= limit:
            break
    return rows


def _v6_address_room_infos(self: SodbAnalyzer, query: str = "", limit: int = 5000) -> List[AddressRoomInfo]:
    q = (query or "").strip().lower()
    occupants_by_addr: Dict[int, List[str]] = {}
    occupants_by_room: Dict[int, List[str]] = {}
    companies_by_addr: Dict[int, List[str]] = {}
    item_count_by_room: Dict[int, int] = {}
    passcode_by_room: Dict[int, str] = {}

    for p in self.person_infos():
        goal = p.raw.get("currentGoal") if isinstance(p.raw.get("currentGoal"), dict) else {}
        addr = goal.get("gameLocation") if goal.get("isAddress") and isinstance(goal.get("gameLocation"), int) else None
        room = goal.get("room") if isinstance(goal.get("room"), int) else None
        label = self.human_name(p.cid)
        if isinstance(addr, int):
            occupants_by_addr.setdefault(addr, []).append(label)
        if isinstance(room, int):
            occupants_by_room.setdefault(room, []).append(label)

    for comp in self.company_infos():
        # Use worker current goals as a weak but useful hint for company location.
        worker_addr_counts: Dict[int, int] = {}
        for hid, _n, _r in comp.workers:
            citizen = next((c for c in self.obj.get("citizens", []) if isinstance(c, dict) and c.get("id") == hid), None)
            goal = citizen.get("currentGoal") if isinstance(citizen, dict) and isinstance(citizen.get("currentGoal"), dict) else {}
            addr = goal.get("gameLocation") if goal.get("isAddress") and isinstance(goal.get("gameLocation"), int) else None
            if isinstance(addr, int):
                worker_addr_counts[addr] = worker_addr_counts.get(addr, 0) + 1
        if worker_addr_counts:
            best = max(worker_addr_counts.items(), key=lambda kv: kv[1])[0]
            companies_by_addr.setdefault(best, []).append(f"Company{comp.cid}")

    for it in self.obj.get("interactables", []) or []:
        if not isinstance(it, dict):
            continue
        r = it.get("r")
        if isinstance(r, int) and r >= 0:
            item_count_by_room[r] = item_count_by_room.get(r, 0) + 1
    for pc in self.password_infos():
        if pc.pc_type == 1 and pc.target_id >= 0:
            passcode_by_room[pc.target_id] = pc.code

    all_room_ids = set(self.room_by_id.keys()) | set(self.room_address_map.keys()) | set(item_count_by_room.keys()) | set(occupants_by_room.keys()) | set(passcode_by_room.keys())
    rows: List[AddressRoomInfo] = []
    for rid in sorted(x for x in all_room_ids if isinstance(x, int)):
        aid = self.room_address_map.get(rid)
        address = self.address_label(aid) if isinstance(aid, int) else "—"
        room = self.room_label(rid)
        occupants = ", ".join((occupants_by_room.get(rid) or occupants_by_addr.get(aid, []))[:8]) if isinstance(aid, int) else ", ".join(occupants_by_room.get(rid, [])[:8])
        company = ", ".join(companies_by_addr.get(aid, [])[:6]) if isinstance(aid, int) else ""
        password = passcode_by_room.get(rid, "—")
        hint = "currentGoal/room map" if isinstance(aid, int) else "room only"
        raw = self.room_by_id.get(rid, {})
        hay = f"Room{rid} Location{aid} {address} {room} {occupants} {company} {password} {hint}".lower()
        if q and q not in hay:
            continue
        rows.append(AddressRoomInfo(
            room_id=f"Room{rid}",
            address_id=f"Location{aid}" if isinstance(aid, int) else "—",
            address=address,
            room=room,
            occupants=occupants or "—",
            company=company or "—",
            password=password,
            item_count=item_count_by_room.get(rid, 0),
            hint=hint,
            raw=raw,
        ))
        if len(rows) >= limit:
            break
    return rows


def _v6_validate_save(self: SodbAnalyzer) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    def add(level: str, path: str, msg: str) -> None:
        issues.append(ValidationIssue(level, path, msg))

    # JSON encode/roundtrip sanity.
    try:
        packed = SodbCodec.encode(self.obj, compressed=True, pretty=False)
        decoded = SodbCodec.decode_bytes_for_validation(packed)
        if not isinstance(decoded, dict):
            add("ERROR", "$", "Encoded/decoded root is not object")
        else:
            add("OK", "$", "JSON can be encoded back to .sodb and decoded again")
    except Exception as e:
        add("ERROR", "$", f"Encode/decode roundtrip failed: {e}")

    required_lists = ["citizens", "evidence", "interactables", "activeCases", "archivedCases", "passcodes", "rooms", "addresses", "companies"]
    for key in required_lists:
        if key not in self.obj:
            add("WARN", key, "Missing top-level section")
        elif not isinstance(self.obj.get(key), list):
            add("ERROR", key, f"Expected list, got {type(self.obj.get(key)).__name__}")
        else:
            add("OK", key, f"List length: {len(self.obj.get(key) or [])}")

    for key in ["money", "lockpicks", "socCredit", "health", "nourishment", "hydration", "energy", "hygiene"]:
        if key in self.obj:
            v = self.obj.get(key)
            if not isinstance(v, (int, float)):
                add("WARN", key, f"Unexpected value type: {type(v).__name__}")
            elif key in {"health", "nourishment", "hydration", "energy", "hygiene"} and not (-10 <= float(v) <= 10):
                add("WARN", key, f"Value looks unusual: {v}")

    def unique_ids(section: str) -> set:
        ids = []
        for idx, item in enumerate(self.obj.get(section, []) or []):
            if isinstance(item, dict):
                ids.append(item.get("id"))
            else:
                add("ERROR", f"{section}[{idx}]", "Item is not object")
        dup = {x for x in ids if x is not None and ids.count(x) > 1}
        if dup:
            add("WARN", section, f"Duplicate ids: {list(dup)[:10]}")
        return {x for x in ids if isinstance(x, int)}

    citizens = unique_ids("citizens")
    rooms = unique_ids("rooms")
    addresses = unique_ids("addresses")
    unique_ids("interactables")
    if citizens:
        add("OK", "citizens.id", f"Unique-ish citizens: {len(citizens)}")

    for field in ("currentMurderer", "currentVictim"):
        val = self.obj.get(field)
        if isinstance(val, int) and val > 0 and val not in citizens:
            add("WARN", field, f"Human{val} is not present in citizens")

    bad_pc = 0
    for idx, pc in enumerate(self.obj.get("passcodes", []) or []):
        if not isinstance(pc, dict):
            bad_pc += 1
            add("ERROR", f"passcodes[{idx}]", "Passcode is not object")
            continue
        digits = pc.get("digits")
        if not isinstance(digits, list) or not digits or not all(isinstance(x, int) and 0 <= x <= 9 for x in digits):
            bad_pc += 1
            add("WARN", f"passcodes[{idx}].digits", "Digits are missing or malformed")
        if pc.get("type") == 0 and isinstance(pc.get("id"), int) and pc.get("id") not in citizens:
            add("WARN", f"passcodes[{idx}].id", f"Human{pc.get('id')} not found in citizens")
    if bad_pc == 0:
        add("OK", "passcodes", "Passcodes shape looks OK")

    bad_rooms = 0
    for idx, it in enumerate(self.obj.get("interactables", []) or []):
        if not isinstance(it, dict):
            continue
        r = it.get("r")
        if isinstance(r, int) and r >= 0 and rooms and r not in rooms:
            bad_rooms += 1
            if bad_rooms <= 20:
                add("WARN", f"interactables[{idx}].r", f"Room{r} not found in rooms")
    if bad_rooms == 0:
        add("OK", "interactables.r", "Room references look OK")
    else:
        add("WARN", "interactables.r", f"Total bad room refs: {bad_rooms}")

    for idx, ci in enumerate(self.case_infos()):
        if ci.target_id and ci.target_id not in citizens:
            add("WARN", f"case[{idx}].target", f"Target Human{ci.target_id} not found in citizens")
    return issues


@staticmethod
def _v6_decode_bytes_for_validation(data: bytes) -> Any:
    if len(data) > 4 and brotli is not None:
        raw = brotli.decompress(data[:-4]).decode("utf-8-sig")
        return json.loads(raw)
    raw = data.decode("utf-8-sig")
    return json.loads(raw)


# Attach analyzer extensions.
SodbAnalyzer.__init__ = _v6_analyzer_init
SodbAnalyzer._build_location_names = _v6_build_location_names
SodbAnalyzer.address_label = _v6_address_label
SodbAnalyzer.room_label = _v6_room_label
SodbAnalyzer._build_room_address_map = _v6_build_room_address_map
SodbAnalyzer._case_answer_value = _v6_case_answer_value
SodbAnalyzer.pretty_case_details = _v6_pretty_case_details
SodbAnalyzer.pretty_person_details = _v6_pretty_person_details
SodbAnalyzer.item_infos = _v6_item_infos
SodbAnalyzer.address_room_infos = _v6_address_room_infos
SodbAnalyzer.validate_save = _v6_validate_save
SodbCodec.decode_bytes_for_validation = _v6_decode_bytes_for_validation


def _v6_make_tree(parent: tk.Widget, columns: Tuple[str, ...], specs: List[Tuple[str, int]], bind_select=None) -> ttk.Treeview:
    frame = ttk.Frame(parent, style="Panel.TFrame")
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=columns, show="headings")
    y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
    for col, width in specs:
        tree.heading(col, text=col)
        tree.column(col, width=width, minwidth=40, anchor="w", stretch=True)
    tree.grid(row=0, column=0, sticky="nsew")
    y.grid(row=0, column=1, sticky="ns")
    x.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    if bind_select:
        tree.bind("<<TreeviewSelect>>", bind_select)
    return tree


def _v6_build_ui(self: SodbEditorApp) -> None:
    top = ttk.Frame(self)
    top.pack(fill="x", padx=14, pady=(12, 6))
    ttk.Label(top, text="Shadows of Doubt Save Editor", style="Title.TLabel").pack(side="left")
    ttk.Label(top, text="  .sodb → JSON → анализ/редактирование → .sodb", style="Hint.TLabel").pack(side="left", padx=8)

    bar = ttk.Frame(self)
    bar.pack(fill="x", padx=14, pady=6)
    self.path_var = tk.StringVar()
    ttk.Entry(bar, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
    ttk.Button(bar, text="Открыть файл", command=self.open_file).pack(side="left", padx=3)
    ttk.Button(bar, text="Расшифровать", style="Accent.TButton", command=self.decode_current).pack(side="left", padx=3)
    ttk.Button(bar, text="Проверить сейв", command=self.run_validation).pack(side="left", padx=3)
    ttk.Button(bar, text="Бэкап сейчас", command=self.create_manual_backup).pack(side="left", padx=3)
    ttk.Button(bar, text="Очистить", command=self.clear_state).pack(side="left", padx=3)
    ttk.Button(bar, text="Сбросить правки", command=self.reset_to_loaded).pack(side="left", padx=3)
    ttk.Button(bar, text="?", width=3, command=self.show_help).pack(side="left", padx=3)
    ttk.Button(bar, text="Сохранить как .sodb", command=self.save_as).pack(side="left", padx=3)
    ttk.Button(bar, text="Backup + overwrite", command=self.backup_and_overwrite).pack(side="left", padx=3)
    ttk.Button(bar, text="Экспорт JSON", command=self.export_json).pack(side="left", padx=3)
    ttk.Button(bar, text="Экспорт CSV", command=self.export_current_table_csv).pack(side="left", padx=3)

    self.status_var = tk.StringVar(value="Открой .sodb файл. Перед overwrite лучше закрыть игру: автобэкап создаётся в backups рядом с сейвом.")
    ttk.Label(self, textvariable=self.status_var, style="Hint.TLabel").pack(fill="x", padx=14, pady=(0, 8))

    self.nb = ttk.Notebook(self)
    self.nb.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    self._build_dashboard_tab()
    self._build_validation_backups_tab()
    self._build_cases_tab()
    self._build_people_tab()
    self._build_passwords_tab()
    self._build_criminals_tab()
    self._build_relation_graph_tab()
    self._build_search_tab()
    self._build_items_tab()
    self._build_addresses_tab()
    self._build_companies_tab()
    self._build_raw_tab()
    self._build_path_editor_tab()
    self.bind("<Configure>", lambda _e: self.after(200, self.autofit_visible_tree))


def _v6_build_validation_backups_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.nb, style="Panel.TFrame")
    self.nb.add(tab, text="Валидатор / бэкапы")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Button(toolbar, text="Проверить сейв", command=self.run_validation).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Создать бэкап сейчас", command=self.create_manual_backup).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Обновить список бэкапов", command=self.populate_backups).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Восстановить выбранный", command=self.restore_selected_backup).pack(side="left", padx=4)
    ttk.Label(toolbar, text="Overwrite теперь делает timestamp-бэкап в папке backups.", style="Hint.TLabel").pack(side="left", padx=12)

    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Frame(paned, style="Panel.TFrame")
    right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3)
    paned.add(right, weight=2)

    ttk.Label(left, text="Проверка сейва", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
    self.validation_tree = _v6_make_tree(left, ("level", "path", "message"), [("level", 70), ("path", 220), ("message", 620)], self.on_validation_select)
    ttk.Label(right, text="Бэкапы", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
    self.backups_tree = _v6_make_tree(right, ("file", "size", "modified"), [("file", 330), ("size", 90), ("modified", 160)], self.on_backup_select)
    self.validation_details = self._text(right)


def _v6_build_items_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.nb, style="Panel.TFrame")
    self.nb.add(tab, text="Предметы / где лежит")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="Предмет / ID / владелец / RoomID:").pack(side="left")
    self.items_filter = tk.StringVar()
    ttk.Entry(toolbar, textvariable=self.items_filter, width=46).pack(side="left", padx=6)
    self.items_filter.trace_add("write", lambda *_: self.populate_items())
    ttk.Button(toolbar, text="Экспорт CSV", command=self.export_items_csv).pack(side="left", padx=4)

    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Frame(paned, style="Panel.TFrame")
    right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3)
    paned.add(right, weight=2)
    cols = ("id", "preset", "name", "owner", "room", "address", "pos", "locked", "passcode", "evidence")
    specs = [("id", 90), ("preset", 170), ("name", 180), ("owner", 180), ("room", 230), ("address", 230), ("pos", 210), ("locked", 70), ("passcode", 100), ("evidence", 140)]
    self.items_tree = _v6_make_tree(left, cols, specs, self.on_item_select)
    self.item_details = self._text(right)
    self._items_cache: List[ItemInfo] = []


def _v6_build_addresses_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.nb, style="Panel.TFrame")
    self.nb.add(tab, text="Адреса / комнаты")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="RoomID / LocationID / житель / компания:").pack(side="left")
    self.address_filter = tk.StringVar()
    ttk.Entry(toolbar, textvariable=self.address_filter, width=44).pack(side="left", padx=6)
    self.address_filter.trace_add("write", lambda *_: self.populate_addresses())
    ttk.Button(toolbar, text="Экспорт CSV", command=self.export_addresses_csv).pack(side="left", padx=4)

    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Frame(paned, style="Panel.TFrame")
    right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3)
    paned.add(right, weight=2)
    cols = ("room", "address_id", "address", "occupants", "company", "password", "items", "hint")
    specs = [("room", 90), ("address_id", 100), ("address", 290), ("occupants", 300), ("company", 180), ("password", 90), ("items", 70), ("hint", 180)]
    self.address_tree = _v6_make_tree(left, cols, specs, self.on_address_select)
    self.address_details = self._text(right)
    self._address_cache: List[AddressRoomInfo] = []


def _v6_refresh_all(self: SodbEditorApp) -> None:
    if not self.obj or not self.analyzer:
        return
    self.fill_stats()
    self.dashboard_text.delete("1.0", "end")
    self.dashboard_text.insert("1.0", self.analyzer.dashboard_text())
    self.populate_cases()
    self.populate_people()
    self.populate_passwords()
    self.populate_criminals()
    self.populate_companies()
    self.populate_items()
    self.populate_addresses()
    self.populate_backups()
    if hasattr(self, "search_tree"):
        self.search_tree.delete(*self.search_tree.get_children())
        self._search_results_cache = []
    if hasattr(self, "search_details"):
        self.search_details.delete("1.0", "end")
    if hasattr(self, "graph_canvas"):
        self.graph_canvas.delete("all")
    if hasattr(self, "graph_tree"):
        self.graph_tree.delete(*self.graph_tree.get_children())
    self.run_validation(silent=True)
    self.show_raw_preview()
    self.after(100, self.autofit_all_trees)


def _v6_clear_state(self: SodbEditorApp) -> None:
    if self.dirty:
        if not messagebox.askyesno("Очистить", "Есть несохранённые изменения. Очистить окно без сохранения?"):
            return
    self.path = None
    self.obj = None
    self.initial_obj = None
    self.analyzer = None
    self.codec_note = ""
    self.compressed = True
    self.dirty = False
    self.raw_loaded_full = False
    self.path_var.set("")
    for var in getattr(self, "stat_vars", {}).values():
        var.set("")
    tree_names = (
        "cases_tree", "people_tree", "passwords_tree", "criminals_tree", "graph_tree", "search_tree", "companies_tree",
        "validation_tree", "backups_tree", "items_tree", "address_tree",
    )
    for tree_name in tree_names:
        tree = getattr(self, tree_name, None)
        if tree is not None:
            try: tree.delete(*tree.get_children())
            except Exception: pass
    text_names = (
        "dashboard_text", "case_details", "person_details", "password_details", "search_details", "company_details", "raw_text", "path_output",
        "validation_details", "item_details", "address_details",
    )
    for text_name in text_names:
        txt = getattr(self, text_name, None)
        if txt is not None:
            try: txt.delete("1.0", "end")
            except Exception: pass
    for var_name, value in (("people_filter", ""), ("password_filter", ""), ("graph_query", ""), ("deep_search_var", ""), ("companies_filter", ""), ("items_filter", ""), ("address_filter", ""), ("raw_search_var", ""), ("path_edit_var", "money"), ("path_value_var", "")):
        if hasattr(self, var_name):
            getattr(self, var_name).set(value)
    if hasattr(self, "graph_canvas"):
        self.graph_canvas.delete("all")
    self._items_cache = []
    self._address_cache = []
    self.status_var.set("Окно очищено. Открой .sodb файл.")


def _v6_run_validation(self: SodbEditorApp, silent: bool = False) -> None:
    if not self.analyzer:
        if not silent:
            messagebox.showinfo("Валидатор", "Сначала открой сейв.")
        return
    issues = self.analyzer.validate_save()
    if hasattr(self, "validation_tree"):
        self.validation_tree.delete(*self.validation_tree.get_children())
        for i, issue in enumerate(issues):
            self.validation_tree.insert("", "end", iid=str(i), values=(issue.level, issue.path, issue.message))
    errors = sum(1 for x in issues if x.level == "ERROR")
    warns = sum(1 for x in issues if x.level == "WARN")
    oks = sum(1 for x in issues if x.level == "OK")
    msg = f"Валидация: OK={oks}, WARN={warns}, ERROR={errors}"
    self.status_var.set(msg)
    if hasattr(self, "validation_details"):
        self.validation_details.delete("1.0", "end")
        self.validation_details.insert("1.0", msg + "\n\n" + "\n".join(f"[{x.level}] {x.path}: {x.message}" for x in issues))
    if not silent and errors:
        messagebox.showwarning("Валидатор", msg + "\nПеред overwrite лучше исправить ERROR или сохранить копией.")


def _v6_on_validation_select(self: SodbEditorApp, _event=None) -> None:
    if not hasattr(self, "validation_tree") or not hasattr(self, "validation_details"):
        return
    sel = self.validation_tree.selection()
    if not sel:
        return
    vals = self.validation_tree.item(sel[0], "values")
    self.validation_details.delete("1.0", "end")
    self.validation_details.insert("1.0", f"Level: {vals[0]}\nPath: {vals[1]}\nMessage: {vals[2]}")


def _v6_backup_root(self: SodbEditorApp) -> Optional[Path]:
    if not self.path:
        return None
    return self.path.parent / "backups" / self.path.stem


def _v6_make_timestamped_backup(self: SodbEditorApp, reason: str = "auto") -> Optional[Path]:
    if not self.path or not self.path.exists():
        return None
    root = self._backup_root()
    if root is None:
        return None
    root.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reason = re.sub(r"[^A-Za-z0-9_-]+", "_", reason).strip("_") or "backup"
    out = root / f"{self.path.stem}_{ts}_{safe_reason}{self.path.suffix}"
    shutil.copy2(self.path, out)
    return out


def _v6_create_manual_backup(self: SodbEditorApp) -> None:
    if not self.path:
        messagebox.showinfo("Бэкап", "Сначала открой файл сейва.")
        return
    try:
        out = self._make_timestamped_backup("manual")
        self.populate_backups()
        self.status_var.set(f"Бэкап создан: {out}")
    except Exception as e:
        messagebox.showerror("Бэкап", str(e))


def _v6_populate_backups(self: SodbEditorApp) -> None:
    if not hasattr(self, "backups_tree"):
        return
    self.backups_tree.delete(*self.backups_tree.get_children())
    root = self._backup_root()
    if not root or not root.exists():
        return
    files = sorted([p for p in root.iterdir() if p.is_file()], key=lambda x: x.stat().st_mtime, reverse=True)
    from datetime import datetime
    for p in files[:500]:
        st = p.stat()
        self.backups_tree.insert("", "end", iid=str(p), values=(p.name, f"{st.st_size/1024:.1f} KB", datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")))


def _v6_on_backup_select(self: SodbEditorApp, _event=None) -> None:
    if not hasattr(self, "backups_tree") or not hasattr(self, "validation_details"):
        return
    sel = self.backups_tree.selection()
    if not sel:
        return
    p = Path(sel[0])
    self.validation_details.delete("1.0", "end")
    self.validation_details.insert("1.0", f"Backup file:\n{p}\n\nНажми «Восстановить выбранный», чтобы заменить текущий сейв этим файлом.")


def _v6_restore_selected_backup(self: SodbEditorApp) -> None:
    if not self.path or not hasattr(self, "backups_tree"):
        return
    sel = self.backups_tree.selection()
    if not sel:
        messagebox.showinfo("Бэкап", "Выбери бэкап в таблице.")
        return
    src = Path(sel[0])
    if not src.exists():
        messagebox.showerror("Бэкап", "Файл бэкапа не найден.")
        return
    if not messagebox.askyesno("Восстановить", f"Заменить текущий сейв файлом:\n{src.name}?\n\nПеред заменой будет создан отдельный pre_restore бэкап."):
        return
    try:
        self._make_timestamped_backup("pre_restore")
        shutil.copy2(src, self.path)
        self.decode_current()
        self.status_var.set(f"Восстановлен бэкап: {src.name}")
    except Exception as e:
        messagebox.showerror("Бэкап", str(e))


def _v6_backup_and_overwrite(self: SodbEditorApp) -> None:
    if not self.obj or not self.path:
        return
    if not messagebox.askyesno("Overwrite", "Закрой игру перед заменой сейва. Создать timestamp-бэкап в backups и перезаписать исходный файл?"):
        return
    try:
        bak = self._make_timestamped_backup("overwrite")
        self.run_validation(silent=True)
        self.path.write_bytes(SodbCodec.encode(self.obj, compressed=self.compressed, pretty=not self.compressed))
        self.dirty = False
        self.populate_backups()
        self.status_var.set(f"Перезаписано: {self.path.name}; бэкап: {bak.name if bak else '—'}")
    except Exception as e:
        messagebox.showerror("Ошибка overwrite", str(e))


def _v6_populate_items(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "items_tree"):
        return
    q = self.items_filter.get() if hasattr(self, "items_filter") else ""
    self._items_cache = self.analyzer.item_infos(q, limit=1500)
    self.items_tree.delete(*self.items_tree.get_children())
    for idx, it in enumerate(self._items_cache):
        self.items_tree.insert("", "end", iid=str(idx), values=(it.iid, it.preset, it.name, it.owner, it.room, it.address, it.position, it.locked, it.passcode, it.evidence))
    self.after(60, lambda: self.autofit_tree(self.items_tree))


def _v6_on_item_select(self: SodbEditorApp, _event=None) -> None:
    if not hasattr(self, "items_tree") or not hasattr(self, "item_details"):
        return
    sel = self.items_tree.selection()
    if not sel:
        return
    idx = int(sel[0])
    if idx < 0 or idx >= len(getattr(self, "_items_cache", [])):
        return
    it = self._items_cache[idx]
    self.item_details.delete("1.0", "end")
    self.item_details.insert("1.0", f"Item {it.iid}\nPreset: {it.preset}\nName: {it.name}\nOwner: {it.owner}\nRoom: {it.room}\nAddress: {it.address}\nPosition: {it.position}\nLocked: {it.locked}\nPasscode: {it.passcode}\nEvidence: {it.evidence}\n\nRAW:\n" + json.dumps(it.raw, ensure_ascii=False, indent=2)[:180_000])


def _v6_populate_addresses(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "address_tree"):
        return
    q = self.address_filter.get() if hasattr(self, "address_filter") else ""
    self._address_cache = self.analyzer.address_room_infos(q, limit=2000)
    self.address_tree.delete(*self.address_tree.get_children())
    for idx, r in enumerate(self._address_cache):
        self.address_tree.insert("", "end", iid=str(idx), values=(r.room_id, r.address_id, r.address, r.occupants, r.company, r.password, r.item_count, r.hint))
    self.after(60, lambda: self.autofit_tree(self.address_tree))


def _v6_on_address_select(self: SodbEditorApp, _event=None) -> None:
    if not hasattr(self, "address_tree") or not hasattr(self, "address_details"):
        return
    sel = self.address_tree.selection()
    if not sel:
        return
    idx = int(sel[0])
    if idx < 0 or idx >= len(getattr(self, "_address_cache", [])):
        return
    r = self._address_cache[idx]
    self.address_details.delete("1.0", "end")
    self.address_details.insert("1.0", f"{r.room_id} → {r.address_id}\nAddress: {r.address}\nOccupants: {r.occupants}\nCompany: {r.company}\nRoom password: {r.password}\nItems in room: {r.item_count}\nHint source: {r.hint}\n\nRAW ROOM:\n" + json.dumps(r.raw, ensure_ascii=False, indent=2)[:160_000])


def _v6_export_current_table_csv(self: SodbEditorApp) -> None:
    current = self.nb.select()
    tab_text = self.nb.tab(current, "text") if current else "table"
    mapping = {
        "Кейсы": (getattr(self, "cases_tree", None), "cases.csv"),
        "Люди / связи": (getattr(self, "people_tree", None), "people.csv"),
        "Пароли": (getattr(self, "passwords_tree", None), "passcodes.csv"),
        "Убийцы / криминалы": (getattr(self, "criminals_tree", None), "criminals.csv"),
        "Граф связей": (getattr(self, "graph_tree", None), "relations.csv"),
        "Поиск evidence/interactables": (getattr(self, "search_tree", None), "search_results.csv"),
        "Предметы / где лежит": (getattr(self, "items_tree", None), "items.csv"),
        "Адреса / комнаты": (getattr(self, "address_tree", None), "addresses_rooms.csv"),
        "Компании / работы": (getattr(self, "companies_tree", None), "companies.csv"),
        "Валидатор / бэкапы": (getattr(self, "validation_tree", None), "validation.csv"),
    }
    tree, default_name = mapping.get(tab_text, (None, "table.csv"))
    if tree is None:
        messagebox.showinfo("CSV", "На текущей вкладке нет таблицы для экспорта.")
        return
    self._tree_to_csv(tree, default_name)


def _v6_export_items_csv(self: SodbEditorApp) -> None:
    if hasattr(self, "items_tree"):
        self._tree_to_csv(self.items_tree, "items.csv")


def _v6_export_addresses_csv(self: SodbEditorApp) -> None:
    if hasattr(self, "address_tree"):
        self._tree_to_csv(self.address_tree, "addresses_rooms.csv")


def _v6_autofit_tree(self: SodbEditorApp, tree: ttk.Treeview, max_rows: int = 200) -> None:
    try:
        import tkinter.font as tkfont
        font = tkfont.nametofont("TkDefaultFont")
        cols = list(tree["columns"])
        if not cols:
            return
        widths: Dict[str, int] = {}
        for col in cols:
            text = str(tree.heading(col, "text") or col)
            widths[col] = max(55, font.measure(text) + 28)
        for row_i, iid in enumerate(tree.get_children("")):
            if row_i >= max_rows:
                break
            vals = tree.item(iid, "values")
            for col, val in zip(cols, vals):
                sample = str(val)
                if len(sample) > 90:
                    sample = sample[:90] + "…"
                widths[col] = max(widths[col], min(520, font.measure(sample) + 34))
        visible = max(tree.winfo_width() - 30, 600)
        total = sum(widths.values())
        if total > visible:
            scale = visible / total
            for col in cols:
                widths[col] = max(65, int(widths[col] * scale))
        for col in cols:
            tree.column(col, width=widths[col], stretch=True)
    except Exception:
        pass


def _v6_autofit_all_trees(self: SodbEditorApp) -> None:
    for name in ("cases_tree", "people_tree", "passwords_tree", "criminals_tree", "graph_tree", "search_tree", "companies_tree", "validation_tree", "backups_tree", "items_tree", "address_tree"):
        tree = getattr(self, name, None)
        if tree is not None:
            self.autofit_tree(tree)


def _v6_autofit_visible_tree(self: SodbEditorApp) -> None:
    current = self.nb.select() if hasattr(self, "nb") else ""
    tab_text = self.nb.tab(current, "text") if current else ""
    by_tab = {
        "Кейсы": "cases_tree", "Люди / связи": "people_tree", "Пароли": "passwords_tree",
        "Убийцы / криминалы": "criminals_tree", "Граф связей": "graph_tree",
        "Поиск evidence/interactables": "search_tree", "Предметы / где лежит": "items_tree",
        "Адреса / комнаты": "address_tree", "Компании / работы": "companies_tree",
        "Валидатор / бэкапы": "validation_tree",
    }
    tree = getattr(self, by_tab.get(tab_text, ""), None)
    if tree is not None:
        self.autofit_tree(tree)


def _v6_show_help(self: SodbEditorApp) -> None:
    text = """SODB Save Editor — справка

Основной поток
- Открыть файл: выбрать .sodb или несжатый .json-сейв.
- Расшифровать: распаковать Brotli-сжатый .sodb и разобрать JSON.
- Проверить сейв: валидатор формы JSON, ссылок и passcodes.
- Backup + overwrite: перед заменой исходника автоматически создаёт timestamp-бэкап в backups/<имя_сейва>/.
- Бэкап сейчас: создать ручной бэкап без сохранения изменений.
- Сбросить правки: вернуть состояние сразу после открытия файла.

Диапазоны основных статов
money, lockpicks, socCredit: целые числа, обычно 0+.
health, nourishment, hydration, energy, hygiene: безопасно держать в диапазоне 0.0–1.0. Игра может хранить/принять и другие значения, но это уже риск.

Кейсы
Вкладка «Кейсы» теперь показывает готовую выжимку: убийца/жертва, цель job, место, reward, resolveQuestions и mapped answers. Если правильные ответы есть в JSON, они будут показаны прямо в карточке.

Люди
Вкладка «Люди / связи» показывает полную карточку человека: работа, компании, домашние комнаты, текущая локация, пароль, связанные кейсы, убийства/жертвы, связи и raw JSON.

Предметы / где лежит
Ищи SealedEnvelope, SniperRifle, Vmail, Note, HumanID, RoomID или ID interactable. Таблица покажет владельца, комнату, адрес, координаты, locked/passcode.

Адреса / комнаты
RoomID → LocationID/адрес строится по currentGoal, known room map, passcodes и interactables. Это эвристика: если в сейве нет прямого адреса комнаты, будет показан room only.

Пароли
Вкладка показывает top-level passcodes. Это обычно уже известные/сгенерированные коды, не гарантированно все пароли всех NPC. Можно задать type=0 пароль человеку вручную.

CSV
Кнопка «Экспорт CSV» работает для текущей таблицы, включая предметы, адреса, валидатор, кейсы, людей, компании и пароли.
"""
    win = tk.Toplevel(self)
    win.title("Справка / ?")
    win.geometry("860x740")
    win.configure(bg="#10131a")
    win.transient(self)
    win.grab_set()
    frame = ttk.Frame(win, style="Panel.TFrame")
    frame.pack(fill="both", expand=True, padx=12, pady=12)
    txt = tk.Text(frame, wrap="word", bg="#0f1320", fg="#d8dde8", insertbackground="#ffffff",
                  selectbackground="#8a3554", font=("Segoe UI", 10), relief="flat")
    y = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=y.set)
    txt.grid(row=0, column=0, sticky="nsew")
    y.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    txt.insert("1.0", text)
    txt.configure(state="disabled")
    ttk.Button(win, text="Закрыть", command=win.destroy).pack(pady=(0, 12))


# Attach GUI extensions/overrides.
SodbEditorApp._build_ui = _v6_build_ui
SodbEditorApp._build_validation_backups_tab = _v6_build_validation_backups_tab
SodbEditorApp._build_items_tab = _v6_build_items_tab
SodbEditorApp._build_addresses_tab = _v6_build_addresses_tab
SodbEditorApp.refresh_all = _v6_refresh_all
SodbEditorApp.clear_state = _v6_clear_state
SodbEditorApp.run_validation = _v6_run_validation
SodbEditorApp.on_validation_select = _v6_on_validation_select
SodbEditorApp._backup_root = _v6_backup_root
SodbEditorApp._make_timestamped_backup = _v6_make_timestamped_backup
SodbEditorApp.create_manual_backup = _v6_create_manual_backup
SodbEditorApp.populate_backups = _v6_populate_backups
SodbEditorApp.on_backup_select = _v6_on_backup_select
SodbEditorApp.restore_selected_backup = _v6_restore_selected_backup
SodbEditorApp.backup_and_overwrite = _v6_backup_and_overwrite
SodbEditorApp.populate_items = _v6_populate_items
SodbEditorApp.on_item_select = _v6_on_item_select
SodbEditorApp.populate_addresses = _v6_populate_addresses
SodbEditorApp.on_address_select = _v6_on_address_select
SodbEditorApp.export_current_table_csv = _v6_export_current_table_csv
SodbEditorApp.export_items_csv = _v6_export_items_csv
SodbEditorApp.export_addresses_csv = _v6_export_addresses_csv
SodbEditorApp.autofit_tree = _v6_autofit_tree
SodbEditorApp.autofit_all_trees = _v6_autofit_all_trees
SodbEditorApp.autofit_visible_tree = _v6_autofit_visible_tree
SodbEditorApp.show_help = _v6_show_help

# ---------------------------------------------------------------------------
# v7 feature extension: optional Detective Mode, global search, timeline, map,
# inventory/sync/apartment viewers, city stats, JSON inspector and lighter UI.
# Detective Mode is hidden by default and can be enabled from the top toolbar.
# ---------------------------------------------------------------------------


def _v7_pos_tuple(value: Any) -> Optional[Tuple[float, float, float]]:
    if isinstance(value, dict):
        try:
            return (float(value.get("x", 0.0)), float(value.get("y", 0.0)), float(value.get("z", 0.0)))
        except Exception:
            return None
    return None


def _v7_pos_label(value: Any) -> str:
    p = _v7_pos_tuple(value)
    if not p:
        return "—"
    return f"{p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f}"


def _v7_short(value: Any, limit: int = 900) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(value)
    text = text.replace("\n", " ")
    return text[:limit]


def _v7_hay(*parts: Any) -> str:
    return " ".join(str(x) for x in parts if x is not None).lower()


def _v7_walk_limited(value: Any, path: str = "$", depth: int = 0, max_depth: int = 6, max_items: int = 3500):
    """Yield (path, scalar_or_compact) pairs. Conservative, to keep search responsive."""
    if max_items <= 0:
        return
    if depth > max_depth:
        yield path, _v7_short(value, 350)
        return
    if isinstance(value, dict):
        count = 0
        for k, v in value.items():
            if count >= 120:
                break
            count += 1
            new_path = f"{path}.{k}" if path != "$" else str(k)
            if isinstance(v, (dict, list)):
                yield from _v7_walk_limited(v, new_path, depth + 1, max_depth, max_items - count)
            else:
                yield new_path, v
    elif isinstance(value, list):
        for i, v in enumerate(value[:120]):
            new_path = f"{path}[{i}]"
            if isinstance(v, (dict, list)):
                yield from _v7_walk_limited(v, new_path, depth + 1, max_depth, max_items - i)
            else:
                yield new_path, v
    else:
        yield path, value


# ----------------------------- Analyzer add-ons -----------------------------

def _v7_city_statistics(self: SodbAnalyzer) -> List[Tuple[str, str, str]]:
    obj = self.obj
    total_cases = len(obj.get("activeCases", []) or []) + len(obj.get("archivedCases", []) or [])
    murder_rows = list(obj.get("murders", []) or []) + list(obj.get("iaMurders", []) or [])
    stats = [
        ("build", str(obj.get("build", "—")), "Версия/билд сейва"),
        ("saveTime", str(obj.get("saveTime", "—")), "Время сохранения"),
        ("gameTime", str(round(float(obj.get("gameTime", 0) or 0), 3)), "Внутриигровое время"),
        ("citizens", str(len(obj.get("citizens", []) or [])), "Все NPC"),
        ("companies", str(len(obj.get("companies", []) or [])), "Компании"),
        ("addresses", str(len(obj.get("addresses", []) or [])), "Локации/адреса"),
        ("rooms", str(len(obj.get("rooms", []) or [])), "Комнаты"),
        ("interactables", str(len(obj.get("interactables", []) or [])), "Предметы/объекты"),
        ("evidence", str(len(obj.get("evidence", []) or [])), "Карточки evidence"),
        ("cases", str(total_cases), "Активные + архивные дела"),
        ("murders", str(len(murder_rows)), "murders + iaMurders"),
        ("passcodes", str(len(obj.get("passcodes", []) or [])), "Сохранённые passcodes"),
        ("keyring", str(len(obj.get("keyring", []) or [])), "Ключи игрока"),
        ("firstPersonItems", str(len(obj.get("firstPersonItems", []) or [])), "Предметы в быстрых слотах"),
        ("sync disks/upgrades", str(len(obj.get("upgrades", []) or [])), "Установленные апгрейды"),
        ("currentMurderer", self.human_name(obj.get("currentMurderer")), "Текущий убийца, если есть"),
        ("currentVictim", self.human_name(obj.get("currentVictim")), "Текущая жертва, если есть"),
        ("player", f"{obj.get('playerFirstName', '')} {obj.get('playerSurname', '')}".strip() or "—", "Имя игрока"),
        ("money", str(obj.get("money", "—")), "Деньги игрока"),
    ]
    try:
        from collections import Counter
        presets = Counter(str(it.get("p") or it.get("lp") or "unknown") for it in obj.get("interactables", []) if isinstance(it, dict))
        for name, count in presets.most_common(12):
            stats.append((f"item preset: {name}", str(count), "Топ interactables по preset"))
    except Exception:
        pass
    return stats


def _v7_murder_timeline(self: SodbAnalyzer) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for source, arr in (("murders", self.obj.get("murders", []) or []), ("iaMurders", self.obj.get("iaMurders", []) or [])):
        for m in arr:
            if not isinstance(m, dict):
                continue
            mid = m.get("murderID")
            killer = m.get("murdererID")
            victim = m.get("victimID")
            address_id = m.get("addressID") if isinstance(m.get("addressID"), int) else m.get("victimSiteID")
            rows.append({
                "time": m.get("time", m.get("creationTime", "")),
                "created": m.get("creationTime", ""),
                "murder": f"Murder{mid}" if mid not in (None, "") else source,
                "killer": self.human_name(killer),
                "victim": self.human_name(victim),
                "weapon": m.get("weaponStr") or m.get("weapon") or "—",
                "preset": m.get("presetStr") or m.get("moStr") or "—",
                "address": self.address_label(address_id) if isinstance(address_id, int) else "—",
                "state": str(m.get("state", "—")),
                "source": source,
                "raw": m,
            })
    def sort_key(x: Dict[str, Any]) -> float:
        try:
            return float(x.get("time") or x.get("created") or 0)
        except Exception:
            return 0.0
    return sorted(rows, key=sort_key)


def _v7_inventory_infos(self: SodbAnalyzer, query: str = "") -> List[Dict[str, Any]]:
    q = (query or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    inter_by_id = {it.get("id"): it for it in self.obj.get("interactables", []) if isinstance(it, dict)}
    for fp in self.obj.get("firstPersonItems", []) or []:
        if not isinstance(fp, dict):
            continue
        iid = fp.get("interactableID")
        it = inter_by_id.get(iid, {}) if iid is not None else {}
        preset = it.get("p") or it.get("lp") or it.get("nEvKey") or fp.get("debugName") or "—"
        room_id = it.get("r") if isinstance(it, dict) else None
        owner_id = it.get("w") if isinstance(it, dict) else None
        row = {
            "kind": "slot",
            "slot": fp.get("index", "—"),
            "hotkey": fp.get("hotkey", "—"),
            "id": str(iid),
            "name": str(preset),
            "owner": self.human_name(owner_id) if isinstance(owner_id, int) and owner_id > 0 else "—",
            "room": self.room_label(room_id) if isinstance(room_id, int) else "—",
            "raw": {"firstPersonItem": fp, "interactable": it},
        }
        if q and q not in _v7_hay(*row.values()):
            continue
        rows.append(row)
    for i, kid in enumerate(self.obj.get("keyring", []) or []):
        row = {"kind": "keyring", "slot": i, "hotkey": "—", "id": str(kid), "name": "Key", "owner": "—", "room": "—", "raw": {"keyringIndex": i, "key": kid}}
        if q and q not in _v7_hay(*row.values()):
            continue
        rows.append(row)
    carried = self.obj.get("carried")
    if carried not in (None, -1, ""):
        it = inter_by_id.get(carried, {})
        row = {"kind": "carried", "slot": "current", "hotkey": "—", "id": str(carried), "name": str(it.get("p") or it.get("lp") or it.get("nEvKey") or "Interactable"), "owner": "player", "room": self.room_label(it.get("r")) if isinstance(it, dict) and isinstance(it.get("r"), int) else "—", "raw": {"carried": carried, "interactable": it}}
        if not q or q in _v7_hay(*row.values()):
            rows.insert(0, row)
    return rows


def _v7_sync_disk_infos(self: SodbAnalyzer, query: str = "") -> List[Dict[str, Any]]:
    q = (query or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    for idx, u in enumerate(self.obj.get("upgrades", []) or []):
        if not isinstance(u, dict):
            continue
        row = {
            "idx": idx,
            "upgrade": str(u.get("upgrade", "—")),
            "state": str(u.get("state", "—")),
            "level": str(u.get("level", "—")),
            "list": str(u.get("list", "—")),
            "objId": str(u.get("objId", "—")),
            "cost": str(u.get("uninstallCost", "—")),
            "raw": u,
        }
        if q and q not in _v7_hay(*row.values()):
            continue
        rows.append(row)
    return rows


def _v7_apartment_infos(self: SodbAnalyzer, query: str = "") -> List[Dict[str, Any]]:
    q = (query or "").strip().lower()
    rooms_by_address: Dict[int, List[int]] = {}
    for rid, aid in getattr(self, "room_address_map", {}).items():
        if isinstance(rid, int) and isinstance(aid, int):
            rooms_by_address.setdefault(aid, []).append(rid)
    residents_by_address: Dict[int, List[str]] = {}
    residents_by_room: Dict[int, List[str]] = {}
    for p in self.person_infos():
        for rid in p.raw.get("atHome", []) if isinstance(p.raw.get("atHome"), list) else []:
            if isinstance(rid, int):
                residents_by_room.setdefault(rid, []).append(self.human_name(p.cid))
                aid = self.room_address_map.get(rid)
                if isinstance(aid, int):
                    residents_by_address.setdefault(aid, []).append(self.human_name(p.cid))
        goal = p.raw.get("currentGoal") if isinstance(p.raw.get("currentGoal"), dict) else {}
        if goal.get("isAddress") and isinstance(goal.get("gameLocation"), int) and goal.get("preset") in ("Home", "Sleep", "Relax", "AtHome"):
            residents_by_address.setdefault(goal.get("gameLocation"), []).append(self.human_name(p.cid))
    pass_by_room = {pc.target_id: pc.code for pc in self.password_infos() if pc.pc_type == 1}
    rows: List[Dict[str, Any]] = []
    all_addr_ids = set(self.address_by_id.keys()) | set(rooms_by_address.keys()) | set(residents_by_address.keys())
    owned = set(x for x in self.obj.get("apartmentsOwned", []) or [] if isinstance(x, int))
    residence = self.obj.get("residence")
    for aid in sorted(x for x in all_addr_ids if isinstance(x, int)):
        rooms = sorted(rooms_by_address.get(aid, []))[:60]
        passcodes = [pass_by_room[r] for r in rooms if r in pass_by_room]
        residents = []
        residents.extend(residents_by_address.get(aid, []))
        for r in rooms:
            residents.extend(residents_by_room.get(r, []))
        residents = list(dict.fromkeys(residents))
        row = {
            "address_id": f"Location{aid}",
            "address": self.address_label(aid),
            "rooms": ", ".join(f"Room{x}" for x in rooms[:12]) or "—",
            "residents": ", ".join(residents[:12]) or "—",
            "passwords": ", ".join(passcodes[:8]) or "—",
            "owned": "yes" if aid in owned or aid == residence else "",
            "raw": self.address_by_id.get(aid, {}),
        }
        if q and q not in _v7_hay(*row.values()):
            continue
        rows.append(row)
    return rows


def _v7_global_search(self: SodbAnalyzer, query: str, limit: int = 700) -> List[Dict[str, Any]]:
    q = (query or "").strip().lower()
    if not q:
        return []
    rows: List[Dict[str, Any]] = []

    def add(section: str, ident: str, title: str, location: str, snippet: str, raw: Any) -> None:
        if len(rows) >= limit:
            return
        rows.append({"section": section, "id": ident, "title": title, "location": location, "snippet": snippet, "raw": raw})

    for p in self.person_infos():
        text = _v7_hay(p.cid, p.name, p.role, p.flags, p.goal, p.location_id, p.position, self.password_status_for_human(p.cid), _v7_short(p.raw, 1400))
        if q in text:
            add("citizen", f"Human{p.cid}", p.name, self.location_label((p.raw.get("currentGoal") or {}).get("gameLocation") if isinstance(p.raw.get("currentGoal"), dict) else None, (p.raw.get("currentGoal") or {}).get("room") if isinstance(p.raw.get("currentGoal"), dict) else None), p.role or ", ".join(p.flags), p.raw)
    for c in self.case_infos():
        text = _v7_hay(c.cid, c.name, c.case_type, c.status, c.target_name, c.preset, c.short, _v7_short(c.raw, 2000))
        if q in text:
            add("case", f"Case{c.cid}", c.name, c.target_name, c.short, c.raw)
    for pc in self.password_infos():
        text = _v7_hay(pc.code, pc.pc_type, pc.target_name, pc.owner_hint, pc.notes)
        if q in text:
            add("passcode", str(pc.index), pc.code, pc.target_name, f"owner={pc.owner_hint}; used={pc.used}; notes={pc.notes}", pc.raw)
    for comp in self.company_infos():
        text = _v7_hay(comp.cid, comp.roster_id, comp.roles, comp.workers, comp.raw_company)
        if q in text:
            add("company", f"Company{comp.cid}", comp.roster_id, "—", "; ".join(f"{n} ({r})" for _hid, n, r in comp.workers[:8]), comp.raw_company)
    for it in self.item_infos(query):
        add("interactable", it.iid, f"{it.preset} {it.name}", it.address or it.room, f"owner={it.owner}; passcode={it.passcode}; evidence={it.evidence}", it.raw)
    for a in self.address_room_infos(query, limit=150):
        add("room/address", a.room_id, a.address, a.occupants, f"company={a.company}; password={a.password}; items={a.item_count}", a.raw)
    for source in ("evidence", "interactables", "rooms", "addresses", "murders", "iaMurders", "messageThreads"):
        arr = self.obj.get(source, []) or []
        if not isinstance(arr, list):
            continue
        for i, x in enumerate(arr[:8000]):
            if len(rows) >= limit:
                return rows
            if not isinstance(x, (dict, list, str, int, float, bool)):
                continue
            s = _v7_short(x, 1600)
            if q in s.lower():
                ident = str(x.get("id", i)) if isinstance(x, dict) else str(i)
                title = str((x.get("n") or x.get("name") or x.get("presetStr") or x.get("p") or x.get("nEvKey") or source) if isinstance(x, dict) else source)
                add(source, ident, title, "—", s[:500], x)
    return rows


SodbAnalyzer.city_statistics = _v7_city_statistics
SodbAnalyzer.murder_timeline = _v7_murder_timeline
SodbAnalyzer.inventory_infos = _v7_inventory_infos
SodbAnalyzer.sync_disk_infos = _v7_sync_disk_infos
SodbAnalyzer.apartment_infos = _v7_apartment_infos
SodbAnalyzer.global_search = _v7_global_search


# ------------------------------- GUI add-ons -------------------------------


def _v7_setup_compact_style(self: SodbEditorApp) -> None:
    _old_setup_style(self)
    try:
        style = ttk.Style(self)
        style.configure("Detective.TFrame", background="#0e1118")
        style.configure("Card.TFrame", background="#171d29")
        style.configure("CardTitle.TLabel", background="#171d29", foreground="#ff9db7", font=("Segoe UI", 12, "bold"))
        style.configure("CardHint.TLabel", background="#171d29", foreground="#aeb7c6", font=("Segoe UI", 9))
    except Exception:
        pass


def _v7_build_ui(self: SodbEditorApp) -> None:
    _v6_build_ui(self)
    self.detective_mode_var = tk.BooleanVar(value=False)
    self.detective_tab: Optional[ttk.Frame] = None
    self.detective_nb: Optional[ttk.Notebook] = None
    # Put a single toggle at the end of the existing toolbar. Detective tabs are lazy-built.
    try:
        children = self.winfo_children()
        bar = children[1] if len(children) > 1 else None
        if bar is not None:
            ttk.Checkbutton(bar, text="Detective Mode", variable=self.detective_mode_var, command=self.toggle_detective_mode).pack(side="left", padx=8)
    except Exception:
        pass
    self.bind_all("<Control-Shift-F>", lambda _e: self.open_detective_search())
    self.bind_all("<Control-p>", lambda _e: self.open_detective_search())
    try:
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self.after(120, self.autofit_visible_tree))
    except Exception:
        pass


def _v7_toggle_detective_mode(self: SodbEditorApp) -> None:
    if self.detective_mode_var.get():
        if self.detective_tab is None:
            self._build_detective_mode_tab()
        # Add only if currently hidden.
        tab_ids = list(self.nb.tabs())
        if str(self.detective_tab) not in tab_ids:
            self.nb.add(self.detective_tab, text="Detective Mode")
        self.nb.select(self.detective_tab)
        self.populate_detective_all()
    else:
        if self.detective_tab is not None:
            try:
                self.nb.forget(self.detective_tab)
            except Exception:
                pass


def _v7_open_detective_search(self: SodbEditorApp) -> None:
    if not getattr(self, "detective_mode_var", None):
        return
    self.detective_mode_var.set(True)
    self.toggle_detective_mode()
    try:
        if self.detective_nb is not None:
            self.detective_nb.select(self.det_global_tab)
        self.det_search_entry.focus_set()
    except Exception:
        pass


def _v7_text_in(parent: tk.Widget, wrap: str = "word") -> tk.Text:
    frame = ttk.Frame(parent, style="Panel.TFrame")
    frame.pack(fill="both", expand=True)
    text = tk.Text(frame, wrap=wrap, bg="#0f1320", fg="#d8dde8", insertbackground="#ffffff", selectbackground="#8a3554", font=("Consolas", 10), relief="flat")
    y = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    x = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=y.set, xscrollcommand=x.set)
    text.grid(row=0, column=0, sticky="nsew")
    y.grid(row=0, column=1, sticky="ns")
    x.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return text


def _v7_build_detective_mode_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.nb, style="Detective.TFrame")
    self.detective_tab = tab
    header = ttk.Frame(tab, style="Detective.TFrame")
    header.pack(fill="x", padx=12, pady=(10, 6))
    ttk.Label(header, text="Detective Mode", style="Title.TLabel").pack(side="left")
    ttk.Label(header, text="  включаемая витрина: поиск, timeline, карта, inventory, sync disks, квартиры, статистика, JSON", style="Hint.TLabel").pack(side="left", padx=8)
    ttk.Button(header, text="Обновить", command=self.populate_detective_all).pack(side="right", padx=3)
    ttk.Button(header, text="Скрыть", command=lambda: (self.detective_mode_var.set(False), self.toggle_detective_mode())).pack(side="right", padx=3)

    self.detective_nb = ttk.Notebook(tab)
    self.detective_nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    self._build_det_global_tab()
    self._build_det_profile_tab()
    self._build_det_timeline_tab()
    self._build_det_map_tab()
    self._build_det_inventory_tab()
    self._build_det_sync_tab()
    self._build_det_apartments_tab()
    self._build_det_stats_tab()
    self._build_det_json_tab()


def _v7_build_det_global_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.det_global_tab = tab
    self.detective_nb.add(tab, text="Поиск всего")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="Ctrl+Shift+F / Ctrl+P:").pack(side="left")
    self.det_search_var = tk.StringVar()
    self.det_search_entry = ttk.Entry(toolbar, textvariable=self.det_search_var, width=48)
    self.det_search_entry.pack(side="left", padx=6)
    self.det_search_entry.bind("<Return>", lambda _e: self.run_global_search())
    ttk.Button(toolbar, text="Искать", command=self.run_global_search).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Экспорт CSV", command=lambda: self._tree_to_csv(self.det_search_tree, "global_search.csv")).pack(side="left", padx=4)
    ttk.Label(toolbar, text="Ищет по citizens, cases, passcodes, companies, items, rooms, evidence/interactables.", style="Hint.TLabel").pack(side="left", padx=10)
    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Frame(paned, style="Panel.TFrame")
    right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3); paned.add(right, weight=2)
    cols = ("section", "id", "title", "location", "snippet")
    self.det_search_tree = _v6_make_tree(left, cols, [("section", 110), ("id", 120), ("title", 260), ("location", 280), ("snippet", 540)], self.on_global_search_select)
    self.det_search_details = _v7_text_in(right)
    self._global_search_cache: List[Dict[str, Any]] = []


def _v7_build_det_profile_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="Карточка NPC")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="HumanID / имя:").pack(side="left")
    self.det_profile_var = tk.StringVar()
    ent = ttk.Entry(toolbar, textvariable=self.det_profile_var, width=42)
    ent.pack(side="left", padx=6)
    ent.bind("<Return>", lambda _e: self.show_detective_profile())
    ttk.Button(toolbar, text="Показать", command=self.show_detective_profile).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Из выбранного в таблице людей", command=self.detective_profile_from_selected_person).pack(side="left", padx=4)
    self.det_profile_text = _v7_text_in(tab, wrap="word")


def _v7_build_det_timeline_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="Timeline убийств")
    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=10)
    left = ttk.Frame(paned, style="Panel.TFrame"); right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3); paned.add(right, weight=2)
    cols = ("time", "murder", "killer", "victim", "weapon", "address", "state")
    self.timeline_tree = _v6_make_tree(left, cols, [("time", 80), ("murder", 90), ("killer", 210), ("victim", 210), ("weapon", 130), ("address", 320), ("state", 70)], self.on_timeline_select)
    self.timeline_details = _v7_text_in(right)
    self._timeline_cache: List[Dict[str, Any]] = []


def _v7_build_det_map_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="Карта / heatmap")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    self.map_show_people = tk.BooleanVar(value=True)
    self.map_show_items = tk.BooleanVar(value=False)
    self.map_show_murders = tk.BooleanVar(value=True)
    self.map_show_heat = tk.BooleanVar(value=True)
    for text, var in (("people", self.map_show_people), ("items", self.map_show_items), ("murders", self.map_show_murders), ("heat", self.map_show_heat)):
        ttk.Checkbutton(toolbar, text=text, variable=var, command=self.draw_detective_map).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Перерисовать", command=self.draw_detective_map).pack(side="left", padx=8)
    ttk.Label(toolbar, text="2D-проекция по координатам из сейва; без игровых текстур, чтобы не перегружать интерфейс.", style="Hint.TLabel").pack(side="left", padx=10)
    self.det_map_canvas = tk.Canvas(tab, bg="#0f1320", highlightthickness=0)
    self.det_map_canvas.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    self.det_map_canvas.bind("<Configure>", lambda _e: self.draw_detective_map())


def _v7_build_det_inventory_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="Инвентарь")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="Фильтр:").pack(side="left")
    self.inventory_filter = tk.StringVar()
    ttk.Entry(toolbar, textvariable=self.inventory_filter, width=34).pack(side="left", padx=6)
    self.inventory_filter.trace_add("write", lambda *_: self.populate_inventory())
    ttk.Button(toolbar, text="Set carried from selected", command=self.set_carried_from_selected_inventory).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Экспорт CSV", command=lambda: self._tree_to_csv(self.inventory_tree, "inventory.csv")).pack(side="left", padx=4)
    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Frame(paned, style="Panel.TFrame"); right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3); paned.add(right, weight=2)
    cols = ("kind", "slot", "hotkey", "id", "name", "owner", "room")
    self.inventory_tree = _v6_make_tree(left, cols, [("kind", 90), ("slot", 70), ("hotkey", 70), ("id", 130), ("name", 240), ("owner", 220), ("room", 300)], self.on_inventory_select)
    self.inventory_details = _v7_text_in(right)
    self._inventory_cache: List[Dict[str, Any]] = []


def _v7_build_det_sync_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="Sync disks")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="Фильтр:").pack(side="left")
    self.sync_filter = tk.StringVar()
    ttk.Entry(toolbar, textvariable=self.sync_filter, width=34).pack(side="left", padx=6)
    self.sync_filter.trace_add("write", lambda *_: self.populate_sync_disks())
    ttk.Button(toolbar, text="Set level", command=self.set_selected_sync_level).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Set state", command=self.set_selected_sync_state).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Экспорт CSV", command=lambda: self._tree_to_csv(self.sync_tree, "sync_disks.csv")).pack(side="left", padx=4)
    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Frame(paned, style="Panel.TFrame"); right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3); paned.add(right, weight=2)
    cols = ("idx", "upgrade", "state", "level", "list", "objId", "cost")
    self.sync_tree = _v6_make_tree(left, cols, [("idx", 60), ("upgrade", 260), ("state", 70), ("level", 70), ("list", 70), ("objId", 120), ("cost", 80)], self.on_sync_select)
    self.sync_details = _v7_text_in(right)
    self._sync_cache: List[Dict[str, Any]] = []


def _v7_build_det_apartments_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="Квартиры")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="Фильтр:").pack(side="left")
    self.apartment_filter = tk.StringVar()
    ttk.Entry(toolbar, textvariable=self.apartment_filter, width=42).pack(side="left", padx=6)
    self.apartment_filter.trace_add("write", lambda *_: self.populate_apartments())
    ttk.Button(toolbar, text="Экспорт CSV", command=lambda: self._tree_to_csv(self.apartment_tree, "apartments.csv")).pack(side="left", padx=4)
    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Frame(paned, style="Panel.TFrame"); right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3); paned.add(right, weight=2)
    cols = ("address_id", "address", "rooms", "residents", "passwords", "owned")
    self.apartment_tree = _v6_make_tree(left, cols, [("address_id", 105), ("address", 300), ("rooms", 250), ("residents", 360), ("passwords", 160), ("owned", 70)], self.on_apartment_select)
    self.apartment_details = _v7_text_in(right)
    self._apartment_cache: List[Dict[str, Any]] = []


def _v7_build_det_stats_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="Статистика")
    cols = ("metric", "value", "note")
    self.stats_tree = _v6_make_tree(tab, cols, [("metric", 270), ("value", 160), ("note", 620)])


def _v7_build_det_json_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="JSON Inspector")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="Поиск path/value:").pack(side="left")
    self.json_inspector_search = tk.StringVar()
    ent = ttk.Entry(toolbar, textvariable=self.json_inspector_search, width=44)
    ent.pack(side="left", padx=6)
    ent.bind("<Return>", lambda _e: self.search_json_inspector())
    ttk.Button(toolbar, text="Искать", command=self.search_json_inspector).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Показать top-level", command=self.populate_json_inspector_top).pack(side="left", padx=4)
    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Frame(paned, style="Panel.TFrame"); right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=2); paned.add(right, weight=3)
    cols = ("path", "type", "summary")
    self.json_tree = _v6_make_tree(left, cols, [("path", 260), ("type", 90), ("summary", 420)], self.on_json_inspector_select)
    self.json_details = _v7_text_in(right)
    self._json_cache: Dict[str, Any] = {}


# ----------------------------- populate/actions -----------------------------

def _v7_populate_detective_all(self: SodbEditorApp) -> None:
    if not getattr(self, "detective_tab", None) or not self.analyzer:
        return
    self.populate_timeline()
    self.populate_inventory()
    self.populate_sync_disks()
    self.populate_apartments()
    self.populate_city_stats()
    self.populate_json_inspector_top()
    self.draw_detective_map()


def _v7_run_global_search(self: SodbEditorApp) -> None:
    if not self.analyzer:
        return
    q = self.det_search_var.get().strip()
    self._global_search_cache = self.analyzer.global_search(q, limit=700)
    self.det_search_tree.delete(*self.det_search_tree.get_children())
    for i, r in enumerate(self._global_search_cache):
        self.det_search_tree.insert("", "end", iid=str(i), values=(r["section"], r["id"], r["title"], r["location"], r["snippet"]))
    self.after(80, lambda: self.autofit_tree(self.det_search_tree))
    self.status_var.set(f"Global search: {len(self._global_search_cache)} результатов")


def _v7_on_global_search_select(self: SodbEditorApp, _event=None) -> None:
    sel = self.det_search_tree.selection() if hasattr(self, "det_search_tree") else []
    if not sel:
        return
    idx = int(sel[0])
    if idx >= len(getattr(self, "_global_search_cache", [])):
        return
    r = self._global_search_cache[idx]
    self.det_search_details.delete("1.0", "end")
    self.det_search_details.insert("1.0", f"{r['section']} | {r['id']} | {r['title']}\nLocation: {r['location']}\n\nRAW:\n" + json.dumps(r.get("raw"), ensure_ascii=False, indent=2)[:180_000])


def _v7_find_person_from_query(self: SodbEditorApp, query: str) -> Optional[int]:
    if not self.analyzer:
        return None
    q = (query or "").strip().lower()
    if not q:
        return None
    hid = _v6_human_id_from_text(q)
    if hid is not None:
        return hid
    if q.isdigit():
        return int(q)
    best = None
    for p in self.analyzer.person_infos():
        if q in (p.name or "").lower() or q in f"human{p.cid}".lower():
            best = p.cid
            if q == (p.name or "").lower():
                break
    return best


def _v7_show_detective_profile(self: SodbEditorApp) -> None:
    if not self.analyzer:
        return
    hid = self._find_person_from_query(self.det_profile_var.get())
    self.det_profile_text.delete("1.0", "end")
    if hid is None:
        self.det_profile_text.insert("1.0", "Не нашёл человека. Введи HumanID, число, имя или фамилию.")
        return
    p = next((x for x in self.analyzer.person_infos() if x.cid == hid), None)
    if not p:
        self.det_profile_text.insert("1.0", f"Human{hid} не найден в citizens.")
        return
    # Reuse the full v6 person card, but add a short detective summary first.
    rels = self.analyzer.person_relations(hid, limit=20)
    summary = [
        f"DETECTIVE PROFILE — {self.analyzer.human_name(hid)}",
        "=" * 72,
        f"Password: {self.analyzer.password_status_for_human(hid)}",
        f"Relations: {len(rels)} shown / more in graph tab",
        "",
    ]
    self.det_profile_text.insert("1.0", "\n".join(summary) + self.analyzer.pretty_person_details(p))


def _v7_detective_profile_from_selected_person(self: SodbEditorApp) -> None:
    sel = self.people_tree.selection() if hasattr(self, "people_tree") else []
    if not sel:
        messagebox.showinfo("NPC", "Выбери человека во вкладке «Люди / связи».")
        return
    vals = self.people_tree.item(sel[0], "values")
    if vals:
        self.det_profile_var.set(str(vals[0]))
        self.show_detective_profile()


def _v7_populate_timeline(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "timeline_tree"):
        return
    self._timeline_cache = self.analyzer.murder_timeline()
    self.timeline_tree.delete(*self.timeline_tree.get_children())
    for i, r in enumerate(self._timeline_cache):
        self.timeline_tree.insert("", "end", iid=str(i), values=(r["time"], r["murder"], r["killer"], r["victim"], r["weapon"], r["address"], r["state"]))
    self.after(80, lambda: self.autofit_tree(self.timeline_tree))


def _v7_on_timeline_select(self: SodbEditorApp, _event=None) -> None:
    sel = self.timeline_tree.selection() if hasattr(self, "timeline_tree") else []
    if not sel:
        return
    idx = int(sel[0])
    if idx >= len(getattr(self, "_timeline_cache", [])):
        return
    r = self._timeline_cache[idx]
    self.timeline_details.delete("1.0", "end")
    self.timeline_details.insert("1.0", f"{r['murder']}\nKiller: {r['killer']}\nVictim: {r['victim']}\nWeapon: {r['weapon']}\nAddress: {r['address']}\nPreset/MO: {r['preset']}\nSource: {r['source']}\n\nRAW:\n" + json.dumps(r.get("raw"), ensure_ascii=False, indent=2)[:140_000])


def _v7_draw_detective_map(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "det_map_canvas"):
        return
    canvas = self.det_map_canvas
    canvas.delete("all")
    w = max(canvas.winfo_width(), 400); h = max(canvas.winfo_height(), 300)
    points: List[Tuple[float, float, str, str, str]] = []
    # kind, label, tooltip is stored in tuple with visual role.
    if self.map_show_people.get():
        for p in self.analyzer.person_infos():
            pos = _v7_pos_tuple(p.raw.get("pos"))
            if pos:
                points.append((pos[0], pos[2], "person", self.analyzer.human_name(p.cid), ""))
    if self.map_show_items.get():
        for it in self.obj.get("interactables", [])[:1200] if self.obj else []:
            if isinstance(it, dict):
                pos = _v7_pos_tuple(it.get("wPos") or it.get("spWPos"))
                if pos:
                    points.append((pos[0], pos[2], "item", str(it.get("p") or it.get("lp") or it.get("id")), ""))
    if self.map_show_murders.get():
        for m in self.analyzer.murder_timeline():
            raw = m.get("raw", {})
            pos = _v7_pos_tuple(raw.get("sniperKillShotNode") or raw.get("victimSite") or raw.get("location"))
            if pos:
                points.append((pos[0], pos[1], "murder", f"{m['murder']} {m['victim']}", ""))
    # fallback: if murder coordinates absent, plot current murderer/victim positions.
    for hid, kind in ((self.obj.get("currentMurderer") if self.obj else None, "murderer"), (self.obj.get("currentVictim") if self.obj else None, "victim")):
        if isinstance(hid, int):
            c = next((x for x in self.obj.get("citizens", []) if isinstance(x, dict) and x.get("id") == hid), None)
            pos = _v7_pos_tuple(c.get("pos")) if c else None
            if pos:
                points.append((pos[0], pos[2], kind, self.analyzer.human_name(hid), ""))
    if not points:
        canvas.create_text(w/2, h/2, text="Нет координат для карты", fill="#d8dde8", font=("Segoe UI", 12))
        return
    xs = [p[0] for p in points]; ys = [p[1] for p in points]
    minx, maxx = min(xs), max(xs); miny, maxy = min(ys), max(ys)
    if maxx == minx: maxx += 1
    if maxy == miny: maxy += 1
    pad = 36
    def tx(x): return pad + (x - minx) / (maxx - minx) * (w - pad*2)
    def ty(y): return h - pad - (y - miny) / (maxy - miny) * (h - pad*2)
    canvas.create_rectangle(pad, pad, w-pad, h-pad, outline="#273246")
    if self.map_show_heat.get():
        heat = [p for p in points if p[2] in ("murder", "murderer", "victim")]
        for x, y, _kind, _label, _ in heat:
            canvas.create_oval(tx(x)-24, ty(y)-24, tx(x)+24, ty(y)+24, outline="#70324a", width=2)
            canvas.create_oval(tx(x)-10, ty(y)-10, tx(x)+10, ty(y)+10, fill="#8a3554", outline="")
    for x, y, kind, label, _ in points[:2500]:
        r = 3
        fill = "#d8dde8"
        if kind in ("murder", "murderer"): fill = "#ff5f7e"; r = 6
        elif kind == "victim": fill = "#ffbd66"; r = 5
        elif kind == "item": fill = "#77a7ff"; r = 2
        canvas.create_oval(tx(x)-r, ty(y)-r, tx(x)+r, ty(y)+r, fill=fill, outline="")
        if kind in ("murder", "murderer", "victim"):
            canvas.create_text(tx(x)+8, ty(y)-8, text=label[:38], fill="#d8dde8", anchor="w", font=("Segoe UI", 9))
    canvas.create_text(12, 12, text=f"points={len(points)} | people/items/murders are approximate 2D positions", fill="#9aa5b5", anchor="nw", font=("Segoe UI", 9))


def _v7_populate_inventory(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "inventory_tree"):
        return
    self._inventory_cache = self.analyzer.inventory_infos(self.inventory_filter.get() if hasattr(self, "inventory_filter") else "")
    self.inventory_tree.delete(*self.inventory_tree.get_children())
    for i, r in enumerate(self._inventory_cache):
        self.inventory_tree.insert("", "end", iid=str(i), values=(r["kind"], r["slot"], r["hotkey"], r["id"], r["name"], r["owner"], r["room"]))
    self.after(80, lambda: self.autofit_tree(self.inventory_tree))


def _v7_on_inventory_select(self: SodbEditorApp, _event=None) -> None:
    sel = self.inventory_tree.selection() if hasattr(self, "inventory_tree") else []
    if not sel:
        return
    idx = int(sel[0])
    if idx >= len(getattr(self, "_inventory_cache", [])):
        return
    r = self._inventory_cache[idx]
    self.inventory_details.delete("1.0", "end")
    self.inventory_details.insert("1.0", json.dumps(r.get("raw"), ensure_ascii=False, indent=2)[:160_000])


def _v7_set_carried_from_selected_inventory(self: SodbEditorApp) -> None:
    if not self.obj or not hasattr(self, "inventory_tree"):
        return
    sel = self.inventory_tree.selection()
    if not sel:
        return
    idx = int(sel[0]); r = self._inventory_cache[idx]
    try:
        iid = int(str(r.get("id")))
    except Exception:
        messagebox.showinfo("Inventory", "У выбранной строки нет числового interactableID.")
        return
    self.obj["carried"] = iid
    self.mark_dirty(f"carried установлен на {iid}")
    self.populate_inventory()


def _v7_populate_sync_disks(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "sync_tree"):
        return
    self._sync_cache = self.analyzer.sync_disk_infos(self.sync_filter.get() if hasattr(self, "sync_filter") else "")
    self.sync_tree.delete(*self.sync_tree.get_children())
    for i, r in enumerate(self._sync_cache):
        self.sync_tree.insert("", "end", iid=str(i), values=(r["idx"], r["upgrade"], r["state"], r["level"], r["list"], r["objId"], r["cost"]))
    self.after(80, lambda: self.autofit_tree(self.sync_tree))


def _v7_on_sync_select(self: SodbEditorApp, _event=None) -> None:
    sel = self.sync_tree.selection() if hasattr(self, "sync_tree") else []
    if not sel:
        return
    idx = int(sel[0])
    if idx >= len(getattr(self, "_sync_cache", [])):
        return
    r = self._sync_cache[idx]
    self.sync_details.delete("1.0", "end")
    self.sync_details.insert("1.0", json.dumps(r.get("raw"), ensure_ascii=False, indent=2)[:120_000])


def _v7_set_selected_sync_level(self: SodbEditorApp) -> None:
    _v7_set_selected_sync_field(self, "level")


def _v7_set_selected_sync_state(self: SodbEditorApp) -> None:
    _v7_set_selected_sync_field(self, "state")


def _v7_set_selected_sync_field(self: SodbEditorApp, field_name: str) -> None:
    if not self.obj or not hasattr(self, "sync_tree"):
        return
    sel = self.sync_tree.selection()
    if not sel:
        return
    row = self._sync_cache[int(sel[0])]
    idx = int(row["idx"])
    current = self.obj.get("upgrades", [])[idx].get(field_name)
    value = simpledialog.askinteger("Sync disk", f"Новое значение {field_name}", initialvalue=int(current or 0), parent=self)
    if value is None:
        return
    self.obj["upgrades"][idx][field_name] = value
    self.mark_dirty(f"Sync disk {idx}: {field_name}={value}")
    self.populate_sync_disks()


def _v7_populate_apartments(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "apartment_tree"):
        return
    self._apartment_cache = self.analyzer.apartment_infos(self.apartment_filter.get() if hasattr(self, "apartment_filter") else "")
    self.apartment_tree.delete(*self.apartment_tree.get_children())
    for i, r in enumerate(self._apartment_cache):
        self.apartment_tree.insert("", "end", iid=str(i), values=(r["address_id"], r["address"], r["rooms"], r["residents"], r["passwords"], r["owned"]))
    self.after(80, lambda: self.autofit_tree(self.apartment_tree))


def _v7_on_apartment_select(self: SodbEditorApp, _event=None) -> None:
    sel = self.apartment_tree.selection() if hasattr(self, "apartment_tree") else []
    if not sel:
        return
    idx = int(sel[0])
    if idx >= len(getattr(self, "_apartment_cache", [])):
        return
    r = self._apartment_cache[idx]
    self.apartment_details.delete("1.0", "end")
    self.apartment_details.insert("1.0", f"{r['address_id']}\n{r['address']}\nRooms: {r['rooms']}\nResidents: {r['residents']}\nPasswords: {r['passwords']}\nOwned: {r['owned']}\n\nRAW ADDRESS:\n" + json.dumps(r.get("raw"), ensure_ascii=False, indent=2)[:120_000])


def _v7_populate_city_stats(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "stats_tree"):
        return
    self.stats_tree.delete(*self.stats_tree.get_children())
    for i, (metric, value, note) in enumerate(self.analyzer.city_statistics()):
        self.stats_tree.insert("", "end", iid=str(i), values=(metric, value, note))
    self.after(80, lambda: self.autofit_tree(self.stats_tree))


def _v7_populate_json_inspector_top(self: SodbEditorApp) -> None:
    if not self.obj or not hasattr(self, "json_tree"):
        return
    self._json_cache = {}
    self.json_tree.delete(*self.json_tree.get_children())
    for k in sorted(self.obj.keys()):
        v = self.obj[k]
        if isinstance(v, list): typ, summary = "list", f"{len(v)} items"
        elif isinstance(v, dict): typ, summary = "dict", f"{len(v)} keys"
        else: typ, summary = type(v).__name__, str(v)[:300]
        self._json_cache[k] = v
        self.json_tree.insert("", "end", iid=k, values=(k, typ, summary))
    self.after(80, lambda: self.autofit_tree(self.json_tree))


def _v7_search_json_inspector(self: SodbEditorApp) -> None:
    if not self.obj or not hasattr(self, "json_tree"):
        return
    q = self.json_inspector_search.get().strip().lower()
    if not q:
        self.populate_json_inspector_top()
        return
    self._json_cache = {}
    self.json_tree.delete(*self.json_tree.get_children())
    count = 0
    for path, val in _v7_walk_limited(self.obj, "$", max_depth=7):
        text = _v7_hay(path, val)
        if q not in text:
            continue
        key = f"hit:{count}"
        self._json_cache[key] = val
        self.json_tree.insert("", "end", iid=key, values=(path, type(val).__name__, str(val)[:700]))
        count += 1
        if count >= 700:
            break
    self.status_var.set(f"JSON Inspector: {count} совпадений")
    self.after(80, lambda: self.autofit_tree(self.json_tree))


def _v7_on_json_inspector_select(self: SodbEditorApp, _event=None) -> None:
    sel = self.json_tree.selection() if hasattr(self, "json_tree") else []
    if not sel:
        return
    key = sel[0]
    val = getattr(self, "_json_cache", {}).get(key)
    self.json_details.delete("1.0", "end")
    try:
        self.json_details.insert("1.0", json.dumps(val, ensure_ascii=False, indent=2)[:240_000])
    except Exception:
        self.json_details.insert("1.0", str(val))


# ------------------------------- refresh/clear/help -------------------------------

def _v7_refresh_all(self: SodbEditorApp) -> None:
    _v6_refresh_all(self)
    if getattr(self, "detective_tab", None) is not None and getattr(self, "detective_mode_var", None) and self.detective_mode_var.get():
        self.populate_detective_all()


def _v7_clear_state(self: SodbEditorApp) -> None:
    _v6_clear_state(self)
    extra_trees = ("det_search_tree", "timeline_tree", "inventory_tree", "sync_tree", "apartment_tree", "stats_tree", "json_tree")
    for name in extra_trees:
        tree = getattr(self, name, None)
        if tree is not None:
            try: tree.delete(*tree.get_children())
            except Exception: pass
    for name in ("det_search_details", "det_profile_text", "timeline_details", "inventory_details", "sync_details", "apartment_details", "json_details"):
        txt = getattr(self, name, None)
        if txt is not None:
            try: txt.delete("1.0", "end")
            except Exception: pass
    if hasattr(self, "det_map_canvas"):
        self.det_map_canvas.delete("all")
    for name in ("det_search_var", "det_profile_var", "inventory_filter", "sync_filter", "apartment_filter", "json_inspector_search"):
        var = getattr(self, name, None)
        if var is not None:
            try: var.set("")
            except Exception: pass


def _v7_autofit_all_trees(self: SodbEditorApp) -> None:
    _v6_autofit_all_trees(self)
    for name in ("det_search_tree", "timeline_tree", "inventory_tree", "sync_tree", "apartment_tree", "stats_tree", "json_tree"):
        tree = getattr(self, name, None)
        if tree is not None:
            self.autofit_tree(tree)


def _v7_export_current_table_csv(self: SodbEditorApp) -> None:
    current = self.nb.select()
    tab_text = self.nb.tab(current, "text") if current else ""
    if tab_text == "Detective Mode" and getattr(self, "detective_nb", None) is not None:
        sub = self.detective_nb.select()
        sub_text = self.detective_nb.tab(sub, "text") if sub else ""
        mapping = {
            "Поиск всего": (getattr(self, "det_search_tree", None), "global_search.csv"),
            "Timeline убийств": (getattr(self, "timeline_tree", None), "murder_timeline.csv"),
            "Инвентарь": (getattr(self, "inventory_tree", None), "inventory.csv"),
            "Sync disks": (getattr(self, "sync_tree", None), "sync_disks.csv"),
            "Квартиры": (getattr(self, "apartment_tree", None), "apartments.csv"),
            "Статистика": (getattr(self, "stats_tree", None), "city_statistics.csv"),
            "JSON Inspector": (getattr(self, "json_tree", None), "json_inspector.csv"),
        }
        tree, default_name = mapping.get(sub_text, (None, "detective.csv"))
        if tree is not None:
            self._tree_to_csv(tree, default_name)
            return
    _v6_export_current_table_csv(self)


def _v7_show_help(self: SodbEditorApp) -> None:
    _v6_show_help(self)
    # Keep the original help small; the Detective Mode header contains the usage hints.


# Attach v7 extensions/overrides. Save original style method before replacing.
_old_setup_style = SodbEditorApp._setup_style
SodbEditorApp._setup_style = _v7_setup_compact_style
SodbEditorApp._build_ui = _v7_build_ui
SodbEditorApp.toggle_detective_mode = _v7_toggle_detective_mode
SodbEditorApp.open_detective_search = _v7_open_detective_search
SodbEditorApp._build_detective_mode_tab = _v7_build_detective_mode_tab
SodbEditorApp._build_det_global_tab = _v7_build_det_global_tab
SodbEditorApp._build_det_profile_tab = _v7_build_det_profile_tab
SodbEditorApp._build_det_timeline_tab = _v7_build_det_timeline_tab
SodbEditorApp._build_det_map_tab = _v7_build_det_map_tab
SodbEditorApp._build_det_inventory_tab = _v7_build_det_inventory_tab
SodbEditorApp._build_det_sync_tab = _v7_build_det_sync_tab
SodbEditorApp._build_det_apartments_tab = _v7_build_det_apartments_tab
SodbEditorApp._build_det_stats_tab = _v7_build_det_stats_tab
SodbEditorApp._build_det_json_tab = _v7_build_det_json_tab
SodbEditorApp.populate_detective_all = _v7_populate_detective_all
SodbEditorApp.run_global_search = _v7_run_global_search
SodbEditorApp.on_global_search_select = _v7_on_global_search_select
SodbEditorApp._find_person_from_query = _v7_find_person_from_query
SodbEditorApp.show_detective_profile = _v7_show_detective_profile
SodbEditorApp.detective_profile_from_selected_person = _v7_detective_profile_from_selected_person
SodbEditorApp.populate_timeline = _v7_populate_timeline
SodbEditorApp.on_timeline_select = _v7_on_timeline_select
SodbEditorApp.draw_detective_map = _v7_draw_detective_map
SodbEditorApp.populate_inventory = _v7_populate_inventory
SodbEditorApp.on_inventory_select = _v7_on_inventory_select
SodbEditorApp.set_carried_from_selected_inventory = _v7_set_carried_from_selected_inventory
SodbEditorApp.populate_sync_disks = _v7_populate_sync_disks
SodbEditorApp.on_sync_select = _v7_on_sync_select
SodbEditorApp.set_selected_sync_level = _v7_set_selected_sync_level
SodbEditorApp.set_selected_sync_state = _v7_set_selected_sync_state
SodbEditorApp.populate_apartments = _v7_populate_apartments
SodbEditorApp.on_apartment_select = _v7_on_apartment_select
SodbEditorApp.populate_city_stats = _v7_populate_city_stats
SodbEditorApp.populate_json_inspector_top = _v7_populate_json_inspector_top
SodbEditorApp.search_json_inspector = _v7_search_json_inspector
SodbEditorApp.on_json_inspector_select = _v7_on_json_inspector_select
SodbEditorApp.refresh_all = _v7_refresh_all
SodbEditorApp.clear_state = _v7_clear_state
SodbEditorApp.autofit_all_trees = _v7_autofit_all_trees
SodbEditorApp.export_current_table_csv = _v7_export_current_table_csv
SodbEditorApp.show_help = _v7_show_help



# v7 small visual upgrade for the existing relation graph tab: relation colors + legend.
def _v7_draw_graph_canvas_upgraded(self: SodbEditorApp, cid: int, edges: List[RelationInfo]) -> None:
    canvas = self.graph_canvas
    canvas.delete("all")
    canvas.update_idletasks()
    w = max(canvas.winfo_width(), 900)
    h = max(canvas.winfo_height(), 600)
    cx, cy = w // 2, h // 2
    radius = max(185, min(w, h) // 3)

    def rel_color(kind: str) -> str:
        k = (kind or "").lower()
        if "murder" in k or "killer" in k or "victim" in k:
            return "#ff5f7e"
        if "company" in k or "work" in k or "roster" in k:
            return "#77a7ff"
        if "home" in k or "room" in k or "address" in k:
            return "#7bd88f"
        if "sighting" in k or "seen" in k:
            return "#ffbd66"
        if "tie" in k or "evidence" in k:
            return "#c792ea"
        return "#b85c7b"

    def node(x: float, y: float, label: str, fill: str, outline: str = "#ff87a7") -> None:
        canvas.create_oval(x - 56, y - 32, x + 56, y + 32, fill=fill, outline=outline, width=2)
        canvas.create_text(x, y - 6, text=label[:20], fill="#ffffff", font=("Segoe UI", 9, "bold"), width=104)

    for gx in range(0, w, 48):
        canvas.create_line(gx, 0, gx, h, fill="#151b2a")
    for gy in range(0, h, 48):
        canvas.create_line(0, gy, w, gy, fill="#151b2a")

    legend = [("crime", "#ff5f7e"), ("work", "#77a7ff"), ("home/room", "#7bd88f"), ("sighting", "#ffbd66"), ("evidence", "#c792ea")]
    lx, ly = 14, 14
    for label, color in legend:
        canvas.create_rectangle(lx, ly, lx + 12, ly + 12, fill=color, outline="")
        canvas.create_text(lx + 18, ly + 6, text=label, fill="#aeb7c6", anchor="w", font=("Segoe UI", 8))
        ly += 18

    node(cx, cy, self.analyzer.human_name(cid), "#7b2d46", "#ffd1dc")
    if not edges:
        canvas.create_text(cx, cy + 75, text="Связи не найдены", fill="#d8dde8", font=("Segoe UI", 12))
        return

    for i, e in enumerate(edges[:48]):
        angle = 2 * math.pi * i / max(min(len(edges), 48), 1) - math.pi / 2
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        color = rel_color(e.kind)
        canvas.create_line(cx, cy, x, y, fill=color, width=2)
        midx, midy = (cx + x) / 2, (cy + y) / 2
        canvas.create_text(midx, midy, text=e.kind.split(",")[0][:24], fill=color, font=("Segoe UI", 8), width=150)
        node(x, y, self.analyzer.human_name(e.dst), "#20283a", color)

SodbEditorApp._draw_graph_canvas = _v7_draw_graph_canvas_upgraded



# ---------------------------------------------------------------------------
# v8: better table layout/scrollbars + SyncDisk installer
# ---------------------------------------------------------------------------

SYNC_DISK_CATALOG: List[Tuple[str, str]] = [
    ("BlackMarket-Infiltrator", "Infiltrator"),
    ("BlackMarket-Interceptor", "Interceptor"),
    ("BlackMarket-Trespasser", "Trespasser"),
    ("Candor-Cartographer", "Cartographer"),
    ("Candor-Community", "Community"),
    ("Candor-ModelCitizen", "Model Citizen"),
    ("Candor-PublicService", "Public Service"),
    ("ElGen-Beauty", "Beauty"),
    ("ElGen-Constitution", "Constitution"),
    ("ElGen-Frame", "Frame"),
    ("ElGen-Physique", "Physique"),
    ("ElGen-Tenacity", "Tenacity"),
    ("ElGen-Vigor", "Vigor"),
    ("Kaizen-DovePlus", "Kaizen V-Love Plus / DovePlus"),
    ("Kensington-AmbassadorScheme", "Ambassador Scheme"),
    ("Kensington-SpartanInsuranceSchemes", "Spartan Insurance"),
    ("Starch-BrandAmbassador", "Starch Brand Ambassador"),
    ("Starch-SugarDaddy", "Sugar Daddy"),
]
SYNC_DISK_CODES = [x[0] for x in SYNC_DISK_CATALOG]


def _v8_make_tree(parent: tk.Widget, columns: Tuple[str, ...], specs: List[Tuple[str, int]], bind_select=None) -> ttk.Treeview:
    """Treeview helper with reliable vertical+horizontal scrolling and non-compressed columns."""
    frame = ttk.Frame(parent, style="Panel.TFrame")
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=columns, show="headings")
    y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
    for col, width in specs:
        tree.heading(col, text=col)
        tree.column(col, width=width, minwidth=55, anchor="w", stretch=False)
    tree.grid(row=0, column=0, sticky="nsew")
    y.grid(row=0, column=1, sticky="ns")
    x.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    if bind_select:
        tree.bind("<<TreeviewSelect>>", bind_select)
    tree._sodb_scrollbars_attached = True  # type: ignore[attr-defined]
    return tree

# Make future builder functions use the improved helper.
_v6_make_tree = _v8_make_tree


def _v8_attach_scrollbars_to_existing_tree(tree: ttk.Treeview) -> None:
    """Attach scrollbars to older Treeviews created before v6_make_tree was introduced."""
    try:
        if getattr(tree, "_sodb_scrollbars_attached", False):
            return
        parent = tree.master
        if parent is None:
            return
        # If the tree already has both x/y callbacks, it probably lives inside a scroll frame.
        if str(tree.cget("xscrollcommand")) and str(tree.cget("yscrollcommand")):
            tree._sodb_scrollbars_attached = True  # type: ignore[attr-defined]
            return
        manager = tree.winfo_manager()
        if manager == "pack":
            tree.pack_forget()
        elif manager == "grid":
            tree.grid_forget()
        y = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        x = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        try:
            tree.grid(row=0, column=0, sticky="nsew")
            y.grid(row=0, column=1, sticky="ns")
            x.grid(row=1, column=0, sticky="ew")
            parent.rowconfigure(0, weight=1)
            parent.columnconfigure(0, weight=1)
        except tk.TclError:
            # Fallback for containers already managed by pack.
            tree.pack(side="left", fill="both", expand=True)
            y.pack(side="right", fill="y")
            x.pack(side="bottom", fill="x")
        tree._sodb_scrollbars_attached = True  # type: ignore[attr-defined]
    except Exception:
        pass


def _v8_attach_all_scrollbars(self: SodbEditorApp) -> None:
    tree_names = (
        "cases_tree", "people_tree", "passwords_tree", "criminals_tree", "graph_tree", "search_tree",
        "companies_tree", "validation_tree", "backups_tree", "items_tree", "address_tree",
        "det_search_tree", "timeline_tree", "inventory_tree", "sync_tree", "apartment_tree", "stats_tree", "json_tree",
    )
    for name in tree_names:
        tree = getattr(self, name, None)
        if isinstance(tree, ttk.Treeview):
            _v8_attach_scrollbars_to_existing_tree(tree)


def _v8_autofit_tree(self: SodbEditorApp, tree: ttk.Treeview, max_rows: int = 350) -> None:
    """Content-based column sizing.

    Unlike older versions, this does not shrink all columns into the visible area.
    Wide data keeps readable widths and the horizontal scrollbar does the rest.
    """
    try:
        import tkinter.font as tkfont
        font_name = tree.cget("font") or "TkDefaultFont"
        try:
            font = tkfont.nametofont(font_name)
        except Exception:
            font = tkfont.nametofont("TkDefaultFont")
        cols = list(tree["columns"])
        if not cols:
            return
        self._attach_all_scrollbars()
        widths: Dict[str, int] = {}
        for col in cols:
            text = str(tree.heading(col, "text") or col)
            widths[col] = max(65, font.measure(text) + 34)
        for row_i, iid in enumerate(tree.get_children("")):
            if row_i >= max_rows:
                break
            vals = tree.item(iid, "values")
            for col, val in zip(cols, vals):
                sample = str(val)
                # Keep meaningful columns readable without letting one giant JSON cell eat the UI.
                if len(sample) > 140:
                    sample = sample[:140] + "…"
                widths[col] = max(widths[col], min(720, font.measure(sample) + 38))
        for col in cols:
            tree.column(col, width=max(65, widths[col]), minwidth=55, stretch=False)
        try:
            tree.update_idletasks()
        except Exception:
            pass
    except Exception:
        pass


def _v8_autofit_all_trees(self: SodbEditorApp) -> None:
    self._attach_all_scrollbars()
    for name in (
        "cases_tree", "people_tree", "passwords_tree", "criminals_tree", "graph_tree", "search_tree",
        "companies_tree", "validation_tree", "backups_tree", "items_tree", "address_tree",
        "det_search_tree", "timeline_tree", "inventory_tree", "sync_tree", "apartment_tree", "stats_tree", "json_tree",
    ):
        tree = getattr(self, name, None)
        if isinstance(tree, ttk.Treeview):
            self.autofit_tree(tree)


def _v8_autofit_visible_tree(self: SodbEditorApp) -> None:
    self._attach_all_scrollbars()
    # Fit the visible regular tab.
    try:
        current = self.nb.select() if hasattr(self, "nb") else ""
        tab_text = self.nb.tab(current, "text") if current else ""
        by_tab = {
            "Кейсы": "cases_tree", "Люди / связи": "people_tree", "Пароли": "passwords_tree",
            "Убийцы / криминалы": "criminals_tree", "Граф связей": "graph_tree",
            "Поиск evidence/interactables": "search_tree", "Предметы / где лежит": "items_tree",
            "Адреса / комнаты": "address_tree", "Компании / работы": "companies_tree",
            "Валидатор / бэкапы": "validation_tree",
        }
        tree = getattr(self, by_tab.get(tab_text, ""), None)
        if isinstance(tree, ttk.Treeview):
            self.autofit_tree(tree)
    except Exception:
        pass
    # Fit the visible Detective subtab.
    try:
        if getattr(self, "detective_nb", None) is not None:
            sub = self.detective_nb.select()
            sub_text = self.detective_nb.tab(sub, "text") if sub else ""
            by_sub = {
                "Поиск всего": "det_search_tree", "Timeline убийств": "timeline_tree", "Инвентарь": "inventory_tree",
                "Sync disks": "sync_tree", "Квартиры": "apartment_tree", "Статистика": "stats_tree", "JSON Inspector": "json_tree",
            }
            tree = getattr(self, by_sub.get(sub_text, ""), None)
            if isinstance(tree, ttk.Treeview):
                self.autofit_tree(tree)
    except Exception:
        pass


def _v8_setup_style(self: SodbEditorApp) -> None:
    _old_setup_style(self)
    try:
        style = ttk.Style(self)
        style.configure("Detective.TFrame", background="#0e1118")
        style.configure("Card.TFrame", background="#171d29")
        style.configure("CardTitle.TLabel", background="#171d29", foreground="#ff9db7", font=("Segoe UI", 12, "bold"))
        style.configure("CardHint.TLabel", background="#171d29", foreground="#aeb7c6", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 9), padding=(7, 4))
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"), padding=(7, 4))
        style.configure("TNotebook.Tab", padding=(10, 6), font=("Segoe UI", 9))
        style.configure("Treeview", rowheight=25, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
    except Exception:
        pass


def _v8_build_ui(self: SodbEditorApp) -> None:
    _v7_build_ui(self)
    self._attach_all_scrollbars()
    # Keep layout readable after resizing and after switching any notebook level.
    try:
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self.after(160, self.autofit_visible_tree), add="+")
    except Exception:
        pass


def _v8_toggle_detective_mode(self: SodbEditorApp) -> None:
    _v7_toggle_detective_mode(self)
    self._attach_all_scrollbars()
    try:
        if getattr(self, "detective_nb", None) is not None:
            self.detective_nb.bind("<<NotebookTabChanged>>", lambda _e: self.after(160, self.autofit_visible_tree), add="+")
    except Exception:
        pass
    self.after(160, self.autofit_visible_tree)


def _v8_build_det_sync_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="Sync disks")
    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="Фильтр:").pack(side="left")
    self.sync_filter = tk.StringVar()
    ttk.Entry(toolbar, textvariable=self.sync_filter, width=28).pack(side="left", padx=6)
    self.sync_filter.trace_add("write", lambda *_: self.populate_sync_disks())
    ttk.Button(toolbar, text="Добавить", command=self.add_sync_disk_dialog).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Добавить все missing", command=self.add_all_missing_sync_disks).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Удалить", command=self.delete_selected_sync_disk).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Set level", command=self.set_selected_sync_level).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Set state", command=self.set_selected_sync_state).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Экспорт CSV", command=lambda: self._tree_to_csv(self.sync_tree, "sync_disks.csv")).pack(side="left", padx=4)
    ttk.Label(toolbar, text="state = выбранная ветка, level = уровень апгрейда; перед overwrite делай бэкап.", style="Hint.TLabel").pack(side="left", padx=10)
    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Frame(paned, style="Panel.TFrame")
    right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3)
    paned.add(right, weight=2)
    cols = ("idx", "upgrade", "display", "state", "level", "list", "objId", "cost")
    specs = [("idx", 60), ("upgrade", 260), ("display", 210), ("state", 70), ("level", 70), ("list", 70), ("objId", 110), ("cost", 80)]
    self.sync_tree = _v8_make_tree(left, cols, specs, self.on_sync_select)
    self.sync_details = _v7_text_in(right)
    self._sync_cache: List[Dict[str, Any]] = []


def _v8_sync_disk_display_name(code: str) -> str:
    for c, name in SYNC_DISK_CATALOG:
        if c == code:
            return name
    return "—"


def _v8_populate_sync_disks(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "sync_tree"):
        return
    self._sync_cache = self.analyzer.sync_disk_infos(self.sync_filter.get() if hasattr(self, "sync_filter") else "")
    self.sync_tree.delete(*self.sync_tree.get_children())
    columns = list(self.sync_tree["columns"])
    for i, r in enumerate(self._sync_cache):
        values = {
            "idx": r.get("idx", ""),
            "upgrade": r.get("upgrade", ""),
            "display": _v8_sync_disk_display_name(str(r.get("upgrade", ""))),
            "state": r.get("state", ""),
            "level": r.get("level", ""),
            "list": r.get("list", ""),
            "objId": r.get("objId", ""),
            "cost": r.get("cost", ""),
        }
        self.sync_tree.insert("", "end", iid=str(i), values=tuple(values.get(c, "") for c in columns))
    self.after(80, lambda: self.autofit_tree(self.sync_tree))


def _v8_on_sync_select(self: SodbEditorApp, _event=None) -> None:
    sel = self.sync_tree.selection() if hasattr(self, "sync_tree") else []
    if not sel:
        return
    idx = int(sel[0])
    if idx >= len(getattr(self, "_sync_cache", [])):
        return
    r = self._sync_cache[idx]
    code = str(r.get("upgrade", ""))
    text = (
        f"Code: {code}\n"
        f"Name: {_v8_sync_disk_display_name(code)}\n"
        f"State/path: {r.get('state')}\n"
        f"Level: {r.get('level')}\n"
        f"List slot: {r.get('list')}\n"
        f"objId: {r.get('objId')}\n"
        f"uninstallCost: {r.get('cost')}\n\n"
        "RAW:\n" + json.dumps(r.get("raw"), ensure_ascii=False, indent=2)[:120_000]
    )
    self.sync_details.delete("1.0", "end")
    self.sync_details.insert("1.0", text)


def _v8_next_sync_list_value(self: SodbEditorApp) -> int:
    vals: List[int] = []
    for u in self.obj.get("upgrades", []) if self.obj else []:
        if isinstance(u, dict) and isinstance(u.get("list"), int):
            vals.append(u["list"])
    return (max(vals) + 1) if vals else 0


def _v8_add_or_update_sync_disk(self: SodbEditorApp, code: str, state: int = 1, level: int = 0, quiet: bool = False) -> str:
    if not self.obj:
        raise RuntimeError("Сначала открой сейв.")
    if code not in SYNC_DISK_CODES:
        raise RuntimeError(f"Неизвестный SyncDisk code: {code}")
    upgrades = self.obj.setdefault("upgrades", [])
    if not isinstance(upgrades, list):
        raise RuntimeError("obj['upgrades'] не является списком; безопасно добавить диск нельзя.")
    for idx, u in enumerate(upgrades):
        if isinstance(u, dict) and u.get("upgrade") == code:
            if not quiet and not messagebox.askyesno("Sync disk", f"{code} уже есть. Обновить state/level?"):
                return "skip"
            u["state"] = int(state)
            u["level"] = int(level)
            u.setdefault("objId", -1)
            u.setdefault("uninstallCost", 0)
            if not isinstance(u.get("list"), int):
                u["list"] = idx
            return "updated"
    upgrades.append({
        "upgrade": code,
        "state": int(state),
        "list": self._next_sync_list_value(),
        "level": int(level),
        "objId": -1,
        "uninstallCost": 0,
    })
    return "added"


def _v8_add_sync_disk_dialog(self: SodbEditorApp) -> None:
    if not self.obj:
        messagebox.showinfo("Sync disk", "Сначала открой сейв.")
        return
    win = tk.Toplevel(self)
    win.title("Добавить SyncDisk")
    win.geometry("560x260")
    win.configure(bg="#10131a")
    win.transient(self)
    win.grab_set()
    frame = ttk.Frame(win, style="Panel.TFrame")
    frame.pack(fill="both", expand=True, padx=14, pady=14)
    ttk.Label(frame, text="SyncDisk:").grid(row=0, column=0, sticky="e", padx=6, pady=8)
    catalog_values = [f"{code} — {name}" for code, name in SYNC_DISK_CATALOG]
    code_var = tk.StringVar(value=catalog_values[0])
    cb = ttk.Combobox(frame, textvariable=code_var, values=catalog_values, state="readonly", width=48)
    cb.grid(row=0, column=1, columnspan=2, sticky="ew", padx=6, pady=8)
    ttk.Label(frame, text="state / ветка:").grid(row=1, column=0, sticky="e", padx=6, pady=8)
    state_var = tk.IntVar(value=1)
    ttk.Spinbox(frame, from_=0, to=3, textvariable=state_var, width=8).grid(row=1, column=1, sticky="w", padx=6, pady=8)
    ttk.Label(frame, text="level:").grid(row=2, column=0, sticky="e", padx=6, pady=8)
    level_var = tk.IntVar(value=0)
    ttk.Spinbox(frame, from_=0, to=3, textvariable=level_var, width=8).grid(row=2, column=1, sticky="w", padx=6, pady=8)
    hint = "0–3 обычно безопасный диапазон. Если не уверен: state=1, level=0."
    ttk.Label(frame, text=hint, style="Hint.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(6, 12))

    def do_add() -> None:
        try:
            code = code_var.get().split(" — ", 1)[0].strip()
            result = self._add_or_update_sync_disk(code, int(state_var.get()), int(level_var.get()))
            if result != "skip":
                self.mark_dirty(f"SyncDisk {code}: {result}")
                if self.obj:
                    self.analyzer = SodbAnalyzer(self.obj)
                self.populate_sync_disks()
                self.after(80, lambda: self.autofit_tree(self.sync_tree))
            win.destroy()
        except Exception as e:
            messagebox.showerror("Sync disk", str(e), parent=win)

    btns = ttk.Frame(frame, style="Panel.TFrame")
    btns.grid(row=4, column=0, columnspan=3, sticky="e", pady=(8, 0))
    ttk.Button(btns, text="Добавить / обновить", command=do_add).pack(side="left", padx=4)
    ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="left", padx=4)
    frame.columnconfigure(1, weight=1)
    cb.focus_set()


def _v8_add_all_missing_sync_disks(self: SodbEditorApp) -> None:
    if not self.obj:
        messagebox.showinfo("Sync disks", "Сначала открой сейв.")
        return
    if not messagebox.askyesno("Sync disks", "Добавить все отсутствующие SyncDisk'и?\n\nБудут добавлены state=1, level=0. Перед overwrite будет доступен бэкап."):
        return
    try:
        existing = {u.get("upgrade") for u in self.obj.get("upgrades", []) if isinstance(u, dict)}
        added = 0
        for code in SYNC_DISK_CODES:
            if code not in existing:
                self._add_or_update_sync_disk(code, state=1, level=0, quiet=True)
                added += 1
        if self.obj:
            self.analyzer = SodbAnalyzer(self.obj)
        self.mark_dirty(f"Добавлены missing SyncDisk: {added}")
        self.populate_sync_disks()
        self.status_var.set(f"Добавлены отсутствующие SyncDisk: {added}")
    except Exception as e:
        messagebox.showerror("Sync disks", str(e))


def _v8_delete_selected_sync_disk(self: SodbEditorApp) -> None:
    if not self.obj or not hasattr(self, "sync_tree"):
        return
    sel = self.sync_tree.selection()
    if not sel:
        messagebox.showinfo("Sync disk", "Выбери диск в таблице.")
        return
    row = self._sync_cache[int(sel[0])]
    idx = int(row["idx"])
    code = str(row.get("upgrade", ""))
    if not messagebox.askyesno("Sync disk", f"Удалить установленный SyncDisk?\n\n{code}"):
        return
    try:
        del self.obj.get("upgrades", [])[idx]
        if self.obj:
            self.analyzer = SodbAnalyzer(self.obj)
        self.mark_dirty(f"Удалён SyncDisk {code}")
        self.populate_sync_disks()
    except Exception as e:
        messagebox.showerror("Sync disk", str(e))


def _v8_refresh_all(self: SodbEditorApp) -> None:
    _v7_refresh_all(self)
    self._attach_all_scrollbars()
    self.after(150, self.autofit_all_trees)


def _v8_clear_state(self: SodbEditorApp) -> None:
    _v7_clear_state(self)
    self._attach_all_scrollbars()


# Attach v8 patches.
SodbEditorApp._setup_style = _v8_setup_style
SodbEditorApp._build_ui = _v8_build_ui
SodbEditorApp._attach_all_scrollbars = _v8_attach_all_scrollbars
SodbEditorApp.autofit_tree = _v8_autofit_tree
SodbEditorApp.autofit_all_trees = _v8_autofit_all_trees
SodbEditorApp.autofit_visible_tree = _v8_autofit_visible_tree
SodbEditorApp.toggle_detective_mode = _v8_toggle_detective_mode
SodbEditorApp._build_det_sync_tab = _v8_build_det_sync_tab
SodbEditorApp.populate_sync_disks = _v8_populate_sync_disks
SodbEditorApp.on_sync_select = _v8_on_sync_select
SodbEditorApp._next_sync_list_value = _v8_next_sync_list_value
SodbEditorApp._add_or_update_sync_disk = _v8_add_or_update_sync_disk
SodbEditorApp.add_sync_disk_dialog = _v8_add_sync_disk_dialog
SodbEditorApp.add_all_missing_sync_disks = _v8_add_all_missing_sync_disks
SodbEditorApp.delete_selected_sync_disk = _v8_delete_selected_sync_disk
SodbEditorApp.refresh_all = _v8_refresh_all
SodbEditorApp.clear_state = _v8_clear_state


# ---------------------------------------------------------------------------
# v9: precise Detective Mode item tracker / item map by floor
# ---------------------------------------------------------------------------

@dataclass
class TrackedItemInfo:
    idx: int
    iid: str
    preset: str
    name: str
    owner: str
    room_id: str
    room: str
    address: str
    floor_key: str
    floor_label: str
    x: float
    y: float
    z: float
    position: str
    evidence: str
    raw: Dict[str, Any]


def _v9_item_display_name(it: Dict[str, Any]) -> str:
    parts = []
    for key in ("p", "lp", "dds", "bo", "sd", "nEvKey"):
        v = it.get(key)
        if v not in (None, "", -1):
            parts.append(str(v))
    # Preserve order, remove duplicates.
    seen = set()
    out = []
    for x in parts:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return " / ".join(out) if out else "Interactable"


def _v9_item_world_pos(it: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    # wPos is the useful world coordinate. spWPos is a stable fallback stored by the game.
    for key in ("wPos", "spWPos"):
        pos = _v7_pos_tuple(it.get(key))
        if pos:
            return pos
    return None


def _v9_floor_info(self: SodbAnalyzer, it: Dict[str, Any], pos: Optional[Tuple[float, float, float]], room_id: Optional[int]) -> Tuple[str, str]:
    room = self.room_by_id.get(room_id) if isinstance(room_id, int) else None
    fid = room.get("fID") if isinstance(room, dict) else None
    iid = room.get("iID") if isinstance(room, dict) else None
    y = pos[1] if pos else None
    if isinstance(fid, int):
        label = f"fID={fid}"
        if isinstance(iid, int):
            label += f", iID={iid}"
        if isinstance(y, (int, float)):
            label += f", y={y:.1f}"
        return f"fID:{fid}", label
    if isinstance(y, (int, float)):
        # Unknown room floor: keep exact height, grouped by 1-unit buckets so the user can filter it.
        bucket = round(float(y), 1)
        return f"y:{bucket:.1f}", f"y={bucket:.1f}"
    return "unknown", "unknown"


def _v9_tracked_item_infos(self: SodbAnalyzer, query: str = "", floor_filter: str = "all", limit: int = 8000) -> List[TrackedItemInfo]:
    q = (query or "").strip().lower()
    ff = (floor_filter or "all").strip()
    rows: List[TrackedItemInfo] = []
    for idx, it in enumerate(self.obj.get("interactables", []) or []):
        if not isinstance(it, dict):
            continue
        pos = _v9_item_world_pos(it)
        if not pos:
            continue
        iid = str(it.get("id", ""))
        preset = str(it.get("p") or it.get("lp") or "Interactable")
        name = _v9_item_display_name(it)
        owner_id = it.get("w") if isinstance(it.get("w"), int) and it.get("w") > 0 else it.get("inv")
        owner = self.human_name(owner_id) if isinstance(owner_id, int) and owner_id > 0 else "—"
        room_id_val = it.get("r") if isinstance(it.get("r"), int) and it.get("r") >= 0 else None
        room = self.room_label(room_id_val) if isinstance(room_id_val, int) else "—"
        addr_id = self.room_address_map.get(room_id_val) if isinstance(room_id_val, int) else None
        address = self.address_label(addr_id) if isinstance(addr_id, int) else "—"
        floor_key, floor_label = self._tracked_item_floor_info(it, pos, room_id_val)
        if ff not in ("", "all", "Все", "All") and floor_key != ff:
            continue
        evidence = str(it.get("nEvKey") or "—")
        position = f"x={pos[0]:.2f}, y={pos[1]:.2f}, z={pos[2]:.2f}"
        rawtxt = _v6_compact_json(it, 2600)
        hay = _v7_hay(iid, preset, name, owner, room, address, floor_label, position, evidence, rawtxt)
        if q and q not in hay:
            continue
        rows.append(TrackedItemInfo(
            idx=idx,
            iid=iid,
            preset=preset or "—",
            name=name,
            owner=owner,
            room_id=str(room_id_val) if isinstance(room_id_val, int) else "—",
            room=room,
            address=address,
            floor_key=floor_key,
            floor_label=floor_label,
            x=float(pos[0]),
            y=float(pos[1]),
            z=float(pos[2]),
            position=position,
            evidence=evidence,
            raw=it,
        ))
        if len(rows) >= limit:
            break
    return rows


def _v9_tracked_item_floors(self: SodbAnalyzer, query: str = "") -> List[Tuple[str, str, int]]:
    counts: Dict[str, int] = {}
    for row in self.tracked_item_infos(query=query, floor_filter="all", limit=20000):
        counts[row.floor_key] = counts.get(row.floor_key, 0) + 1

    def label_for(key: str) -> str:
        m = re.match(r"fID:(-?\d+)", key)
        if m:
            return f"fID={m.group(1)}"
        m = re.match(r"y:(-?\d+(?:\.\d+)?)", key)
        if m:
            return f"y={float(m.group(1)):.1f}"
        return key

    def sort_key(pair: Tuple[str, int]):
        key, _count = pair
        m = re.match(r"fID:(-?\d+)", key)
        if m:
            return (0, int(m.group(1)), key)
        m = re.match(r"y:(-?\d+(?:\.\d+)?)", key)
        if m:
            return (1, float(m.group(1)), key)
        return (2, 0, key)

    return [(key, label_for(key), count) for key, count in sorted(counts.items(), key=sort_key)]


SodbAnalyzer._tracked_item_floor_info = _v9_floor_info
SodbAnalyzer.tracked_item_infos = _v9_tracked_item_infos
SodbAnalyzer.tracked_item_floors = _v9_tracked_item_floors


def _v9_build_detective_mode_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.nb, style="Detective.TFrame")
    self.detective_tab = tab
    header = ttk.Frame(tab, style="Detective.TFrame")
    header.pack(fill="x", padx=12, pady=(10, 6))
    ttk.Label(header, text="Detective Mode", style="Title.TLabel").pack(side="left")
    ttk.Label(header, text="  включаемая витрина: поиск, timeline, карта, трекер предметов, inventory, sync disks, квартиры, статистика, JSON", style="Hint.TLabel").pack(side="left", padx=8)
    ttk.Button(header, text="Обновить", command=self.populate_detective_all).pack(side="right", padx=3)
    ttk.Button(header, text="Скрыть", command=lambda: (self.detective_mode_var.set(False), self.toggle_detective_mode())).pack(side="right", padx=3)

    self.detective_nb = ttk.Notebook(tab)
    self.detective_nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    self._build_det_global_tab()
    self._build_det_profile_tab()
    self._build_det_timeline_tab()
    self._build_det_map_tab()
    self._build_det_item_tracker_tab()
    self._build_det_inventory_tab()
    self._build_det_sync_tab()
    self._build_det_apartments_tab()
    self._build_det_stats_tab()
    self._build_det_json_tab()


def _v9_build_det_item_tracker_tab(self: SodbEditorApp) -> None:
    tab = ttk.Frame(self.detective_nb, style="Panel.TFrame")
    self.detective_nb.add(tab, text="Трекер предметов")

    toolbar = ttk.Frame(tab, style="Panel.TFrame")
    toolbar.pack(fill="x", padx=10, pady=(10, 4))
    ttk.Label(toolbar, text="Предмет:").pack(side="left")
    self.item_track_filter = tk.StringVar()
    ent = ttk.Entry(toolbar, textvariable=self.item_track_filter, width=32)
    ent.pack(side="left", padx=6)
    ent.bind("<Return>", lambda _e: self.populate_item_tracker())
    self.item_track_filter.trace_add("write", lambda *_: self._schedule_item_tracker_refresh())

    ttk.Label(toolbar, text="Этаж:").pack(side="left", padx=(8, 0))
    self.item_track_floor = tk.StringVar(value="all")
    self.item_track_floor_combo = ttk.Combobox(toolbar, textvariable=self.item_track_floor, width=26, state="readonly", values=("all",))
    self.item_track_floor_combo.pack(side="left", padx=6)
    self.item_track_floor_combo.bind("<<ComboboxSelected>>", lambda _e: self.populate_item_tracker())

    self.item_track_selected_only = tk.BooleanVar(value=False)
    ttk.Checkbutton(toolbar, text="только выбранный", variable=self.item_track_selected_only, command=self.draw_item_tracker_map).pack(side="left", padx=8)
    ttk.Button(toolbar, text="Найти на карте", command=self.focus_selected_tracked_item).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Копировать координаты", command=self.copy_selected_tracked_item_position).pack(side="left", padx=4)
    ttk.Button(toolbar, text="Экспорт CSV", command=lambda: self._tree_to_csv(self.item_track_tree, "tracked_items.csv")).pack(side="left", padx=4)
    ttk.Label(toolbar, text="пустой фильтр = все предметы; пример: Envelope / SealedEnvelope / SniperRifle", style="Hint.TLabel").pack(side="left", padx=10)

    paned = ttk.Panedwindow(tab, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    left = ttk.Panedwindow(paned, orient="vertical")
    right = ttk.Frame(paned, style="Panel.TFrame")
    paned.add(left, weight=3)
    paned.add(right, weight=2)

    table_frame = ttk.Frame(left, style="Panel.TFrame")
    detail_frame = ttk.Frame(left, style="Panel.TFrame")
    left.add(table_frame, weight=3)
    left.add(detail_frame, weight=1)

    cols = ("id", "preset", "name", "owner", "floor", "room", "address", "x", "y", "z", "evidence")
    specs = [
        ("id", 100), ("preset", 170), ("name", 220), ("owner", 190), ("floor", 150),
        ("room", 260), ("address", 290), ("x", 80), ("y", 80), ("z", 80), ("evidence", 120),
    ]
    self.item_track_tree = _v8_make_tree(table_frame, cols, specs, self.on_item_tracker_select)
    self.item_track_details = _v7_text_in(detail_frame)

    map_toolbar = ttk.Frame(right, style="Panel.TFrame")
    map_toolbar.pack(fill="x", padx=4, pady=(0, 4))
    ttk.Label(map_toolbar, text="Карта X/Z, высота Y и floorID берутся из world position + Room.fID", style="Hint.TLabel").pack(side="left")
    self.item_track_canvas = tk.Canvas(right, bg="#0f1320", highlightthickness=0)
    self.item_track_canvas.pack(fill="both", expand=True)
    self.item_track_canvas.bind("<Configure>", lambda _e: self.draw_item_tracker_map())
    self.item_track_canvas.bind("<Button-1>", self.on_item_tracker_map_click)

    self._item_track_cache: List[TrackedItemInfo] = []
    self._item_track_map_points: List[Tuple[float, float, int]] = []
    self._item_track_refresh_job = None


def _v9_schedule_item_tracker_refresh(self: SodbEditorApp) -> None:
    try:
        if getattr(self, "_item_track_refresh_job", None):
            self.after_cancel(self._item_track_refresh_job)
    except Exception:
        pass
    self._item_track_refresh_job = self.after(300, self.populate_item_tracker)


def _v9_update_item_floor_combo(self: SodbEditorApp, query: str) -> None:
    if not self.analyzer or not hasattr(self, "item_track_floor_combo"):
        return
    try:
        floors = self.analyzer.tracked_item_floors(query)
        values = ["all"] + [f"{key} — {label} ({count})" for key, label, count in floors]
        current = self.item_track_floor.get() or "all"
        current_key = current.split(" — ", 1)[0]
        self.item_track_floor_combo.configure(values=values)
        if current_key != "all" and not any(v.startswith(current_key + " —") for v in values):
            self.item_track_floor.set("all")
    except Exception:
        pass


def _v9_current_item_floor_key(self: SodbEditorApp) -> str:
    raw = self.item_track_floor.get() if hasattr(self, "item_track_floor") else "all"
    return (raw or "all").split(" — ", 1)[0]


def _v9_populate_item_tracker(self: SodbEditorApp) -> None:
    if not self.analyzer or not hasattr(self, "item_track_tree"):
        return
    q = self.item_track_filter.get() if hasattr(self, "item_track_filter") else ""
    self._update_item_floor_combo(q)
    floor_key = self._current_item_floor_key()
    self._item_track_cache = self.analyzer.tracked_item_infos(q, floor_key, limit=8000)
    self.item_track_tree.delete(*self.item_track_tree.get_children())
    for i, it in enumerate(self._item_track_cache):
        self.item_track_tree.insert("", "end", iid=str(i), values=(
            it.iid, it.preset, it.name, it.owner, it.floor_label, it.room, it.address,
            f"{it.x:.2f}", f"{it.y:.2f}", f"{it.z:.2f}", it.evidence,
        ))
    self.status_var.set(f"Трекер предметов: {len(self._item_track_cache)} item(s)")
    self.after(80, lambda: self.autofit_tree(self.item_track_tree))
    self.after(90, self.draw_item_tracker_map)


def _v9_on_item_tracker_select(self: SodbEditorApp, _event=None) -> None:
    if not hasattr(self, "item_track_tree") or not hasattr(self, "item_track_details"):
        return
    sel = self.item_track_tree.selection()
    if not sel:
        return
    idx = int(sel[0])
    if idx < 0 or idx >= len(getattr(self, "_item_track_cache", [])):
        return
    it = self._item_track_cache[idx]
    text = (
        f"Item {it.iid}\n"
        f"Preset: {it.preset}\n"
        f"Name: {it.name}\n"
        f"Owner: {it.owner}\n"
        f"Floor: {it.floor_label}\n"
        f"Room: {it.room}\n"
        f"Address: {it.address}\n"
        f"Position: {it.position}\n"
        f"Evidence: {it.evidence}\n\n"
        "RAW:\n" + json.dumps(it.raw, ensure_ascii=False, indent=2)[:160_000]
    )
    self.item_track_details.delete("1.0", "end")
    self.item_track_details.insert("1.0", text)
    self.draw_item_tracker_map()


def _v9_selected_tracked_item_index(self: SodbEditorApp) -> Optional[int]:
    if not hasattr(self, "item_track_tree"):
        return None
    sel = self.item_track_tree.selection()
    if not sel:
        return None
    try:
        idx = int(sel[0])
    except Exception:
        return None
    if 0 <= idx < len(getattr(self, "_item_track_cache", [])):
        return idx
    return None


def _v9_focus_selected_tracked_item(self: SodbEditorApp) -> None:
    idx = self._selected_tracked_item_index()
    if idx is None:
        messagebox.showinfo("Трекер предметов", "Выбери предмет в таблице.")
        return
    self.item_track_selected_only.set(True)
    self.draw_item_tracker_map()
    it = self._item_track_cache[idx]
    self.status_var.set(f"Выделен Item {it.iid}: {it.preset} @ {it.position}")


def _v9_copy_selected_tracked_item_position(self: SodbEditorApp) -> None:
    idx = self._selected_tracked_item_index()
    if idx is None:
        messagebox.showinfo("Трекер предметов", "Выбери предмет в таблице.")
        return
    it = self._item_track_cache[idx]
    text = f"Item {it.iid} | {it.preset} | {it.position} | {it.floor_label} | {it.room} | {it.address}"
    self.clipboard_clear()
    self.clipboard_append(text)
    self.status_var.set("Координаты предмета скопированы")


def _v9_draw_item_tracker_map(self: SodbEditorApp) -> None:
    if not hasattr(self, "item_track_canvas"):
        return
    canvas = self.item_track_canvas
    canvas.delete("all")
    self._item_track_map_points = []
    w = max(canvas.winfo_width(), 420)
    h = max(canvas.winfo_height(), 320)
    rows: List[TrackedItemInfo] = list(getattr(self, "_item_track_cache", []) or [])
    selected_idx = self._selected_tracked_item_index()
    if getattr(self, "item_track_selected_only", None) and self.item_track_selected_only.get() and selected_idx is not None:
        rows = [self._item_track_cache[selected_idx]]
    if not rows:
        canvas.create_text(w / 2, h / 2, text="Нет предметов с координатами для текущего фильтра", fill="#d8dde8", font=("Segoe UI", 12))
        return

    xs = [r.x for r in rows]
    zs = [r.z for r in rows]
    minx, maxx = min(xs), max(xs)
    minz, maxz = min(zs), max(zs)
    if maxx == minx:
        maxx += 1.0
        minx -= 1.0
    if maxz == minz:
        maxz += 1.0
        minz -= 1.0
    pad = 42

    def tx(x: float) -> float:
        return pad + (x - minx) / (maxx - minx) * (w - pad * 2)

    def tz(z: float) -> float:
        return h - pad - (z - minz) / (maxz - minz) * (h - pad * 2)

    canvas.create_rectangle(pad, pad, w - pad, h - pad, outline="#2c3952")
    for i in range(1, 8):
        gx = pad + (w - pad * 2) * i / 8
        gy = pad + (h - pad * 2) * i / 8
        canvas.create_line(gx, pad, gx, h - pad, fill="#151b2a")
        canvas.create_line(pad, gy, w - pad, gy, fill="#151b2a")
    canvas.create_text(pad, 18, text=f"X/Z map | floor={self._current_item_floor_key()} | items={len(rows)}", fill="#aeb7c6", anchor="w", font=("Segoe UI", 9))
    canvas.create_text(w - pad, h - 16, text=f"x {minx:.1f}..{maxx:.1f} | z {minz:.1f}..{maxz:.1f}", fill="#647089", anchor="e", font=("Segoe UI", 8))

    # Use a stable index lookup so clicking map can select the source row.
    index_by_iid_and_pos = {(r.iid, round(r.x, 3), round(r.y, 3), round(r.z, 3)): i for i, r in enumerate(getattr(self, "_item_track_cache", []) or [])}
    max_draw = 3000
    for draw_i, r in enumerate(rows[:max_draw]):
        sx, sy = tx(r.x), tz(r.z)
        real_idx = index_by_iid_and_pos.get((r.iid, round(r.x, 3), round(r.y, 3), round(r.z, 3)), draw_i)
        is_sel = selected_idx is not None and real_idx == selected_idx
        fill = "#ff5f7e" if is_sel else "#77a7ff"
        outline = "#ffd1dc" if is_sel else ""
        radius = 7 if is_sel else 3
        if "envelope" in (r.preset + " " + r.name).lower():
            fill = "#ffbd66" if not is_sel else "#ff5f7e"
            radius = max(radius, 5)
        canvas.create_oval(sx - radius, sy - radius, sx + radius, sy + radius, fill=fill, outline=outline, width=2 if outline else 0)
        self._item_track_map_points.append((sx, sy, real_idx))
        if is_sel or len(rows) <= 80:
            label = f"{r.preset} #{r.iid}"
            canvas.create_text(sx + 8, sy - 8, text=label[:42], fill="#d8dde8", anchor="w", font=("Segoe UI", 8))
    if len(rows) > max_draw:
        canvas.create_text(w / 2, h - 18, text=f"Показаны первые {max_draw} из {len(rows)}. Уточни фильтр для точного предмета.", fill="#ffbd66", font=("Segoe UI", 9))

    if selected_idx is not None and selected_idx < len(getattr(self, "_item_track_cache", [])):
        r = self._item_track_cache[selected_idx]
        canvas.create_rectangle(12, h - 82, min(w - 12, 720), h - 12, fill="#171d29", outline="#70324a")
        canvas.create_text(24, h - 68, text=f"Selected: {r.preset} / Item {r.iid}", fill="#ffffff", anchor="w", font=("Segoe UI", 10, "bold"))
        canvas.create_text(24, h - 48, text=f"{r.position} | {r.floor_label}", fill="#d8dde8", anchor="w", font=("Segoe UI", 9))
        canvas.create_text(24, h - 28, text=f"{r.room} | {r.address}", fill="#aeb7c6", anchor="w", font=("Segoe UI", 8))


def _v9_on_item_tracker_map_click(self: SodbEditorApp, event) -> None:
    pts = getattr(self, "_item_track_map_points", []) or []
    if not pts or not hasattr(self, "item_track_tree"):
        return
    best = None
    best_d2 = 999999.0
    for sx, sy, idx in pts:
        d2 = (sx - event.x) ** 2 + (sy - event.y) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best = idx
    if best is None or best_d2 > 24 ** 2:
        return
    iid = str(best)
    if self.item_track_tree.exists(iid):
        self.item_track_tree.selection_set(iid)
        self.item_track_tree.see(iid)
        self.on_item_tracker_select()


def _v9_populate_detective_all(self: SodbEditorApp) -> None:
    _v7_populate_detective_all(self)
    if hasattr(self, "item_track_tree"):
        self.populate_item_tracker()


def _v9_clear_state(self: SodbEditorApp) -> None:
    _v8_clear_state(self)
    tree = getattr(self, "item_track_tree", None)
    if tree is not None:
        try:
            tree.delete(*tree.get_children())
        except Exception:
            pass
    txt = getattr(self, "item_track_details", None)
    if txt is not None:
        try:
            txt.delete("1.0", "end")
        except Exception:
            pass
    if hasattr(self, "item_track_canvas"):
        self.item_track_canvas.delete("all")
    for name, val in (("item_track_filter", ""), ("item_track_floor", "all")):
        var = getattr(self, name, None)
        if var is not None:
            try:
                var.set(val)
            except Exception:
                pass
    self._item_track_cache = []
    self._item_track_map_points = []


def _v9_autofit_all_trees(self: SodbEditorApp) -> None:
    _v8_autofit_all_trees(self)
    tree = getattr(self, "item_track_tree", None)
    if isinstance(tree, ttk.Treeview):
        self.autofit_tree(tree)


def _v9_autofit_visible_tree(self: SodbEditorApp) -> None:
    _v8_autofit_visible_tree(self)
    try:
        if getattr(self, "detective_nb", None) is not None:
            sub = self.detective_nb.select()
            sub_text = self.detective_nb.tab(sub, "text") if sub else ""
            if sub_text == "Трекер предметов" and isinstance(getattr(self, "item_track_tree", None), ttk.Treeview):
                self.autofit_tree(self.item_track_tree)
    except Exception:
        pass


def _v9_attach_all_scrollbars(self: SodbEditorApp) -> None:
    _v8_attach_all_scrollbars(self)
    tree = getattr(self, "item_track_tree", None)
    if isinstance(tree, ttk.Treeview):
        _v8_attach_scrollbars_to_existing_tree(tree)


def _v9_export_current_table_csv(self: SodbEditorApp) -> None:
    current = self.nb.select()
    tab_text = self.nb.tab(current, "text") if current else ""
    if tab_text == "Detective Mode" and getattr(self, "detective_nb", None) is not None:
        sub = self.detective_nb.select()
        sub_text = self.detective_nb.tab(sub, "text") if sub else ""
        if sub_text == "Трекер предметов" and hasattr(self, "item_track_tree"):
            self._tree_to_csv(self.item_track_tree, "tracked_items.csv")
            return
    _v7_export_current_table_csv(self)


# Attach v9 patches.
SodbEditorApp._build_detective_mode_tab = _v9_build_detective_mode_tab
SodbEditorApp._build_det_item_tracker_tab = _v9_build_det_item_tracker_tab
SodbEditorApp._schedule_item_tracker_refresh = _v9_schedule_item_tracker_refresh
SodbEditorApp._update_item_floor_combo = _v9_update_item_floor_combo
SodbEditorApp._current_item_floor_key = _v9_current_item_floor_key
SodbEditorApp.populate_item_tracker = _v9_populate_item_tracker
SodbEditorApp.on_item_tracker_select = _v9_on_item_tracker_select
SodbEditorApp._selected_tracked_item_index = _v9_selected_tracked_item_index
SodbEditorApp.focus_selected_tracked_item = _v9_focus_selected_tracked_item
SodbEditorApp.copy_selected_tracked_item_position = _v9_copy_selected_tracked_item_position
SodbEditorApp.draw_item_tracker_map = _v9_draw_item_tracker_map
SodbEditorApp.on_item_tracker_map_click = _v9_on_item_tracker_map_click
SodbEditorApp.populate_detective_all = _v9_populate_detective_all
SodbEditorApp.clear_state = _v9_clear_state
SodbEditorApp.autofit_all_trees = _v9_autofit_all_trees
SodbEditorApp.autofit_visible_tree = _v9_autofit_visible_tree
SodbEditorApp._attach_all_scrollbars = _v9_attach_all_scrollbars
SodbEditorApp.export_current_table_csv = _v9_export_current_table_csv

def main() -> None:
    if brotli is None:
        # GUI всё равно запустим, чтобы сообщение было видным.
        pass
    app = SodbEditorApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        app.path = p
        app.path_var.set(str(p))
        app.after(100, app.decode_current)
    app.mainloop()


if __name__ == "__main__":
    main()
