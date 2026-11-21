#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeckWatch — monitoraggio del tempo di gioco per Steam Deck (SteamOS).

Funzioni:
  * misura il tempo di utilizzo per applicazione/gioco (solo app in primo piano,
    Deck non sospesa, con input recente da tastiera/mouse/touch/gamepad);
  * limiti "soft" (solo avviso) e "hard" (chiusura dopo un tempo di grazia),
    per app e totali, diversi per giorno della settimana;
  * report giornaliero ai genitori (Telegram) e al figlio (email e/o Telegram);
  * comandi remoti via bot Telegram: blocco (kill-switch), sblocco, minuti extra,
    stato, spegnimento.

Solo libreria standard di Python (>= 3.11): niente pip, sopravvive agli
aggiornamenti di SteamOS perché vive tutto nella home dell'utente.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import datetime as dt
import fnmatch
import glob
import json
import logging
import os
import queue
import re
import select
import shutil
import signal
import smtplib
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None

__version__ = "0.2.0"

APP = "deckwatch"
TICK = 5.0
log = logging.getLogger(APP)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
GROUPS = {"weekdays": DAYS[:5], "weekend": DAYS[5:]}
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]

# Steam: appid "speciali" che non sono giochi (769 = interfaccia Steam in gamescope)
NON_GAME_APPIDS = {"", "0", "769"}
STEAM_CLIENT_COMMS = {"steam", "steamwebhelper", "steamservice", "steam-runtime-l", "steam.sh"}

DEFAULT_EXCLUDE = [
    "class:plasmashell", "class:krunner", "class:kdialog", "class:zenity",
    "class:ksmserver*", "class:kwin*", "class:org.kde.polkit*", "class:polkit*",
    "steam:ui",
]
DEFAULT_ALLOWED = [
    "class:steam", "class:steamwebhelper", "class:systemsettings",
    "class:org.kde.dolphin", "class:dolphin", "class:konsole", "class:org.kde.konsole",
]


# --------------------------------------------------------------------------- #
# Utilità
# --------------------------------------------------------------------------- #
def load_toml_text(text: str) -> dict:
    if tomllib is None:
        raise RuntimeError("Serve Python 3.11 o superiore (modulo tomllib)")
    if text.startswith("﻿"):
        text = text[1:]
    return tomllib.loads(text)


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def fmt_min(minutes: float | None) -> str:
    if minutes is None:
        return "nessun limite"
    neg = minutes < 0
    m = int(round(abs(minutes)))
    h, mm = divmod(m, 60)
    s = f"{h}h {mm:02d}m" if h else f"{mm} min"
    return "-" + s if neg else s


def parse_version(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def http_request(url: str, data: dict | None = None, timeout: float = 20) -> bytes:
    headers = {"User-Agent": f"{APP}/{__version__}", "Cache-Control": "no-cache"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(4 * 1024 * 1024)


def read_environ(pid: int) -> dict:
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for item in data.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def proc_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def own_pids():
    uid = os.getuid()
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid != uid:
                continue
        except OSError:
            continue
        yield int(d)


def steam_appid_from_env(env: dict) -> str | None:
    for k in ("SteamAppId", "SteamGameId"):
        v = (env.get(k) or "").strip()
        if v and v not in NON_GAME_APPIDS:
            return v
    return None


# --------------------------------------------------------------------------- #
# Percorsi e configurazione
# --------------------------------------------------------------------------- #
@dataclass
class Paths:
    config_dir: Path
    state_dir: Path

    @classmethod
    def default(cls) -> "Paths":
        home = Path.home()
        cfg = os.environ.get("DECKWATCH_CONFIG_DIR") or \
            str(Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / APP)
        st = os.environ.get("DECKWATCH_STATE_DIR") or \
            str(Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state") / APP)
        return cls(Path(cfg), Path(st))

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def policy_file(self) -> Path:
        return self.config_dir / "policy.toml"

    @property
    def days_dir(self) -> Path:
        return self.state_dir / "days"

    @property
    def outbox_dir(self) -> Path:
        return self.state_dir / "outbox"

    @property
    def overrides_file(self) -> Path:
        return self.state_dir / "overrides.json"

    @property
    def meta_file(self) -> Path:
        return self.state_dir / "meta.json"


@dataclass
class Config:
    update_url: str = ""
    auto_update: bool = True
    tg_token: str = ""
    tg_parents: list = field(default_factory=list)
    tg_kid: list = field(default_factory=list)
    smtp_host: str = "smtp-relay.brevo.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_sender: str = ""
    email_kid: list = field(default_factory=list)
    email_parents: list = field(default_factory=list)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_token)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password and self.smtp_sender)

    @classmethod
    def load(cls, path: Path) -> "Config":
        c = cls()
        if not path.exists():
            log.warning("Config %s non trovata: uso i valori predefiniti", path)
            return c
        d = load_toml_text(path.read_text(encoding="utf-8"))
        c.update_url = str(d.get("update_url", "")).strip()
        c.auto_update = bool(d.get("auto_update", True))
        tg = d.get("telegram", {}) or {}
        c.tg_token = str(tg.get("token", "")).strip()
        c.tg_parents = [int(x) for x in tg.get("parents", [])]
        c.tg_kid = [int(x) for x in tg.get("kid", [])]
        em = d.get("email", {}) or {}
        c.smtp_host = str(em.get("smtp_host", c.smtp_host))
        c.smtp_port = int(em.get("smtp_port", c.smtp_port))
        c.smtp_user = str(em.get("username", ""))
        c.smtp_password = str(em.get("password", ""))
        c.smtp_sender = str(em.get("sender", ""))
        c.email_kid = [str(x) for x in em.get("kid", [])]
        c.email_parents = [str(x) for x in em.get("parents", [])]
        return c


# --------------------------------------------------------------------------- #
# Policy (regole/limiti) — file policy.toml sulla Deck
# --------------------------------------------------------------------------- #
class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Limit:
    soft: float | None = None
    hard: float | None = None

    def plus(self, m: float) -> "Limit":
        return Limit(None if self.soft is None else max(0.0, self.soft + m),
                     None if self.hard is None else max(0.0, self.hard + m))


@dataclass
class AppRule:
    id: str
    name: str
    patterns: list
    limits: dict


def _num(v, where: str):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PolicyError(f"{where}: atteso un numero (minuti), trovato {v!r}")
    if v < 0:
        raise PolicyError(f"{where}: il valore non può essere negativo")
    return float(v)


def _patterns(v, where: str) -> list:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise PolicyError(f"{where}: attesa una lista di testi, es. [\"steam:322170\"]")
    return [x.strip().lower() for x in v if x.strip()]


def _limit_table(tbl: dict, where: str, extra_keys=()) -> dict:
    out = {"base": Limit(_num(tbl.get("soft"), f"{where}.soft"),
                         _num(tbl.get("hard"), f"{where}.hard"))}
    for k, v in tbl.items():
        if k in ("soft", "hard") or k in extra_keys:
            continue
        if k in DAYS or k in GROUPS:
            if not isinstance(v, dict):
                raise PolicyError(f"{where}.{k}: atteso es. {{ soft = 60, hard = 90 }}")
            for kk in v:
                if kk not in ("soft", "hard"):
                    raise PolicyError(f"{where}.{k}: chiave sconosciuta '{kk}'")
            out[k] = Limit(_num(v.get("soft"), f"{where}.{k}.soft"),
                           _num(v.get("hard"), f"{where}.{k}.hard"))
        else:
            raise PolicyError(
                f"{where}: chiave sconosciuta '{k}' (giorni validi: {', '.join(DAYS)}, weekdays, weekend)")
    return out


def _resolve(lt: dict, day: str) -> Limit:
    soft, hard = lt["base"].soft, lt["base"].hard
    for k in ("weekend" if day in GROUPS["weekend"] else "weekdays", day):
        o = lt.get(k)
        if o:
            soft = o.soft if o.soft is not None else soft
            hard = o.hard if o.hard is not None else hard
    return Limit(soft, hard)


def _match(patterns: list, key: str, name: str) -> bool:
    k, n = key.lower(), (name or "").lower()
    for p in patterns:
        if p.startswith("name:"):
            if fnmatch.fnmatchcase(n, p[5:]):
                return True
        elif fnmatch.fnmatchcase(k, p):
            return True
    return False


def _parse_lock_until(v):
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None) if v.tzinfo is None else v.astimezone().replace(tzinfo=None)
    if isinstance(v, dt.date):
        return dt.datetime.combine(v, dt.time())
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v)
        except ValueError:
            pass
    raise PolicyError(f"lock_until non valido: {v!r} (es. 2026-11-20T08:00:00)")


