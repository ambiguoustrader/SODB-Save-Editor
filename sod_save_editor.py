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
- умеет экспортировать JSON и сохранять обратно .sodb.

Зависимости: Python 3.10+, tkinter, brotli.
Установка brotli:  python -m pip install brotli
"""

from __future__ import annotations

import copy
import json
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
        self.name_map: Dict[int, str] = {}
        self.role_map: Dict[int, str] = {}
        self.roster_source: Dict[int, str] = {}
        self._build_people_names()
        self.job_by_id = self._build_jobs()

    def human_name(self, cid: Any) -> str:
        try:
            i = int(cid)
        except Exception:
            return str(cid)
        name = self.name_map.get(i, "")
        return "—" if i <= 0 else (f"{name} (Human{i})" if name else f"Human{i}")

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
        room = next((r for r in self.obj.get("rooms", []) or [] if isinstance(r, dict) and r.get("id") == rid), None)
        if not room:
            return f"Room{rid}"
        bits = [f"Room{rid}"]
        if room.get("fID") is not None:
            bits.append(f"floor/fID={room.get('fID')}")
        if room.get("iID") is not None:
            bits.append(f"interior/iID={room.get('iID')}")
        if room.get("ml") is not None:
            bits.append(f"main={room.get('ml')}")
        return " (" + ", ".join(bits[1:]) + ")" if len(bits) == 1 else bits[0] + " (" + ", ".join(bits[1:]) + ")"

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
        self.geometry("1320x820")
        self.minsize(1000, 650)
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
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("hucker9996.sod.save.editor")
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

        self.status_var = tk.StringVar(value="Открой .sodb файл. Перед overwrite лучше закрыть игру и сделать бэкап.")
        ttk.Label(self, textvariable=self.status_var, style="Hint.TLabel").pack(fill="x", padx=14, pady=(0, 8))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        self._build_dashboard_tab()
        self._build_cases_tab()
        self._build_people_tab()
        self._build_passwords_tab()
        self._build_criminals_tab()
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
        for tree_name in ("cases_tree", "people_tree", "passwords_tree", "criminals_tree"):
            tree = getattr(self, tree_name, None)
            if tree is not None:
                try:
                    tree.delete(*tree.get_children())
                except Exception:
                    pass
        for text_name in ("dashboard_text", "case_details", "person_details", "password_details", "raw_text", "path_output"):
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