class Policy:
    TOP_KEYS = {"mode", "lock", "lock_until", "lock_message", "lock_grace_seconds", "idle_minutes",
                "alerts", "exclude", "always_allowed", "grace", "total", "apps", "exceptions"}

    def __init__(self, data: dict):
        if not isinstance(data, dict):
            raise PolicyError("policy non valida")
        unknown = set(data) - self.TOP_KEYS
        if unknown:
            raise PolicyError(f"chiavi sconosciute: {', '.join(sorted(unknown))}")
        self.idle_minutes = _num(data.get("idle_minutes", 3), "idle_minutes") or 3.0
        self.lock = bool(data.get("lock", False))
        self.lock_until = _parse_lock_until(data.get("lock_until"))
        self.lock_message = str(data.get("lock_message",
                                         "Mamma e papà hanno messo in pausa la Deck."))
        self.lock_grace_seconds = _num(data.get("lock_grace_seconds", 60), "lock_grace_seconds")
        self.alerts = bool(data.get("alerts", True))
        self.mode = str(data.get("mode", "monitor"))
        if self.mode not in ("monitor", "enforce"):
            raise PolicyError('mode: valori ammessi "monitor" o "enforce"')
        self.exclude = _patterns(data.get("exclude", DEFAULT_EXCLUDE), "exclude")
        self.always_allowed = _patterns(data.get("always_allowed", DEFAULT_ALLOWED), "always_allowed")

        g = data.get("grace", {})
        if not isinstance(g, dict):
            raise PolicyError("[grace] non valido")
        for k in g:
            if k not in ("minutes", "warn_before", "on_total_exceeded", "relaunch_seconds"):
                raise PolicyError(f"grace: chiave sconosciuta '{k}'")
        self.grace_minutes = _num(g.get("minutes", 5), "grace.minutes")
        wb = g.get("warn_before", [10, 5, 1])
        if not isinstance(wb, list):
            raise PolicyError("grace.warn_before: attesa una lista, es. [10, 5, 1]")
        self.warn_before = sorted({_num(x, "grace.warn_before") for x in wb}, reverse=True)
        self.on_total_exceeded = str(g.get("on_total_exceeded", "kill"))
        if self.on_total_exceeded not in ("kill", "shutdown"):
            raise PolicyError("grace.on_total_exceeded: valori ammessi \"kill\" o \"shutdown\"")
        self.relaunch_seconds = _num(g.get("relaunch_seconds", 20), "grace.relaunch_seconds")

        total = data.get("total", {})
        if not isinstance(total, dict):
            raise PolicyError("[total] non valido")
        self.total = _limit_table(total, "total")

        self.apps: dict[str, AppRule] = {}
        apps = data.get("apps", {})
        if not isinstance(apps, dict):
            raise PolicyError("[apps] non valido")
        for aid, a in apps.items():
            where = f"apps.{aid}"
            if not isinstance(a, dict):
                raise PolicyError(f"{where}: non valido")
            if "match" not in a:
                raise PolicyError(f"{where}: manca 'match' (es. match = [\"steam:322170\"])")
            self.apps[aid] = AppRule(
                id=aid, name=str(a.get("name", aid)),
                patterns=_patterns(a["match"], f"{where}.match"),
                limits=_limit_table(a, where, extra_keys=("match", "name")))

        self.exceptions: dict[str, dict] = {}
        ex = data.get("exceptions", {})
        if not isinstance(ex, dict):
            raise PolicyError("[exceptions] non valido")
        for k, v in ex.items():
            day = str(k)
            try:
                dt.date.fromisoformat(day)
            except ValueError:
                raise PolicyError(f"exceptions: data non valida '{day}' (formato AAAA-MM-GG)")
            if not isinstance(v, dict):
                raise PolicyError(f"exceptions.{day}: non valido")
            for kk in v:
                if kk not in ("extra", "total_soft", "total_hard", "unlimited", "apps", "note"):
                    raise PolicyError(f"exceptions.{day}: chiave sconosciuta '{kk}'")
            ext = v.get("extra", 0)
            if isinstance(ext, bool) or not isinstance(ext, (int, float)):
                raise PolicyError(f"exceptions.{day}.extra: atteso un numero di minuti")
            _num(v.get("total_soft"), f"exceptions.{day}.total_soft")
            _num(v.get("total_hard"), f"exceptions.{day}.total_hard")
            apps_ex = v.get("apps") or {}
            if not isinstance(apps_ex, dict):
                raise PolicyError(f"exceptions.{day}.apps: atteso es. {{ geometry_dash = 15 }}")
            for aid, m in apps_ex.items():
                if aid not in self.apps:
                    raise PolicyError(f"exceptions.{day}.apps: app '{aid}' non definita in [apps]")
                if isinstance(m, bool) or not isinstance(m, (int, float)):
                    raise PolicyError(f"exceptions.{day}.apps.{aid}: atteso un numero di minuti")
            self.exceptions[day] = v

    # -- interrogazioni --------------------------------------------------- #
    def app_for(self, key: str, name: str) -> str | None:
        for aid, r in self.apps.items():
            if _match(r.patterns, key, name):
                return aid
        return None

    def is_excluded(self, key: str, name: str) -> bool:
        return _match(self.exclude, key, name)

    def is_allowed(self, key: str, name: str) -> bool:
        return _match(self.always_allowed, key, name) or self.is_excluded(key, name)

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"

    def locked(self, now: dt.datetime) -> bool:
        return self.lock or (self.lock_until is not None and now < self.lock_until)

    def find_app(self, text: str) -> str | None:
        t = text.lower().strip()
        if t in self.apps:
            return t
        for aid, r in self.apps.items():
            if t == r.name.lower():
                return aid
        cands = [aid for aid, r in self.apps.items() if t in aid.lower() or t in r.name.lower()]
        return cands[0] if len(cands) == 1 else None

    def day_limits(self, date: dt.date, extras: dict | None = None):
        day = DAYS[date.weekday()]
        total = _resolve(self.total, day)
        apps = {aid: _resolve(r.limits, day) for aid, r in self.apps.items()}
        ex = self.exceptions.get(date.isoformat())
        if ex:
            if ex.get("unlimited"):
                return Limit(), {aid: Limit() for aid in apps}
            total = Limit(ex.get("total_soft", total.soft), ex.get("total_hard", total.hard))
            total = total.plus(float(ex.get("extra", 0)))
            for aid, m in (ex.get("apps") or {}).items():
                apps[aid] = apps[aid].plus(float(m))
        if extras:
            total = total.plus(float(extras.get("total", 0)))
            for aid, m in (extras.get("apps") or {}).items():
                if aid in apps:
                    apps[aid] = apps[aid].plus(float(m))
        return total, apps


def load_policy_file(path: Path) -> Policy:
    if not path.exists():
        log.warning("Policy %s non trovata: nessun limite, solo monitoraggio", path)
        return Policy({})
    return Policy(load_toml_text(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# Stato persistente
# --------------------------------------------------------------------------- #
def new_day(date: dt.date) -> dict:
    return {"version": 1, "date": date.isoformat(), "total": 0.0, "apps": {},
            "events": [], "flags": {}, "exceeded": {}, "enforced": [],
            "reported": False}


class DayStore:
    def __init__(self, days_dir: Path):
        self.dir = days_dir

    def path(self, date_iso: str) -> Path:
        return self.dir / f"{date_iso}.json"

    def load(self, date_iso: str) -> dict | None:
        p = self.path(date_iso)
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.exception("Stato %s illeggibile: riparto da zero", p)
            return None
        base = new_day(dt.date.fromisoformat(date_iso))
        base.update(st)
        return base

    def save(self, state: dict) -> None:
        atomic_write(self.path(state["date"]), json.dumps(state, ensure_ascii=False, indent=1))

    def dates(self) -> list:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("????-??-??.json"))

    def latest_before(self, date_iso: str) -> dict | None:
        older = [d for d in self.dates() if d < date_iso]
        return self.load(older[-1]) if older else None

    def cleanup(self, keep: int = 120) -> None:
        for d in self.dates()[:-keep]:
            try:
                self.path(d).unlink()
            except OSError:
                pass


class JsonFile:
    """Piccolo dizionario persistente (override remoti, metadati)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def get(self, k, default=None):
        with self.lock:
            return self.data.get(k, default)

    def set(self, k, v):
        with self.lock:
            self.data[k] = v
            atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    def update(self, fn):
        with self.lock:
            fn(self.data)
            atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))


# --------------------------------------------------------------------------- #
# Messaggi in uscita (Telegram + email), con coda persistente per l'offline
# --------------------------------------------------------------------------- #
class Outbox:
    def __init__(self, dir_: Path, cfg: Config):
        self.dir = dir_
        self.cfg = cfg
        self.wake = threading.Event()
        self._lock = threading.Lock()

    # destinatari logici
    def to_parents(self, text: str, subject: str = "DeckWatch") -> None:
        for chat in self.cfg.tg_parents:
            self.enqueue("telegram", str(chat), subject, text)
        if not self.cfg.tg_parents or not self.cfg.telegram_enabled:
            for addr in self.cfg.email_parents:
                self.enqueue("email", addr, subject, text)

    def to_kid(self, text: str, subject: str) -> None:
        for addr in self.cfg.email_kid:
            self.enqueue("email", addr, subject, text)
        for chat in self.cfg.tg_kid:
            self.enqueue("telegram", str(chat), subject, text)

    def enqueue(self, channel: str, to: str, subject: str, body: str) -> None:
        if channel == "telegram" and not self.cfg.telegram_enabled:
            log.info("[telegram non configurato] %s: %s", subject, body[:200])
            return
        if channel == "email" and not self.cfg.email_enabled:
            log.info("[email non configurata] %s: %s", subject, body[:200])
            return
        item = {"channel": channel, "to": to, "subject": subject, "body": body,
                "created": time.time()}
        name = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}.json"
        atomic_write(self.dir / name, json.dumps(item, ensure_ascii=False))
        self.wake.set()

    def flush(self) -> None:
        with self._lock:
            if not self.dir.exists():
                return
            for p in sorted(self.dir.glob("*.json")):
                try:
                    item = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    p.unlink(missing_ok=True)
                    continue
                if time.time() - item.get("created", 0) > 7 * 86400:
                    p.unlink(missing_ok=True)
                    continue
                try:
                    self.send(item)
                except Exception as e:  # rete assente ecc.: riprovo più tardi
                    log.warning("Invio %s non riuscito (riprovo): %s", item["channel"], e)
                    return
                p.unlink(missing_ok=True)

    def send(self, item: dict) -> None:
        if item["channel"] == "telegram":
            text = item["body"]
            for i in range(0, len(text), 4000):
                telegram_call(self.cfg.tg_token, "sendMessage",
                              {"chat_id": int(item["to"]), "text": text[i:i + 4000],
                               "disable_web_page_preview": True})
        else:
            send_email(self.cfg, item["to"], item["subject"], item["body"])


def telegram_call(token: str, method: str, data: dict | None = None, timeout: float = 20) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        raw = http_request(url, data or {}, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            desc = json.loads(raw).get("description", str(e))
        except Exception:
            desc = str(e)
        raise RuntimeError(f"Telegram {method}: {desc}") from None
    res = json.loads(raw)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {method}: {res.get('description')}")
    return res.get("result")


def send_email(cfg: Config, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.smtp_sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=30) as s:
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
GIORNI_BREVI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def app_breakdown(state: dict, policy: Policy):
    """Ritorna ({app_id: secondi} per le regole definite, [(secondi, nome)] per le altre app)."""
    groups: dict[str, float] = {}
    others: dict[str, float] = {}
    for key, rec in state["apps"].items():
        aid = policy.app_for(key, rec.get("name", ""))
        if aid:
            groups[aid] = groups.get(aid, 0.0) + rec["seconds"]
        else:
            name = rec.get("name") or key
            others[name] = others.get(name, 0.0) + rec["seconds"]
    return groups, sorted(((v, k) for k, v in others.items()), reverse=True)


def _has_limit(lim: Limit) -> bool:
    return lim.soft is not None or lim.hard is not None


def _over(used: float, lim: Limit) -> bool:
    return lim.hard is not None and used >= lim.hard and used > 0


def _icon(used: float, lim: Limit) -> str:
    if _over(used, lim):
        return "❌"
    if lim.soft is not None and used >= lim.soft and used > 0:
        return "🟡"
    return "✅"


def _vs(used: float, lim: Limit) -> str:
    """'1h 04m / 1h 00m (+4 min)' — tempo effettivo contro tempo concordato."""
    if lim.hard is not None:
        s = f"{fmt_min(used)} / {fmt_min(lim.hard)}"
        if _over(used, lim) and used - lim.hard >= 0.5:
            s += f" (+{fmt_min(used - lim.hard)})"
        return s
    if lim.soft is not None:
        return f"{fmt_min(used)} (avviso a {fmt_min(lim.soft)})"
    return f"{fmt_min(used)} (nessun limite)"


def _detail_rows(groups: dict, others: list, policy: Policy) -> list:
    items = [(secs, policy.apps[aid].name) for aid, secs in groups.items() if aid in policy.apps]
    items += others
    items.sort(reverse=True)
    rows, small = [], 0.0
    for secs, name in items:
        if secs < 60:
            small += secs
        else:
            rows.append(f"• {name}: {fmt_min(secs / 60)}")
    if small >= 30:
        rows.append(f"• altre app brevi: {fmt_min(small / 60)}")
    return rows or ["(nessuna attività registrata)"]


def build_report(state: dict, policy: Policy, audience: str, extras: dict | None = None,
                 as_of: str | None = None) -> tuple[str, str]:
    """Report giornaliero: riepilogo compatto in alto (effettivo / concordato), poi i dettagli."""
    date = dt.date.fromisoformat(state["date"])
    total_lim, app_lims = policy.day_limits(date, extras)
    used = state["total"] / 60
    groups, others = app_breakdown(state, policy)
    day = f"{GIORNI[date.weekday()]} {date:%d/%m}"
    when = f" (fino alle {as_of})" if as_of else ""

    if audience == "kid":
        lines = [f"Ciao! Ecco il tuo tempo sulla Deck di {day}{when}.", ""]
    else:
        lines = [f"📊 DeckWatch — {day}{when}", ""]

    lines.append("RIEPILOGO")
    overs = []
    lines.append(f"{_icon(used, total_lim)} Totale: {_vs(used, total_lim)}")
    if _over(used, total_lim):
        overs.append("tempo totale")
    for aid, r in policy.apps.items():
        lim = app_lims.get(aid, Limit())
        if not _has_limit(lim):
            continue
        u = groups.get(aid, 0.0) / 60
        lines.append(f"{_icon(u, lim)} {r.name}: {_vs(u, lim)}")
        if _over(u, lim):
            overs.append(r.name)

    lines += ["", "DETTAGLIO"]
    lines += _detail_rows(groups, others, policy)
    ex = policy.exceptions.get(state["date"])
    if ex and ex.get("note"):
        lines.append(f"Giorno speciale: {ex['note']}")
    if extras and (extras.get("total") or extras.get("apps")):
        parts = []
        if extras.get("total"):
            parts.append(f"{extras['total']:+g} min sul totale")
        for aid, m in (extras.get("apps") or {}).items():
            parts.append(f"{m:+g} min a {policy.apps[aid].name if aid in policy.apps else aid}")
        lines.append("Minuti extra concessi: " + ", ".join(parts))

    if audience == "kid":
        lines.append("")
        if overs:
            lines.append("Oggi sei andato oltre il tempo concordato per: " + ", ".join(overs) +
                         ". Domani cerchiamo di rispettarlo.")
        elif _has_limit(total_lim) or any(_has_limit(l) for l in app_lims.values()):
            lines.append("Tutto nei tempi concordati, ottimo! 👍")
        subject = f"Il tuo tempo sulla Deck di {GIORNI[date.weekday()]}: {fmt_min(used)}"
    else:
        evs = [e for e in state.get("events", [])
               if len(e) > 2 and e[2] in ("block", "kill", "sim", "lock", "extra", "cmd")]
        if evs:
            lines += ["", "EVENTI"]
            lines += [f"{e[0]} {e[1]}" for e in evs[-30:]]
        subject = f"DeckWatch {date:%d/%m}: {fmt_min(used)}" + (" ❌" if overs else "")
    return subject, "\n".join(lines)


def last_friday_before(today: dt.date) -> dt.date:
    """L'ultimo venerdì strettamente precedente a oggi (la settimana va da sabato a venerdì)."""
    delta = (today.weekday() - 4) % 7 or 7
    return today - dt.timedelta(days=delta)


def build_weekly(states: dict, policy: Policy, extras_map: dict, audience: str) -> tuple[str, str]:
    """Riepilogo settimanale. states: {data_iso: stato o None} per i 7 giorni sab→ven."""
    dates = sorted(states)
    d0, d6 = dt.date.fromisoformat(dates[0]), dt.date.fromisoformat(dates[-1])
    period = f"{d0:%d/%m} – {d6:%d/%m}"
    week_total = 0.0
    total_overs = 0
    app_week: dict[str, float] = {}
    app_over_days: dict[str, int] = {}
    other_week: dict[str, float] = {}
    day_lines = []
    any_total_limit = False
    for iso in dates:
        st = states[iso] or new_day(dt.date.fromisoformat(iso))
        date = dt.date.fromisoformat(iso)
        total_lim, app_lims = policy.day_limits(date, extras_map.get(iso))
        used = st["total"] / 60
        week_total += used
        any_total_limit |= total_lim.hard is not None
        groups, others = app_breakdown(st, policy)
        if _over(used, total_lim):
            total_overs += 1
        day_overs = []
        for aid, secs in groups.items():
            app_week[aid] = app_week.get(aid, 0.0) + secs
        for aid, lim in app_lims.items():
            if _over(groups.get(aid, 0.0) / 60, lim):
                app_over_days[aid] = app_over_days.get(aid, 0) + 1
                day_overs.append(policy.apps[aid].name)
        for secs, name in others:
            other_week[name] = other_week.get(name, 0.0) + secs
        icon = "❌" if day_overs else _icon(used, total_lim)
        line = f"{icon} {GIORNI_BREVI[date.weekday()]} {date:%d/%m}: {_vs(used, total_lim)}"
        if day_overs:
            line += " — oltre: " + ", ".join(day_overs)
        day_lines.append(line)

    if audience == "kid":
        lines = [f"Ciao! Ecco la tua settimana sulla Deck ({period}).", ""]
    else:
        lines = [f"📅 DeckWatch — settimana {period}", ""]
    lines.append("RIEPILOGO")
    lines.append(f"Totale settimana: {fmt_min(week_total)} (media {fmt_min(week_total / 7)} al giorno)")
    if any_total_limit:
        lines.append(f"Giorni oltre il tempo totale concordato: {total_overs} su 7")
    limited = [aid for aid in policy.apps
               if any(_has_limit(l) for l in [policy.day_limits(dt.date.fromisoformat(i))[1][aid]
                                              for i in dates])]
    for aid in limited:
        n = app_over_days.get(aid, 0)
        lines.append(f"• {policy.apps[aid].name}: {fmt_min(app_week.get(aid, 0) / 60)} — "
                     f"oltre il limite {n} {'giorno' if n == 1 else 'giorni'}")
    lines += ["", "GIORNO PER GIORNO"] + day_lines
    lines += ["", "DETTAGLIO APP (settimana)"]
    lines += _detail_rows(app_week, sorted(((v, k) for k, v in other_week.items()), reverse=True),
                          policy)
    n_overs = total_overs + sum(app_over_days.values())
    if audience == "kid":
        lines.append("")
        lines.append("Settimana nei tempi concordati, complimenti! 🎉" if n_overs == 0 else
                     f"Questa settimana i tempi concordati sono stati superati {n_overs} "
                     f"{'volta' if n_overs == 1 else 'volte'}: la prossima facciamo meglio!")
        subject = f"La tua settimana sulla Deck: {fmt_min(week_total)}"
    else:
        subject = f"DeckWatch settimana {period}: {fmt_min(week_total)}" + (" ❌" if n_overs else "")
    return subject, "\n".join(lines)


# --------------------------------------------------------------------------- #
# Motore: conteggio, limiti, comandi
# --------------------------------------------------------------------------- #
@dataclass
class Foreground:
    key: str
    name: str
    title: str = ""
    pid: int | None = None
    window: int | None = None
    steam_appid: str | None = None


class Engine:
    def __init__(self, platform, store: DayStore, outbox: Outbox, cfg: Config, policy: Policy,
                 overrides: JsonFile):
        self.platform = platform
        self.store = store
        self.outbox = outbox
        self.cfg = cfg
        self.policy = policy
        self.overrides = overrides
        self.state: dict = new_day(dt.date.today())
        self._pending: dict[str, dict] = {}
        self._lock_since: float | None = None
        self._last_lock_popup = 0.0
        self._last_save = 0.0
        self._map_cache: tuple = (None, {})
        self.last_fg: Foreground | None = None
        self.last_active = False
        self._now: dt.datetime | None = None

    # -- ciclo di vita ----------------------------------------------------- #
    def start(self, now: dt.datetime) -> None:
        today = now.date().isoformat()
        st = self.store.load(today)
        if st is None:
            st = new_day(now.date())
            prev = self.store.latest_before(today)
            if prev:
                self._close_day(prev, st)
        self.state = st
        self._maybe_weekly(now.date())
        self.store.cleanup()
        self.save(now.timestamp())

    def save(self, ts: float | None = None) -> None:
        self.store.save(self.state)
        self._last_save = ts if ts is not None else time.time()

    def _close_day(self, old: dict, new: dict) -> None:
        """Giornata finita (mezzanotte o prima accensione del giorno dopo): invio il report."""
        if not old.get("reported"):
            self._send_reports(old, as_of=None)
            old["reported"] = True
        self.store.save(old)

    def _maybe_weekly(self, today: dt.date) -> None:
        """Dopo il venerdì (settimana sab→ven) invio il riepilogo settimanale, una volta sola."""
        fri = last_friday_before(today)
        sent = self.overrides.get("last_weekly")
        if sent and sent >= fri.isoformat():
            return
        days = [(fri - dt.timedelta(days=6 - i)).isoformat() for i in range(7)]
        states = {iso: (self.state if iso == self.state["date"] else self.store.load(iso))
                  for iso in days}
        if any(states.values()):
            extras = {iso: self.extras_for(iso) for iso in days}
            subj, body = build_weekly(states, self.policy, extras, "parents")
            self.outbox.to_parents(body, subj)
            subj, body = build_weekly(states, self.policy, extras, "kid")
            self.outbox.to_kid(body, subj)
        self.overrides.set("last_weekly", fri.isoformat())

    def _rollover(self, now: dt.datetime) -> None:
        if now.date().isoformat() == self.state["date"]:
            return
        new = new_day(now.date())
        self._close_day(self.state, new)
        self.state = new
        self._pending.clear()
        self._maybe_weekly(now.date())
        self.save(now.timestamp())

    # -- override dai genitori (Telegram) ---------------------------------- #
    def extras_for(self, date_iso: str) -> dict:
        return (self.overrides.get("extras") or {}).get(date_iso, {})

    def remote_locked(self, now: dt.datetime) -> bool:
        if self.overrides.get("lock"):
            return True
        until = self.overrides.get("lock_until")
        return bool(until and now.timestamp() < until)

    def locked(self, now: dt.datetime) -> bool:
        return self.policy.locked(now) or self.remote_locked(now)

    # -- mappature --------------------------------------------------------- #
    def app_id_for(self, key: str, name: str) -> str | None:
        pol = self.policy
        if self._map_cache[0] is not pol:
            self._map_cache = (pol, {})
        cache = self._map_cache[1]
        if key not in cache:
            cache[key] = pol.app_for(key, name)
        return cache[key]

    def usage_by_app(self) -> dict:
        out: dict[str, float] = {}
        for key, rec in self.state["apps"].items():
            aid = self.app_id_for(key, rec.get("name", ""))
            if aid:
                out[aid] = out.get(aid, 0.0) + rec["seconds"]
        return out

    def limits_now(self, date: dt.date):
        return self.policy.day_limits(date, self.extras_for(date.isoformat()))

    # -- eventi e notifiche ------------------------------------------------ #
    def _event(self, kind: str, text: str) -> None:
        evs = self.state.setdefault("events", [])
        evs.append([(self._now or dt.datetime.now()).strftime("%H:%M"), text, kind])
        del evs[:-300]

    def _say(self, title: str, body: str, urgent: bool = False, popup: bool = False,
             kind: str = "info") -> None:
        log.info("Avviso: %s — %s", title, body)
        try:
            self.platform.notify(title, body, urgent)
            if popup:
                self.platform.popup(f"{title}\n\n{body}")
        except Exception:
            log.exception("Notifica non riuscita")
        self._event(kind, title if kind in ("block", "lock") else f"{title} — {body}")

    def _alert(self, key: str, text: str) -> None:
        if not self.policy.alerts:
            return
        sent = self.state["flags"].setdefault("alerts", [])
        if key in sent:
            return
        sent.append(key)
        self.outbox.to_parents(text, "DeckWatch: avviso")

    # -- tick principale --------------------------------------------------- #
    def tick(self, now: dt.datetime, delta: float) -> None:
        self._now = now
        self._rollover(now)
        st, pol, ts = self.state, self.policy, now.timestamp()

        fg = self.platform.foreground()
        idle = self.platform.idle_seconds()
        active = idle is None or idle < pol.idle_minutes * 60
        self.last_fg, self.last_active = fg, active
        if fg and active and delta > 0 and not pol.is_excluded(fg.key, fg.name):
            rec = st["apps"].setdefault(fg.key, {"name": fg.name or fg.key, "seconds": 0.0})
            if fg.name:
                rec["name"] = fg.name
            rec["seconds"] += delta
            st["total"] += delta

        total_lim, app_lims = self.limits_now(now.date())
        usage = self.usage_by_app()
        total_status = self._check("total", "Tempo totale", total_lim, st["total"] / 60, ts)
        app_status = {aid: self._check(f"app:{aid}", pol.apps[aid].name, lim,
                                       usage.get(aid, 0.0) / 60, ts)
                      for aid, lim in app_lims.items()}

        locked = self.locked(now)
        lock_enforce = self._update_lock(locked, ts)
        targets = self._collect_targets(fg, total_status, app_status, locked, lock_enforce)
        if locked and targets and ts - self._last_lock_popup >= 60:
            self._last_lock_popup = ts
            try:
                self.platform.popup(f"🔒 Deck bloccata\n\n{pol.lock_message}")
            except Exception:
                log.exception("popup")
        self._apply_targets(targets, ts)
        if ts - self._last_save >= 30:
            self.save(ts)

    def _check(self, lid: str, label: str, lim: Limit, used: float, ts: float) -> str:
        fl, pol = self.state["flags"], self.policy
        if lim.soft is not None and used >= lim.soft and (lim.hard is None or used < lim.hard):
            k = f"{lid}|soft|{lim.soft:g}"
            if k not in fl:
                fl[k] = ts
                tail = f" Il massimo è {fmt_min(lim.hard)}." if lim.hard is not None else ""
                self._say(f"{label}: soglia raggiunta",
                          f"Hai già usato {fmt_min(used)}.{tail}", kind="soft")
        if lim.hard is None:
            return "ok"
        if lim.hard <= 0 and used <= 0:
            return "blocked"  # app/tempo non consentito oggi: blocco al lancio, con preavviso
        remaining = lim.hard - used
        if remaining > 0:
            due = [w for w in pol.warn_before if remaining <= w]
            if due:
                w = min(due)
                if f"{lid}|warn|{w:g}|{lim.hard:g}" not in fl:
                    for x in due:
                        fl[f"{lid}|warn|{x:g}|{lim.hard:g}"] = ts
                    self._say(f"{label}: mancano {fmt_min(remaining)}",
                              f"Il tempo concordato ({fmt_min(lim.hard)}) sta per finire: "
                              "inizia a salvare.", urgent=(w == pol.warn_before[-1]), kind="warn")
            return "ok"
        ex = self.state["exceeded"]
        k = f"{lid}|{lim.hard:g}"
        if k not in ex:
            ex[k] = ts
            if pol.enforce:
                body = (f"Hai raggiunto {fmt_min(lim.hard)}. Hai {fmt_min(pol.grace_minutes)} per "
                        "salvare e chiudere, poi verrà chiuso automaticamente.")
            else:
                body = (f"Hai raggiunto il tempo concordato ({fmt_min(lim.hard)}). Per ora è solo "
                        f"un avviso: con le regole attive verrebbe chiuso tra "
                        f"{fmt_min(pol.grace_minutes)}.")
            self._say(f"{label}: tempo finito", body, urgent=True, popup=True, kind="block")
        left = pol.grace_minutes * 60 - (ts - ex[k])
        if left > 0:
            if left <= 60 and pol.enforce:
                k2 = f"{lid}|grace1|{lim.hard:g}"
                if k2 not in fl:
                    fl[k2] = ts
                    self._say(f"{label}: chiusura tra un minuto", "Salva adesso!", urgent=True,
                              kind="warn")
            return "grace"
        return "blocked"

    def _update_lock(self, locked: bool, ts: float) -> bool:
        pol = self.policy
        if locked:
            if self._lock_since is None:
                self._lock_since = ts
                self._say("Deck in pausa", f"{pol.lock_message} Hai "
                          f"{int(pol.lock_grace_seconds)} secondi per salvare.",
                          urgent=True, popup=True, kind="lock")
            return ts - self._lock_since >= pol.lock_grace_seconds
        if self._lock_since is not None:
            self._lock_since = None
            if "lock" in self.state["enforced"]:
                self.state["enforced"].remove("lock")
            self._say("Pausa terminata", "Puoi di nuovo usare la Deck.", kind="lock")
        return False

    def _collect_targets(self, fg, total_status, app_status, locked, lock_enforce) -> dict:
        pol = self.policy
        targets: dict[str, dict] = {}
        lock_on = locked and lock_enforce
        total_block = lock_on or total_status == "blocked"
        total_lid = "lock" if lock_on else "total"
        total_reason = pol.lock_message if lock_on else "Il tempo totale di oggi è finito."

        def consider(key, name, fgobj, appid):
            if key in targets:
                return
            aid = self.app_id_for(key, name)
            if aid and app_status.get(aid) == "blocked":
                targets[key] = dict(name=name, lid=f"app:{aid}", fg=fgobj, appid=appid,
                                    reason=f"Il tempo per {pol.apps[aid].name} è finito per oggi.")
            elif total_block and not pol.is_allowed(key, name):
                targets[key] = dict(name=name, lid=total_lid, fg=fgobj, appid=appid,
                                    reason=total_reason)

        if fg:
            consider(fg.key, fg.name, fg, fg.steam_appid)
        if lock_on or (pol.enforce and (total_status == "blocked" or
                                        any(s == "blocked" for s in app_status.values()))):
            for appid in self.platform.running_steam_games():
                key = f"steam:{appid}"
                consider(key, self.platform.steam_name(appid) or key, None, appid)
        return targets

    def _apply_targets(self, targets: dict, ts: float) -> None:
        pol = self.policy
        for key in list(self._pending):
            if key not in targets:
                del self._pending[key]
        enforced = self.state["enforced"]
        for key, t in targets.items():
            lid = t["lid"]
            if not pol.enforce and lid != "lock":
                k = f"sim|{lid}"
                if k not in self.state["flags"]:
                    self.state["flags"][k] = ts
                    self._event("sim", f"(simulazione) {t['name']} sarebbe stato chiuso: "
                                       f"{t['reason']}")
                continue
            immediate = lid not in enforced and (
                lid == "lock" or any(k.startswith(lid + "|") for k in self.state["exceeded"]))
            p = self._pending.get(key)
            if p is None and not immediate:
                self._pending[key] = {"t": ts}
                self._say(f"{t['name']}: non consentito ora",
                          f"{t['reason']} Verrà chiuso tra {int(pol.relaunch_seconds)} secondi.",
                          urgent=True, popup=True, kind="warn")
                continue
            if p is not None and ts - p["t"] < pol.relaunch_seconds:
                continue
            self._execute(t)
            if lid not in enforced:
                enforced.append(lid)
            self._pending[key] = {"t": ts}

    def _execute(self, t: dict) -> None:
        pol = self.policy
        if t["lid"] == "total" and pol.on_total_exceeded == "shutdown":
            self._event("kill", f"Spegnimento della Deck: tempo totale finito ({t['name']})")
            self._alert("shutdown", f"⏻ Deck spenta: tempo totale finito (era aperto {t['name']}).")
            self.save()
            self.platform.poweroff()
            return
        try:
            if t["fg"] is not None:
                self.platform.kill(t["fg"])
            elif t["appid"]:
                self.platform.kill_steam(t["appid"])
        except Exception:
            log.exception("Chiusura di %s non riuscita", t["name"])
        self._event("kill", f"Chiuso {t['name']}: {t['reason']}")
        self._alert(f"kill|{t['lid']}", f"⛔ Chiuso {t['name']} — {t['reason']}")

    # -- report ------------------------------------------------------------ #
    def _send_reports(self, st: dict, as_of: str | None) -> None:
        extras = self.extras_for(st["date"])
        subj, body = build_report(st, self.policy, "parents", extras, as_of)
        self.outbox.to_parents(body, subj)
        subj, body = build_report(st, self.policy, "kid", extras)
        self.outbox.to_kid(body, subj)

    # -- comandi Telegram -------------------------------------------------- #
    def help_text(self) -> str:
        lines = ["Comandi DeckWatch:",
                 "/stato — cosa sta usando ora e tempo di oggi",
                 "/oggi — report di oggi fin qui",
                 "/limiti — limiti di oggi",
                 "/stop — blocca la Deck (kill-switch) finché non mandi /sblocca",
                 "/stop 60 — blocca per 60 minuti",
                 "/sblocca — rimuove il blocco"]
        if self.policy.enforce:
            lines += ["/extra 30 — 30 minuti in più sul totale di oggi",
                      "/extra geometry_dash 15 — 15 minuti in più a quell'app (anche negativi)",
                      "/spegni — spegne la Deck adesso"]
        lines.append("/aiuto — questo messaggio")
        if not self.policy.enforce:
            lines += ["", "Modalità monitor: nessuna chiusura automatica, solo avvisi; nel report "
                          "trovi cosa sarebbe stato chiuso. Il blocco /stop funziona sempre."]
        return "\n".join(lines)

    def handle_command(self, chat_id: int, sender: str, text: str, msg_ts: float,
                       now: dt.datetime) -> str | None:
        parts = text.strip().split()
        if not parts:
            return None
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        age = now.timestamp() - msg_ts
        today = now.date().isoformat()
        pol = self.policy

        if cmd in ("/start", "/aiuto", "/help"):
            return self.help_text()
        if cmd in ("/extra", "/spegni") and not pol.enforce:
            return (f"{cmd} non è attivo in modalità monitor. Per attivarlo: "
                    'mode = "enforce" in policy.toml.')
        if cmd == "/stato":
            return self.status_text(now)
        if cmd == "/oggi":
            return build_report(self.state, pol, "parents", self.extras_for(today),
                                now.strftime("%H:%M"))[1]
        if cmd == "/limiti":
            return limits_text(pol, now.date(), self.extras_for(today))
        if cmd == "/stop":
            if args:
                try:
                    mins = float(args[0])
                except ValueError:
                    return "Uso: /stop oppure /stop <minuti>"
                until = msg_ts + mins * 60
                if until <= now.timestamp():
                    return f"Comando /stop {args[0]} scaduto (ricevuto in ritardo): ignorato."
                self.overrides.update(lambda d: (d.__setitem__("lock", False),
                                                 d.__setitem__("lock_until", until)))
                hhmm = dt.datetime.fromtimestamp(until).strftime("%H:%M")
                self._event("cmd", f"{sender}: blocco fino alle {hhmm}")
                self._broadcast_others(chat_id, f"🔒 {sender} ha bloccato la Deck fino alle {hhmm}.")
                return f"🔒 Deck bloccata fino alle {hhmm}. /sblocca per annullare."
            self.overrides.update(lambda d: (d.__setitem__("lock", True),
                                             d.__setitem__("lock_until", None)))
            self._event("cmd", f"{sender}: blocco attivato")
            self._broadcast_others(chat_id, f"🔒 {sender} ha bloccato la Deck.")
            return "🔒 Deck bloccata. Manda /sblocca per sbloccarla."
        if cmd == "/sblocca":
            self.overrides.update(lambda d: (d.__setitem__("lock", False),
                                             d.__setitem__("lock_until", None)))
            self._event("cmd", f"{sender}: blocco rimosso")
            self._broadcast_others(chat_id, f"🔓 {sender} ha sbloccato la Deck.")
            extra = " (Attenzione: il blocco 'lock = true' nel file policy resta attivo.)" \
                if pol.locked(now) else ""
            return "🔓 Blocco rimosso." + extra
        if cmd == "/extra":
            if not args:
                return "Uso: /extra 30  oppure  /extra <app> 15"
            try:
                mins = float(args[-1])
            except ValueError:
                return "Uso: /extra 30  oppure  /extra <app> 15"
            aid = None
            if len(args) > 1:
                aid = pol.find_app(" ".join(args[:-1]))
                if not aid:
                    return "App non trovata. App definite: " + ", ".join(
                        f"{a} ({r.name})" for a, r in pol.apps.items())
            day = dt.datetime.fromtimestamp(msg_ts).date().isoformat()
            if day != today:
                return "Comando di un giorno precedente: ignorato."

            def upd(d):
                ex = d.setdefault("extras", {})
                for k in list(ex):
                    if k < (now.date() - dt.timedelta(days=14)).isoformat():
                        del ex[k]
                e = ex.setdefault(day, {})
                if aid:
                    e.setdefault("apps", {})
                    e["apps"][aid] = e["apps"].get(aid, 0) + mins
                else:
                    e["total"] = e.get("total", 0) + mins
            self.overrides.update(upd)
            what = pol.apps[aid].name if aid else "il totale"
            self._event("extra", f"{sender}: {mins:+g} min per {what}")
            self._say("Tempo extra", f"I genitori ti hanno dato {mins:+g} minuti per {what}.",
                      kind="extra")
            self._broadcast_others(chat_id, f"⏱ {sender} ha dato {mins:+g} min per {what}.")
            return f"⏱ Fatto: {mins:+g} min per {what} oggi.\n\n" + \
                limits_text(pol, now.date(), self.extras_for(today))
        if cmd == "/spegni":
            if age > 600:
                return "Comando /spegni ricevuto in ritardo (Deck spenta o offline): ignorato."
            self._event("cmd", f"{sender}: spegnimento")
            self._broadcast_others(chat_id, f"⏻ {sender} ha spento la Deck.")
            self.save()
            self.platform.notify("Spegnimento", "I genitori stanno spegnendo la Deck.", True)
            threading.Timer(30, self.platform.poweroff).start()
            return "⏻ La Deck si spegnerà tra 30 secondi."
        return "Comando sconosciuto. /aiuto per l'elenco."

    def _broadcast_others(self, chat_id: int, text: str) -> None:
        for c in self.cfg.tg_parents:
            if c != chat_id:
                self.outbox.enqueue("telegram", str(c), "DeckWatch", text)

    def status_text(self, now: dt.datetime) -> str:
        fg = self.last_fg
        total_lim, app_lims = self.limits_now(now.date())
        lines = []
        if self.locked(now):
            lines.append("🔒 Blocco ATTIVO")
        if fg:
            lines.append(f"In primo piano: {fg.name} ({fg.key})" +
                         ("" if self.last_active else " — inattivo"))
        else:
            lines.append("In primo piano: niente")
        lim = f" su {fmt_min(total_lim.hard)}" if total_lim.hard is not None else ""
        lines.append(f"Totale oggi: {fmt_min(self.state['total'] / 60)}{lim}")
        usage = self.usage_by_app()
        for aid, l in app_lims.items():
            if l.hard is not None or usage.get(aid):
                lines.append(f"• {self.policy.apps[aid].name}: {fmt_min(usage.get(aid, 0) / 60)}"
                             + (f" su {fmt_min(l.hard)}" if l.hard is not None else ""))
        return "\n".join(lines)


def limits_text(pol: Policy, date: dt.date, extras: dict | None = None) -> str:
    total, apps = pol.day_limits(date, extras)

    def lt(l: Limit):
        if l.hard is None and l.soft is None:
            return "nessun limite"
        s = []
        if l.soft is not None:
            s.append(f"avviso a {fmt_min(l.soft)}")
        if l.hard is not None:
            s.append(f"massimo {fmt_min(l.hard)}")
        return ", ".join(s)
    lines = [f"Limiti di {GIORNI[date.weekday()]} {date:%d/%m}:", f"Totale: {lt(total)}"]
    for aid, l in apps.items():
        lines.append(f"• {pol.apps[aid].name}: {lt(l)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Piattaforma Linux / SteamOS
# --------------------------------------------------------------------------- #
_XErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_XIOErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)


class _XScreenSaverInfo(ctypes.Structure):
    _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong),
                ("eventMask", ctypes.c_ulong)]


_X_IO_CALLBACK = None


def _x_io_error(_d):
    # Il server X è sparito (cambio modalità Desktop/Gioco, logout): salvo ed esco,
    # systemd mi riavvia.
    try:
        if _X_IO_CALLBACK:
            _X_IO_CALLBACK()
    finally:
        os._exit(3)
    return 0


_X_ERR_HANDLER = _XErrorHandler(lambda d, e: 0)
_X_IO_HANDLER = _XIOErrorHandler(_x_io_error)


@dataclass
class WindowInfo:
    cls: str = ""
    title: str = ""
    pid: int | None = None
    steam_game: int | None = None


class X11:
    """Accesso minimale a Xlib via ctypes (finestra attiva, proprietà, inattività)."""

    def __init__(self, on_io_error=None):
        L = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        self.L = L
        c_ul, c_vp = ctypes.c_ulong, ctypes.c_void_p
        L.XOpenDisplay.restype = c_vp
        L.XOpenDisplay.argtypes = [ctypes.c_char_p]
        L.XCloseDisplay.argtypes = [c_vp]
        L.XDefaultRootWindow.restype = c_ul
        L.XDefaultRootWindow.argtypes = [c_vp]
        L.XInternAtom.restype = c_ul
        L.XInternAtom.argtypes = [c_vp, ctypes.c_char_p, ctypes.c_int]
        L.XGetWindowProperty.restype = ctypes.c_int
        L.XGetWindowProperty.argtypes = [
            c_vp, c_ul, c_ul, ctypes.c_long, ctypes.c_long, ctypes.c_int, c_ul,
            ctypes.POINTER(c_ul), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(c_ul),
            ctypes.POINTER(c_ul), ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        L.XFree.argtypes = [c_vp]
        L.XKillClient.argtypes = [c_vp, c_ul]
        L.XFlush.argtypes = [c_vp]
        L.XSetErrorHandler.argtypes = [_XErrorHandler]
        L.XSetErrorHandler.restype = c_vp
        L.XSetIOErrorHandler.argtypes = [_XIOErrorHandler]
        L.XSetIOErrorHandler.restype = c_vp
        global _X_IO_CALLBACK
        _X_IO_CALLBACK = on_io_error
        # I callback sono globali: Xlib li tiene per tutto il processo e non devono
        # essere liberati dal garbage collector.
        L.XSetErrorHandler(_X_ERR_HANDLER)
        L.XSetIOErrorHandler(_X_IO_HANDLER)

        self.xss = None
        try:
            self.xss = ctypes.CDLL(ctypes.util.find_library("Xss") or "libXss.so.1")
            self.xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XScreenSaverInfo)
            self.xss.XScreenSaverQueryInfo.argtypes = [c_vp, c_ul, ctypes.POINTER(_XScreenSaverInfo)]
            self._xss_info = self.xss.XScreenSaverAllocInfo()
        except OSError:
            log.warning("libXss non disponibile: inattività rilevata solo dal gamepad")
            self.xss = None
        self.dpy = None
        self.display = None
        self.root = 0
        self._atoms: dict[str, int] = {}

    def open(self, display: str | None, xauth: str | None = None) -> bool:
        if self.dpy and display == self.display:
            return True
        self.close()
        if not display:
            return False
        os.environ["DISPLAY"] = display
        if xauth:
            os.environ["XAUTHORITY"] = xauth
        d = self.L.XOpenDisplay(display.encode())
        if not d:
            return False
        self.dpy, self.display = d, display
        self.root = self.L.XDefaultRootWindow(d)
        self._atoms = {}
        log.info("Connesso al display X %s", display)
        return True

    def close(self) -> None:
        if self.dpy:
            try:
                self.L.XCloseDisplay(self.dpy)
            except Exception:
                pass
        self.dpy = None
        self.display = None

    def atom(self, name: str) -> int:
        a = self._atoms.get(name)
        if a:
            return a
        a = self.L.XInternAtom(self.dpy, name.encode(), 1)
        if a:
            self._atoms[name] = a
        return a

    def get_prop(self, win: int, name: str):
        if not self.dpy or not win:
            return None
        a = self.atom(name)
        if not a:
            return None
        at, af = ctypes.c_ulong(), ctypes.c_int()
        n, after = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        st = self.L.XGetWindowProperty(self.dpy, win, a, 0, 4096, 0, 0, ctypes.byref(at),
                                       ctypes.byref(af), ctypes.byref(n), ctypes.byref(after),
                                       ctypes.byref(data))
        if st != 0 or not data:
            return None
        try:
            if af.value == 32:
                raw = ctypes.string_at(data, n.value * ctypes.sizeof(ctypes.c_ulong))
                return list(struct.unpack(f"@{n.value}L", raw))
            if af.value == 16:
                raw = ctypes.string_at(data, n.value * 2)
                return list(struct.unpack(f"@{n.value}H", raw))
            return ctypes.string_at(data, n.value)
        finally:
            self.L.XFree(data)

    def cardinal(self, win: int, name: str) -> int | None:
        v = self.get_prop(win, name)
        return v[0] if isinstance(v, list) and v else None

    def active_window(self) -> int:
        return self.cardinal(self.root, "_NET_ACTIVE_WINDOW") or 0

    def window_info(self, win: int) -> WindowInfo:
        wi = WindowInfo()
        c = self.get_prop(win, "WM_CLASS")
        if isinstance(c, bytes):
            parts = [p.decode(errors="replace") for p in c.split(b"\0") if p]
            if parts:
                wi.cls = parts[-1] if len(parts) > 1 else parts[0]
        t = self.get_prop(win, "_NET_WM_NAME")
        if isinstance(t, bytes):
            wi.title = t.decode(errors="replace")
        else:
            t = self.get_prop(win, "WM_NAME")
            if isinstance(t, bytes):
                wi.title = t.decode("latin-1", errors="replace")
        wi.pid = self.cardinal(win, "_NET_WM_PID")
        wi.steam_game = self.cardinal(win, "STEAM_GAME")
        return wi

    def idle_seconds(self) -> float | None:
        if not self.xss or not self.dpy:
            return None
        if self.xss.XScreenSaverQueryInfo(self.dpy, self.root, self._xss_info):
            return self._xss_info.contents.idle / 1000.0
        return None

    def kill_client(self, win: int) -> None:
        if self.dpy and win:
            self.L.XKillClient(self.dpy, win)
            self.L.XFlush(self.dpy)


class SessionEnv:
    """Ricava DISPLAY/XAUTHORITY/DBUS dalla sessione grafica attiva dell'utente."""
    ORDER = ["plasmashell", "kwin_x11", "kwin_wayland", "steam", "gamescope", "gamescope-wl"]
    KEYS = ["DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
            "XDG_RUNTIME_DIR"]

    def __init__(self):
        self.env: dict[str, str] = {}
        self.mode = "unknown"

    def refresh(self) -> None:
        found: dict[str, dict] = {}
        for pid in own_pids():
            comm = proc_comm(pid)
            if comm in self.ORDER and comm not in found:
                e = read_environ(pid)
                if e.get("DISPLAY") or e.get("WAYLAND_DISPLAY"):
                    found[comm] = e
        env = {}
        for comm in self.ORDER:
            if comm in found:
                env = found[comm]
                break
        self.mode = "desktop" if ("plasmashell" in found or "kwin_x11" in found or
                                  "kwin_wayland" in found) else \
            "game" if ("gamescope" in found or "gamescope-wl" in found) else "unknown"
        new = {k: env[k] for k in self.KEYS if env.get(k)}
        rd = f"/run/user/{os.getuid()}"
        new.setdefault("XDG_RUNTIME_DIR", rd)
        if os.path.exists(f"{rd}/bus"):
            new.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={rd}/bus")
        self.env = new

    def subprocess_env(self) -> dict:
        e = dict(os.environ)
        e.update(self.env)
        return e


EV_KEY, EV_ABS = 1, 3
_EV_FMT = "llHHi"
_EV_SIZE = struct.calcsize(_EV_FMT)


class InputActivity:
    """Rileva l'uso dei controlli leggendo i dispositivi /dev/input accessibili (gamepad)."""
    SKIP = re.compile(r"motion|sensor|accel|gyro|\blid\b|video bus|power button|sleep button|"
                      r"hdmi|headphone|jack|hda |acpi", re.I)

    def __init__(self):
        self.fds: dict[str, int] = {}
        self.last: float | None = None

    def rescan(self) -> None:
        for path in list(self.fds):
            if not os.path.exists(path):
                self._close(path)
        for path in glob.glob("/dev/input/event*"):
            if path in self.fds:
                continue
            base = os.path.basename(path)
            try:
                name = Path(f"/sys/class/input/{base}/device/name").read_text().strip()
            except OSError:
                name = ""
            if self.SKIP.search(name):
                continue
            try:
                self.fds[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue

    def _close(self, path: str) -> None:
        fd = self.fds.pop(path, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def poll(self) -> None:
        if not self.fds:
            return
        try:
            ready, _, _ = select.select(list(self.fds.values()), [], [], 0)
        except (OSError, ValueError):
            for p in list(self.fds):
                self._close(p)
            return
        by_fd = {fd: p for p, fd in self.fds.items()}
        for fd in ready:
            act = False
            while True:
                try:
                    data = os.read(fd, _EV_SIZE * 128)
                except BlockingIOError:
                    break
                except OSError:
                    self._close(by_fd[fd])
                    break
                if not data:
                    break
                for off in range(0, len(data) - _EV_SIZE + 1, _EV_SIZE):
                    _, _, typ, code, val = struct.unpack_from(_EV_FMT, data, off)
                    if typ == EV_KEY and val == 1:
                        act = True
                    elif typ == EV_ABS:
                        if code in (0, 1, 3, 4) and abs(val) > 12000:
                            act = True
                        elif 16 <= code <= 23 and val != 0:  # croce direzionale / trackpad
                            act = True
            if act:
                self.last = time.monotonic()

    def idle_seconds(self) -> float | None:
        return None if self.last is None else time.monotonic() - self.last


class SteamInfo:
    def __init__(self):
        home = Path.home()
        self.roots = [home / ".local/share/Steam", home / ".steam/steam",
                      home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
        self.names: dict[str, str] = {}
        self._scanned = 0.0

    def scan(self) -> None:
        libs = set()
        for r in self.roots:
            sa = r / "steamapps"
            if not sa.is_dir():
                continue
            libs.add(sa.resolve())
            try:
                vdf = (sa / "libraryfolders.vdf").read_text(errors="replace")
                for p in re.findall(r'"path"\s+"([^"]+)"', vdf):
                    libs.add((Path(p.replace("\\\\", "/")) / "steamapps").resolve())
            except OSError:
                pass
        names = {}
        for lib in libs:
            for f in lib.glob("appmanifest_*.acf"):
                try:
                    t = f.read_text(errors="replace")
                except OSError:
                    continue
                a = re.search(r'"appid"\s+"(\d+)"', t)
                n = re.search(r'"name"\s+"([^"]*)"', t)
                if a and n:
                    names[a.group(1)] = n.group(1)
        self.names = names
        self._scanned = time.monotonic()

    def name(self, appid: str) -> str | None:
        age = time.monotonic() - self._scanned
        if not self._scanned or age > 600 or (appid not in self.names and age > 60):
            self.scan()
        return self.names.get(appid)


class Notifier:
    SOUNDS = ["/usr/share/sounds/ocean/stereo/dialog-warning.oga",
              "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
              "/usr/share/sounds/freedesktop/stereo/bell.oga"]

    def __init__(self, session: SessionEnv):
        self.session = session
        self._procs: list = []
        self._popup = None

    def _spawn(self, args):
        self._procs = [p for p in self._procs if p.poll() is None]
        try:
            p = subprocess.Popen(args, env=self.session.subprocess_env(),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            self._procs.append(p)
            return p
        except OSError as e:
            log.warning("Impossibile eseguire %s: %s", args[0], e)
            return None

    def notify(self, title: str, body: str, urgent: bool = False) -> None:
        if shutil.which("notify-send"):
            self._spawn(["notify-send", "-a", "DeckWatch", "-u", "critical" if urgent else "normal",
                         "-i", "dialog-warning" if urgent else "dialog-information", title, body])
        elif shutil.which("gdbus"):
            self._spawn(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                         "--object-path", "/org/freedesktop/Notifications", "--method",
                         "org.freedesktop.Notifications.Notify", "DeckWatch", "0",
                         "dialog-warning", title, body, "[]",
                         "{'urgency': <byte %d>}" % (2 if urgent else 1), "8000"])
        if urgent:
            self.sound()

    def popup(self, text: str) -> None:
        if self._popup is not None and self._popup.poll() is None:
            return
        if shutil.which("kdialog"):
            self._popup = self._spawn(["kdialog", "--title", "DeckWatch", "--sorry", text])
        elif shutil.which("zenity"):
            self._popup = self._spawn(["zenity", "--warning", "--title", "DeckWatch", "--text", text])

    def sound(self) -> None:
        player = shutil.which("paplay") or shutil.which("pw-play")
        if not player:
            return
        for s in self.SOUNDS:
            if os.path.exists(s):
                self._spawn([player, s])
                return


class LinuxPlatform:
    def __init__(self, on_x_io_error=None):
        self.session = SessionEnv()
        self.notifier = Notifier(self.session)
        self.input = InputActivity()
        self.steam = SteamInfo()
        try:
            self.x: X11 | None = X11(on_io_error=on_x_io_error)
        except OSError:
            log.warning("libX11 non disponibile: rilevo solo i giochi Steam in esecuzione")
            self.x = None
        self._last_env = 0.0
        self._last_scan = 0.0
        self._killing: set = set()

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_env > 30 or (self.x and not self.x.dpy):
            self._last_env = now
            self.session.refresh()
            if self.x:
                self.x.open(self.session.env.get("DISPLAY"), self.session.env.get("XAUTHORITY"))
        if force or now - self._last_scan > 60:
            self._last_scan = now
            self.input.rescan()
        self.input.poll()

    # -- interfaccia usata dal motore -------------------------------------- #
    def steam_name(self, appid: str) -> str | None:
        return self.steam.name(appid)

    def _steam_fg(self, appid: str, title: str = "", pid=None, win=None) -> Foreground:
        name = self.steam.name(appid) or title or f"Gioco Steam {appid}"
        return Foreground(f"steam:{appid}", name, title, pid, win, appid)

    def foreground(self) -> Foreground | None:
        x = self.x
        if x and x.dpy:
            focused = x.cardinal(x.root, "GAMESCOPE_FOCUSED_APP")
            if focused is not None:  # Modalità Gioco (gamescope)
                if str(focused) in NON_GAME_APPIDS:
                    return Foreground("steam:ui", "Steam (interfaccia)")
                return self._steam_fg(str(focused))
            win = x.active_window()
            if not win:
                return None
            wi = x.window_info(win)
            appid = None
            if wi.steam_game and str(wi.steam_game) not in NON_GAME_APPIDS:
                appid = str(wi.steam_game)
            elif wi.pid:
                appid = steam_appid_from_env(read_environ(wi.pid))
            if appid:
                return self._steam_fg(appid, wi.title, wi.pid, win)
            cls = (wi.cls or (proc_comm(wi.pid) if wi.pid else "") or "sconosciuta")
            return Foreground(f"class:{cls.lower()}", wi.cls or cls, wi.title, wi.pid, win)
        games = self.running_steam_games()
        if games:
            appid = max(games, key=lambda a: max(games[a]))
            return self._steam_fg(appid)
        return None

    def idle_seconds(self) -> float | None:
        vals = [v for v in (self.x.idle_seconds() if self.x else None,
                            self.input.idle_seconds()) if v is not None]
        return min(vals) if vals else None

    def notify(self, title: str, body: str, urgent: bool = False) -> None:
        self.notifier.notify(title, body, urgent)

    def popup(self, text: str) -> None:
        self.notifier.popup(text)

    def running_steam_games(self) -> dict:
        games: dict[str, list] = {}
        me = os.getpid()
        for pid in own_pids():
            if pid == me or proc_comm(pid) in STEAM_CLIENT_COMMS:
                continue
            appid = steam_appid_from_env(read_environ(pid))
            if appid:
                games.setdefault(appid, []).append(pid)
        return games

    def _kill_pids(self, pids: list, label: str) -> None:
        if label in self._killing:
            return
        self._killing.add(label)

        def work():
            try:
                for p in pids:
                    try:
                        os.kill(p, signal.SIGTERM)
                    except OSError:
                        pass
                time.sleep(6)
                for p in pids:
                    try:
                        os.kill(p, signal.SIGKILL)
                    except OSError:
                        pass
            finally:
                self._killing.discard(label)
        threading.Thread(target=work, daemon=True).start()

    def kill_steam(self, appid: str) -> None:
        pids = self.running_steam_games().get(appid, [])
        log.info("Chiudo gioco Steam %s (pid %s)", appid, pids)
        if pids:
            self._kill_pids(pids, f"steam:{appid}")

    def kill(self, fg: Foreground) -> None:
        log.info("Chiudo %s (%s)", fg.name, fg.key)
        if fg.steam_appid:
            pids = self.running_steam_games().get(fg.steam_appid, [])
            if pids:
                self._kill_pids(pids, fg.key)
                return
        if fg.window and self.x:
            self.x.kill_client(fg.window)
        if fg.pid:
            # Solo se il processo sembra davvero quello della finestra (i flatpak usano pid
            # "interni" che non corrispondono a quelli dell'host).
            comm = proc_comm(fg.pid).lower()
            cls = fg.name.lower()
            if comm and (comm[:6] in cls or cls[:6] in comm):
                self._kill_pids([fg.pid], fg.key)

    def poweroff(self) -> None:
        log.warning("Spegnimento della Deck")
        subprocess.run(["systemctl", "poweroff"], check=False)


# --------------------------------------------------------------------------- #
# Demone
# --------------------------------------------------------------------------- #
INSTALL_DIR = Path.home() / ".local" / "share" / APP


class Daemon:
    def __init__(self, paths: Paths, cfg: Config):
        self.paths, self.cfg = paths, cfg
        self.stop = threading.Event()
        self.commands: queue.Queue = queue.Queue()
        self.meta = JsonFile(paths.meta_file)
        self.platform = LinuxPlatform(on_x_io_error=self._emergency_save)
        self.outbox = Outbox(paths.outbox_dir, cfg)
        try:
            policy = load_policy_file(paths.policy_file)
        except Exception as e:
            log.error("Policy non valida (%s): nessun limite finché non viene corretta", e)
            self.outbox.to_parents(f"⚠️ policy.toml non valido: {e}")
            policy = Policy({})
        self._policy_mtime = self._mtime()
        self.engine = Engine(self.platform, DayStore(paths.days_dir), self.outbox, cfg, policy,
                             JsonFile(paths.overrides_file))

    def _mtime(self) -> float:
        try:
            return self.paths.policy_file.stat().st_mtime
        except OSError:
            return 0.0

    def _emergency_save(self) -> None:
        try:
            self.engine.save()
        except Exception:
            pass

    def _reload_policy(self) -> None:
        m = self._mtime()
        if m == self._policy_mtime:
            return
        self._policy_mtime = m
        try:
            self.engine.policy = load_policy_file(self.paths.policy_file)
            log.info("Policy ricaricata")
            self.outbox.to_parents("✅ policy.toml aggiornato e caricato.")
        except Exception as e:
            log.error("Policy non valida: %s", e)
            self.outbox.to_parents(f"⚠️ policy.toml non valido, continuo con la versione "
                                   f"precedente.\nErrore: {e}")

    # -- thread ------------------------------------------------------------ #
    def _outbox_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.outbox.flush()
            except Exception:
                log.exception("outbox")
            self.outbox.wake.wait(60)
            self.outbox.wake.clear()

    def _telegram_loop(self) -> None:
        if not self.cfg.telegram_enabled:
            return
        offset = self.meta.get("tg_offset")
        while not self.stop.is_set():
            try:
                data = {"timeout": 25, "allowed_updates": ["message"]}
                if offset is not None:
                    data["offset"] = offset
                updates = telegram_call(self.cfg.tg_token, "getUpdates", data, timeout=40)
            except Exception as e:
                log.warning("Telegram non raggiungibile: %s", e)
                self.stop.wait(30)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                text = msg.get("text") or ""
                chat = (msg.get("chat") or {}).get("id")
                if not chat or not text.startswith("/"):
                    continue
                frm = msg.get("from") or {}
                sender = frm.get("first_name") or frm.get("username") or str(chat)
                if chat not in self.cfg.tg_parents:
                    log.warning("Comando da chat non autorizzata %s (%s)", chat, sender)
                    self.outbox.enqueue("telegram", str(chat), "DeckWatch",
                                        f"Non sei autorizzato. Il tuo chat id è {chat}: "
                                        "aggiungilo a [telegram] parents in config.toml.")
                    continue
                self.commands.put((chat, sender, text, float(msg.get("date", time.time()))))
            if updates:
                self.meta.set("tg_offset", offset)

    def _update_loop(self) -> None:
        if not (self.cfg.auto_update and self.cfg.update_url):
            return
        if self.stop.wait(300):
            return
        while not self.stop.is_set():
            last = self.meta.get("last_update_check", 0)
            if time.time() - last >= 86400:
                self.meta.set("last_update_check", time.time())
                try:
                    v = self_update(self.cfg)
                    if v:
                        self.outbox.to_parents(f"🔄 DeckWatch aggiornato alla versione {v}.")
                        log.info("Aggiornato a %s: riavvio", v)
                        self.stop.set()
                        return
                except Exception as e:
                    log.warning("Controllo aggiornamenti fallito: %s", e)
            self.stop.wait(3600)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        log.info("DeckWatch %s avviato", __version__)
        self.platform.refresh(force=True)
        self.engine.start(dt.datetime.now())
        for fn in (self._outbox_loop, self._telegram_loop, self._update_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
        last = time.monotonic()
        last_reload = 0.0
        while not self.stop.is_set():
            now_m = time.monotonic()
            delta = now_m - last
            last = now_m
            if delta > TICK * 3:
                delta = TICK
            try:
                if now_m - last_reload > 15:
                    last_reload = now_m
                    self._reload_policy()
                self.platform.refresh()
                while not self.commands.empty():
                    chat, sender, text, ts = self.commands.get_nowait()
                    reply = self.engine.handle_command(chat, sender, text, ts, dt.datetime.now())
                    if reply:
                        self.outbox.enqueue("telegram", str(chat), "DeckWatch", reply)
                self.engine.tick(dt.datetime.now(), delta)
            except Exception:
                log.exception("Errore nel ciclo principale")
            # attesa reattiva sugli input (per non perdere l'attività del gamepad)
            end = time.monotonic() + TICK
            while not self.stop.is_set() and time.monotonic() < end:
                self.stop.wait(1.0)
                self.platform.input.poll()
        self.engine.save()
        log.info("DeckWatch fermato")


def self_update(cfg: Config) -> str | None:
    target = Path(__file__).resolve()
    if target.parent != INSTALL_DIR.resolve():
        log.info("Aggiornamento automatico solo per l'installazione in %s", INSTALL_DIR)
        return None
    base = cfg.update_url.rstrip("/")
    remote = http_request(f"{base}/VERSION").decode().strip()
    if parse_version(remote) <= parse_version(__version__):
        return None
    code = http_request(f"{base}/deckwatch.py").decode("utf-8")
    compile(code, "deckwatch.py", "exec")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', code, re.M)
    if not m or m.group(1) != remote:
        raise RuntimeError("versione nel file scaricato non coerente")
    atomic_write(target, code, 0o755)
    return remote


# --------------------------------------------------------------------------- #
# Riga di comando
# --------------------------------------------------------------------------- #
def cmd_run(paths: Paths, args) -> int:
    Daemon(paths, Config.load(paths.config_file)).run()
    return 0


def cmd_status(paths: Paths, args) -> int:
    pol = load_policy_file(paths.policy_file)
    st = DayStore(paths.days_dir).load(dt.date.today().isoformat()) or new_day(dt.date.today())
    ov = JsonFile(paths.overrides_file)
    extras = (ov.get("extras") or {}).get(st["date"], {})
    print(build_report(st, pol, "parents", extras, dt.datetime.now().strftime("%H:%M"))[1])
    if ov.get("lock") or (ov.get("lock_until") or 0) > time.time() or pol.locked(dt.datetime.now()):
        print("\n🔒 Blocco attivo")
    return 0


def cmd_check(paths: Paths, args) -> int:
    path = Path(args.file) if args.file else paths.policy_file
    try:
        pol = load_policy_file(path)
    except Exception as e:
        print(f"❌ {path}: {e}")
        return 1
    print(f"✅ {path} valido.\n")
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    for i in range(7):
        print(limits_text(pol, monday + dt.timedelta(days=i)))
        print()
    if pol.exceptions:
        print("Eccezioni:", ", ".join(sorted(pol.exceptions)))
    if pol.locked(dt.datetime.now()):
        print("🔒 Il file contiene un blocco attivo (lock / lock_until).")
    return 0


def cmd_fg(paths: Paths, args) -> int:
    p = LinuxPlatform()
    p.refresh(force=True)
    print(f"Modalità: {p.session.mode}  DISPLAY={p.session.env.get('DISPLAY')}  "
          f"input leggibili: {len(p.input.fds)}")
    try:
        while True:
            fg = p.foreground()
            idle = p.idle_seconds()
            idle_s = "?" if idle is None else f"{idle:.0f}s"
            if fg:
                print(f"{fg.key:<30} {fg.name!r:<30} inattivo da {idle_s}  titolo={fg.title!r}")
            else:
                print(f"(nessuna finestra attiva)  inattivo da {idle_s}")
            if not args.watch:
                break
            for _ in range(3):
                time.sleep(1)
                p.refresh()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_games(paths: Paths, args) -> int:
    s = SteamInfo()
    s.scan()
    for appid, name in sorted(s.names.items(), key=lambda x: x[1].lower()):
        print(f"steam:{appid:<12} {name}")
    if not s.names:
        print("Nessun gioco Steam trovato.")
    return 0


def cmd_report(paths: Paths, args) -> int:
    cfg = Config.load(paths.config_file)
    pol = load_policy_file(paths.policy_file)
    date = args.date or dt.date.today().isoformat()
    st = DayStore(paths.days_dir).load(date)
    if not st:
        print(f"Nessun dato per {date}")
        return 1
    extras = (JsonFile(paths.overrides_file).get("extras") or {}).get(date, {})
    for aud in ("parents", "kid"):
        subj, body = build_report(st, pol, aud, extras)
        print(f"=== {aud}: {subj}\n{body}\n")
    if args.send:
        ob = Outbox(paths.outbox_dir, cfg)
        ob.to_parents(*reversed(build_report(st, pol, "parents", extras)))
        ob.to_kid(*reversed(build_report(st, pol, "kid", extras)))
        ob.flush()
        print("Inviato (o in coda se offline).")
    return 0


def cmd_test(paths: Paths, args) -> int:
    cfg = Config.load(paths.config_file)
    ok = True
    if cfg.telegram_enabled:
        for c in cfg.tg_parents + cfg.tg_kid:
            try:
                telegram_call(cfg.tg_token, "sendMessage",
                              {"chat_id": c, "text": "✅ Test DeckWatch: Telegram funziona."})
                print(f"Telegram → {c}: ok")
            except Exception as e:
                ok = False
                print(f"Telegram → {c}: ERRORE {e}")
        if not cfg.tg_parents:
            print("Nessun genitore in [telegram] parents: usa 'deckwatch telegram-setup'.")
    else:
        print("Telegram non configurato.")
    if cfg.email_enabled:
        for a in cfg.email_kid + cfg.email_parents:
            try:
                send_email(cfg, a, "Test DeckWatch", "Se leggi questo messaggio l'email funziona.")
                print(f"Email → {a}: ok")
            except Exception as e:
                ok = False
                print(f"Email → {a}: ERRORE {e}")
    else:
        print("Email non configurata.")
    return 0 if ok else 1


def cmd_telegram_setup(paths: Paths, args) -> int:
    cfg = Config.load(paths.config_file)
    if not cfg.tg_token:
        print("Prima inserisci il token del bot in config.toml ([telegram] token = \"...\").")
        return 1
    try:
        me = telegram_call(cfg.tg_token, "getMe")
        updates = telegram_call(cfg.tg_token, "getUpdates", {"timeout": 0})
    except Exception as e:
        print(f"Errore Telegram: {e}")
        return 1
    print(f"Bot: @{me.get('username')}")
    chats = {}
    for u in updates:
        m = u.get("message") or {}
        c = m.get("chat") or {}
        if c.get("id"):
            chats[c["id"]] = c.get("first_name") or c.get("title") or c.get("username") or "?"
    if not chats:
        print("Nessun messaggio ricevuto. Ogni genitore deve aprire il bot su Telegram, "
              "premere Avvia (/start), poi rilancia questo comando.\n"
              "Nota: se il servizio DeckWatch è già in esecuzione legge lui i messaggi; "
              "fermalo con: systemctl --user stop deckwatch")
        return 1
    print("Chat trovate:")
    for cid, name in chats.items():
        print(f"  {cid}  {name}")
    if args.save:
        text = paths.config_file.read_text(encoding="utf-8")
        ids = sorted(set(cfg.tg_parents) | set(chats))
        new_line = "parents = [" + ", ".join(str(i) for i in ids) + "]"
        pat = re.compile(r"(\[telegram\][^\[]*?)^parents\s*=\s*\[[^\]]*\]", re.M | re.S)
        if pat.search(text):
            text = pat.sub(lambda m: m.group(1) + new_line, text, count=1)
        else:
            text = re.sub(r"^\[telegram\]\s*$", "[telegram]\n" + new_line, text, count=1, flags=re.M)
        atomic_write(paths.config_file, text, 0o600)
        print(f"Salvato in config.toml: {new_line}")
        print("Riavvia il servizio: systemctl --user restart deckwatch")
    else:
        print("Per salvarle tutte come genitori: deckwatch telegram-setup --save")
    return 0


def cmd_update(paths: Paths, args) -> int:
    cfg = Config.load(paths.config_file)
    if not cfg.update_url:
        print("update_url non impostato in config.toml")
        return 1
    v = self_update(cfg)
    if v:
        print(f"Aggiornato alla versione {v}. Riavvio servizio...")
        subprocess.run(["systemctl", "--user", "restart", APP], check=False)
    else:
        print(f"Nessun aggiornamento (versione attuale {__version__}).")
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog=APP, description="Monitor del tempo di gioco per Steam Deck")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="avvia il servizio (usato da systemd)")
    sub.add_parser("status", help="mostra il tempo di oggi")
    p = sub.add_parser("check", help="verifica policy.toml e mostra i limiti della settimana")
    p.add_argument("file", nargs="?")
    p = sub.add_parser("fg", help="mostra l'app in primo piano (per scrivere le regole 'match')")
    p.add_argument("--watch", action="store_true")
    sub.add_parser("games", help="elenca i giochi Steam installati con il loro appid")
    p = sub.add_parser("report", help="mostra (e opzionalmente invia) il report di un giorno")
    p.add_argument("--date")
    p.add_argument("--send", action="store_true")
    sub.add_parser("test", help="invia messaggi di prova (Telegram ed email)")
    p = sub.add_parser("telegram-setup", help="trova i chat id dei genitori")
    p.add_argument("--save", action="store_true")
    sub.add_parser("update", help="aggiorna DeckWatch ora")
    args = ap.parse_args(argv)
    paths = Paths.default()
    fn = {"run": cmd_run, "status": cmd_status, "check": cmd_check, "fg": cmd_fg,
          "games": cmd_games, "report": cmd_report, "test": cmd_test,
          "telegram-setup": cmd_telegram_setup, "update": cmd_update}[args.cmd]
    return fn(paths, args)


if __name__ == "__main__":
    sys.exit(main())
