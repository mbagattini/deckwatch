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
import email.utils
import glob
import hashlib
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

__version__ = "0.4.1"

APP = "deckwatch"
TICK = 10.0
log = logging.getLogger(APP)

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
        _note_server_time(r.headers.get("Date"))
        return r.read(4 * 1024 * 1024)


# Ora dei server (dall'intestazione HTTP "Date"), per accorgersi di un orologio spostato.
HTTP_CLOCK: dict = {}


def _note_server_time(value) -> None:
    try:
        srv = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return
    HTTP_CLOCK.update(server=srv, local=time.time())


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

    @property
    def limits_file(self) -> Path:
        return self.config_dir / "limiti.toml"

    @property
    def heartbeat_file(self) -> Path:
        return self.state_dir / "heartbeat.json"

    @property
    def update_file(self) -> Path:
        return self.state_dir / "update.json"


@dataclass
class Config:
    update_url: str = ""
    auto_update: bool = True
    tg_token: str = ""
    tg_parents: list = field(default_factory=list)
    tg_kid: list = field(default_factory=list)
    tg_description: str = "Monitor del tempo sulla Deck"
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
        c.tg_description = str(tg.get("description", c.tg_description)).strip() or c.tg_description
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
# Regole: policy.toml (impostazioni tecniche, scritto solo dai genitori a mano)
#         limiti.toml (limiti, nomi delle app, crescita: siti e app; scritto dal bot)
# --------------------------------------------------------------------------- #
class PolicyError(ValueError):
    pass


DAY_TYPES = ("feriale", "festivo")

DEFAULT_GROWTH_SITES = [
    "nuvola.madisoft.it", "*.wikipedia.org", "classroom.google.com", "docs.google.com",
    "drive.google.com", "meet.google.com", "accounts.google.com",
]


@dataclass(frozen=True)
class Limit:
    soft: float | None = None   # tempo concordato (minuti)
    hard: float | None = None   # limite massimo (minuti)

    def plus(self, m: float) -> "Limit":
        return Limit(None if self.soft is None else max(0.0, self.soft + m),
                     None if self.hard is None else max(0.0, self.hard + m))

    @property
    def empty(self) -> bool:
        return self.soft is None and self.hard is None


@dataclass
class AppRule:
    id: str
    name: str
    patterns: list
    limits: dict = field(default_factory=dict)   # {"feriale": Limit, "festivo": Limit}


def _num(v, where: str):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PolicyError(f"{where}: atteso un numero (minuti), trovato {v!r}")
    if v < 0:
        raise PolicyError(f"{where}: il valore non può essere negativo")
    return float(v)


def _num_list(v, where: str) -> list:
    if not isinstance(v, list):
        raise PolicyError(f"{where}: attesa una lista di minuti, es. [10, 5, 1]")
    return sorted({_num(x, where) for x in v}, reverse=True)


def _patterns(v, where: str) -> list:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise PolicyError(f"{where}: attesa una lista di testi, es. [\"steam:322170\"]")
    return [x.strip().lower() for x in v if x.strip()]


def _match(patterns: list, key: str, name: str) -> bool:
    k, n = key.lower(), (name or "").lower()
    for p in patterns:
        if p.startswith("name:"):
            if fnmatch.fnmatchcase(n, p[5:]):
                return True
        elif fnmatch.fnmatchcase(k, p):
            return True
    return False


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:40] or "app"


def default_day_type(date: dt.date) -> str:
    return "festivo" if date.weekday() >= 5 else "feriale"


class Policy:
    """Impostazioni tecniche (policy.toml). I limiti stanno in limiti.toml."""
    KEYS = {"mode", "idle_minutes", "default_hard_margin", "lock_message", "lock_grace_seconds",
            "relaunch_seconds", "new_app_minutes", "together_reminder_minutes",
            "together_repeat_minutes", "countdown_soft", "countdown_hard", "alerts",
            "exclude", "always_allowed", "on_total_exceeded"}
    # chiavi delle versioni precedenti: accettate (i limiti vengono importati in limiti.toml)
    LEGACY = {"lock", "lock_until", "grace", "total", "apps", "exceptions"}

    def __init__(self, data: dict):
        if not isinstance(data, dict):
            raise PolicyError("policy non valida")
        unknown = set(data) - self.KEYS - self.LEGACY
        if unknown:
            raise PolicyError(f"chiavi sconosciute: {', '.join(sorted(unknown))}")
        self.raw = data
        self.mode = str(data.get("mode", "monitor"))
        if self.mode not in ("monitor", "enforce"):
            raise PolicyError('mode: valori ammessi "monitor" o "enforce"')
        self.idle_minutes = _num(data.get("idle_minutes", 2), "idle_minutes") or 2.0
        self.default_hard_margin = _num(data.get("default_hard_margin", 5), "default_hard_margin")
        self.lock_message = str(data.get("lock_message",
                                         "Mamma e papà hanno messo in pausa la Deck."))
        self.lock_grace_seconds = _num(data.get("lock_grace_seconds", 60), "lock_grace_seconds")
        self.relaunch_seconds = _num(data.get("relaunch_seconds", 20), "relaunch_seconds")
        self.new_app_minutes = _num(data.get("new_app_minutes", 5), "new_app_minutes")
        self.together_reminder_minutes = _num(data.get("together_reminder_minutes", 90),
                                              "together_reminder_minutes")
        self.together_repeat_minutes = _num(data.get("together_repeat_minutes", 30),
                                            "together_repeat_minutes") or 30.0
        self.countdown_soft = _num_list(data.get("countdown_soft", []), "countdown_soft")
        self.countdown_hard = _num_list(data.get("countdown_hard", []), "countdown_hard")
        self.alerts = bool(data.get("alerts", True))
        self.exclude = _patterns(data.get("exclude", DEFAULT_EXCLUDE), "exclude")
        self.always_allowed = _patterns(data.get("always_allowed", DEFAULT_ALLOWED), "always_allowed")
        self.on_total_exceeded = str(data.get("on_total_exceeded", "kill"))
        if self.on_total_exceeded not in ("kill", "shutdown"):
            raise PolicyError('on_total_exceeded: valori ammessi "kill" o "shutdown"')

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"

    @property
    def legacy(self) -> bool:
        return bool(set(self.raw) & {"total", "apps", "grace", "exceptions"})

    def is_excluded(self, key: str, name: str) -> bool:
        return _match(self.exclude, key, name)

    def is_allowed(self, key: str, name: str) -> bool:
        return _match(self.always_allowed, key, name) or self.is_excluded(key, name)


def load_policy_file(path: Path) -> Policy:
    if not path.exists():
        log.warning("Policy %s non trovata: uso i valori predefiniti", path)
        return Policy({})
    return Policy(load_toml_text(path.read_text(encoding="utf-8")))


def _limit_entry(v, where: str, margin: float) -> Limit:
    if v is None:
        return Limit()
    if not isinstance(v, dict):
        raise PolicyError(f"{where}: atteso es. {{ concordato = 60, massimo = 65 }}")
    for k in v:
        if k not in ("concordato", "massimo"):
            raise PolicyError(f"{where}: chiave sconosciuta '{k}' (usa concordato / massimo)")
    soft = _num(v.get("concordato"), f"{where}.concordato")
    hard = _num(v.get("massimo"), f"{where}.massimo")
    if soft is not None and hard is None:
        hard = soft + margin
    if soft is not None and hard is not None and hard < soft:
        raise PolicyError(f"{where}: il massimo non può essere minore del concordato")
    return Limit(soft, hard)


def _norm_site(text: str) -> str:
    """'https://it.wikipedia.org/wiki/Roma' -> 'it.wikipedia.org'; mantiene '*.' iniziale."""
    t = text.strip().lower()
    wild = t.startswith("*.")
    if wild:
        t = t[2:]
    if "://" in t:
        t = urllib.parse.urlsplit(t).hostname or ""
    t = t.split("/")[0].split(":")[0].strip(".")
    if t.startswith("www."):
        t = t[4:]
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", t):
        raise PolicyError(f"sito non valido: {text!r}")
    return ("*." if wild else "") + t


def site_allowed(host: str, sites: list) -> bool:
    h = (host or "").lower().strip(".")
    for s in sites:
        if s.startswith("*."):
            base = s[2:]
            if h == base or h.endswith("." + base):
                return True
        elif h == s or h == "www." + s:
            return True
    return False


class Limits:
    """Limiti per tipo di giorno, regole per le app, siti e app di crescita (limiti.toml)."""

    def __init__(self, data: dict | None = None, margin: float = 5.0):
        data = data or {}
        self.margin = margin
        unknown = set(data) - {"totale", "app", "crescita", "scuola"}
        if unknown:
            raise PolicyError(f"limiti.toml: sezioni sconosciute: {', '.join(sorted(unknown))}")
        tot = data.get("totale", {}) or {}
        if not isinstance(tot, dict):
            raise PolicyError("[totale] non valido")
        for k in tot:
            if k not in DAY_TYPES:
                raise PolicyError(f"totale: chiave sconosciuta '{k}' (usa feriale / festivo)")
        self.total = {d: _limit_entry(tot.get(d), f"totale.{d}", margin) for d in DAY_TYPES}
        self.apps: dict[str, AppRule] = {}
        apps = data.get("app", {}) or {}
        if not isinstance(apps, dict):
            raise PolicyError("[app] non valido")
        for aid, a in apps.items():
            where = f"app.{aid}"
            if not isinstance(a, dict):
                raise PolicyError(f"{where}: non valido")
            for k in a:
                if k not in ("nome", "match") + DAY_TYPES:
                    raise PolicyError(f"{where}: chiave sconosciuta '{k}'")
            if "match" not in a:
                raise PolicyError(f"{where}: manca 'match' (es. match = [\"steam:322170\"])")
            self.apps[aid] = AppRule(
                id=aid, name=str(a.get("nome", aid)), patterns=_patterns(a["match"], f"{where}.match"),
                limits={d: _limit_entry(a.get(d), f"{where}.{d}", margin) for d in DAY_TYPES})
        # [crescita] (in passato [scuola]): siti e app che non contano come svago
        if "crescita" in data:
            sc = data.get("crescita") or {}
        elif "scuola" in data:
            sc = data.get("scuola") or {}
        else:
            sc = {"siti": DEFAULT_GROWTH_SITES}
        if not isinstance(sc, dict):
            raise PolicyError("[crescita] non valido")
        for k in sc:
            if k not in ("siti", "app"):
                raise PolicyError(f"crescita: chiave sconosciuta '{k}' (usa siti / app)")
        siti = sc.get("siti", [])
        if not isinstance(siti, list) or not all(isinstance(x, str) for x in siti):
            raise PolicyError("crescita.siti: attesa una lista di siti")
        self.growth_sites = [_norm_site(x) for x in siti]
        self.growth_apps = _patterns(sc.get("app", []), "crescita.app")

    # -- interrogazioni ---------------------------------------------------- #
    def app_for(self, key: str, name: str) -> str | None:
        for aid, r in self.apps.items():
            if _match(r.patterns, key, name):
                return aid
        return None

    def find_app(self, text: str) -> str | None:
        t = text.lower().strip()
        if not t:
            return None
        if t in self.apps:
            return t
        for aid, r in self.apps.items():
            if t == r.name.lower() or t in r.patterns or slugify(t) == aid:
                return aid
        cands = [aid for aid, r in self.apps.items() if t in r.name.lower()]
        return cands[0] if len(cands) == 1 else None

    def is_growth_app(self, key: str, name: str) -> bool:
        return _match(self.growth_apps, key, name)

    def day_limits(self, day_type: str, extras: dict | None = None):
        total = self.total.get(day_type, Limit())
        apps = {aid: r.limits.get(day_type, Limit()) for aid, r in self.apps.items()}
        if extras:
            total = total.plus(float(extras.get("total", 0)))
            for aid, m in (extras.get("apps") or {}).items():
                if aid in apps:
                    apps[aid] = apps[aid].plus(float(m))
        return total, apps

    # -- modifiche (comandi Telegram) -------------------------------------- #
    def new_app_id(self, name: str) -> str:
        base = slugify(name)
        aid, i = base, 2
        while aid in self.apps:
            aid, i = f"{base}_{i}", i + 1
        return aid

    def to_toml(self) -> str:
        q = lambda s: json.dumps(s, ensure_ascii=False)

        def lim(l: Limit) -> str:
            parts = []
            if l.soft is not None:
                parts.append(f"concordato = {l.soft:g}")
            if l.hard is not None:
                parts.append(f"massimo = {l.hard:g}")
            return "{ " + ", ".join(parts) + " }"
        out = [
            "# =====================================================================",
            "#  DeckWatch — LIMITI (limiti.toml)",
            "#  Questo file viene scritto dal bot Telegram (/limite_imposta, ecc.).",
            "#  Si può modificare anche a mano: viene ricaricato da solo.",
            "#  Tempi in minuti. concordato = avviso \"chiudi appena possibile\";",
            "#  massimo = oltre questo l'app si chiuderebbe (modalità enforce).",
            "#  feriale = lunedì-venerdì, festivo = sabato-domenica.",
            "# =====================================================================",
            "", "[totale]"]
        for d in DAY_TYPES:
            if not self.total[d].empty:
                out.append(f"{d} = {lim(self.total[d])}")
        for aid, r in self.apps.items():
            out += ["", f"[app.{q(aid)}]", f"nome = {q(r.name)}",
                    "match = [" + ", ".join(q(p) for p in r.patterns) + "]"]
            for d in DAY_TYPES:
                l = r.limits.get(d, Limit())
                if not l.empty:
                    out.append(f"{d} = {lim(l)}")
        out += ["", "[crescita]",
                "# Tempo che NON conta come svago (nel report: dettaglio per sito e app).",
                "# siti: valgono solo se in Firefox TUTTE le schede aperte sono di questa",
                "#       lista. \"*.sito.it\" include i sottodomini.",
                "# app:  identificativi delle app (es. \"class:scratch-desktop\").",
                "siti = [" + ", ".join(q(s) for s in self.growth_sites) + "]",
                "app = [" + ", ".join(q(s) for s in self.growth_apps) + "]", ""]
        return "\n".join(out)

    def summary_lines(self) -> list:
        def lt(l: Limit) -> str:
            if l.empty:
                return "nessun limite"
            s = []
            if l.soft is not None:
                s.append(f"concordato {fmt_min(l.soft)}")
            if l.hard is not None:
                s.append(f"massimo {fmt_min(l.hard)}")
            return ", ".join(s)
        lines = ["Totale:", f"  feriale: {lt(self.total['feriale'])}",
                 f"  festivo: {lt(self.total['festivo'])}"]
        for aid, r in self.apps.items():
            if all(r.limits.get(d, Limit()).empty for d in DAY_TYPES):
                continue
            f, w = r.limits.get("feriale", Limit()), r.limits.get("festivo", Limit())
            if f == w:
                lines.append(f"• {r.name}: {lt(f)}")
            else:
                lines.append(f"• {r.name}: feriale {lt(f)}; festivo {lt(w)}")
        return lines


def limits_diff(old: Limits, new: Limits) -> list:
    """Descrizione leggibile delle differenze tra due versioni dei limiti."""
    out = []

    def lt(l: Limit):
        return "nessuno" if l.empty else f"{fmt_min(l.soft) if l.soft is not None else '-'}/" \
                                          f"{fmt_min(l.hard) if l.hard is not None else '-'}"
    for d in DAY_TYPES:
        if old.total[d] != new.total[d]:
            out.append(f"totale {d}: {lt(old.total[d])} → {lt(new.total[d])}")
    for aid in sorted(set(old.apps) | set(new.apps)):
        o, n = old.apps.get(aid), new.apps.get(aid)
        name = (n or o).name
        for d in DAY_TYPES:
            lo = o.limits.get(d, Limit()) if o else Limit()
            ln = n.limits.get(d, Limit()) if n else Limit()
            if lo != ln:
                out.append(f"{name} {d}: {lt(lo)} → {lt(ln)}")
        if o and n and (o.name != n.name or o.patterns != n.patterns):
            out.append(f"{o.name}: nome/identificativo cambiato")
    for attr, label in (("growth_sites", "siti"), ("growth_apps", "app")):
        a, b = getattr(old, attr), getattr(new, attr)
        if a != b:
            add, rem = sorted(set(b) - set(a)), sorted(set(a) - set(b))
            if add:
                out.append(f"crescita, {label} aggiunti: " + ", ".join(add))
            if rem:
                out.append(f"crescita, {label} rimossi: " + ", ".join(rem))
    return out


def migrate_legacy_limits(data: dict, margin: float) -> Limits:
    """Importa i limiti dal vecchio policy.toml (v0.1/v0.2) nel nuovo formato."""
    old_days = {"feriale": "mon", "festivo": "sat"}

    def resolve(tbl: dict, day: str) -> Limit:
        # Nel vecchio formato "hard" era il tempo concordato e "soft" solo un preavviso:
        # il concordato diventa il vecchio hard, il massimo concordato + margine.
        soft, hard = tbl.get("soft"), tbl.get("hard")
        for k in ("weekend" if day in ("sat", "sun") else "weekdays", day):
            o = tbl.get(k)
            if isinstance(o, dict):
                soft = o.get("soft", soft)
                hard = o.get("hard", hard)
        agreed = hard if hard is not None else soft
        if agreed is None:
            return Limit()
        return Limit(float(agreed), float(agreed) + margin)
    L = Limits({}, margin)
    tot = data.get("total", {}) or {}
    L.total = {d: resolve(tot, od) for d, od in old_days.items()}
    for aid, a in (data.get("apps", {}) or {}).items():
        if isinstance(a, dict) and "match" in a:
            L.apps[slugify(aid)] = AppRule(slugify(aid), str(a.get("name", aid)),
                                           _patterns(a["match"], aid),
                                           {d: resolve(a, od) for d, od in old_days.items()})
    return L


# --------------------------------------------------------------------------- #
# Stato persistente
# --------------------------------------------------------------------------- #
def new_day(date: dt.date) -> dict:
    return {"version": 2, "date": date.isoformat(), "day_type": default_day_type(date),
            "total": 0.0, "apps": {}, "growth": 0.0, "growth_detail": {}, "together": 0.0,
            "offline": 0.0,
            "events": [], "flags": {}, "enforced": [], "reported": False}


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
        if "school" in st and "growth" not in st:   # formato v0.3
            st["growth"] = st.pop("school")
            if st["growth"]:
                st["growth_detail"] = {"Scuola": st["growth"]}
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
    """Piccolo dizionario persistente (stato dei comandi, metadati)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
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
            self._write()

    def update(self, fn):
        with self.lock:
            fn(self.data)
            self._write()

    def _write(self):
        try:
            atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))
        except OSError:
            log.exception("Scrittura %s non riuscita", self.path)


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


def app_breakdown(state: dict, limits: Limits):
    """({app_id: secondi} per le app con regola, [(secondi, nome, chiave)] per le altre)."""
    groups: dict[str, float] = {}
    others: dict[str, list] = {}
    for key, rec in state["apps"].items():
        aid = limits.app_for(key, rec.get("name", ""))
        if aid:
            groups[aid] = groups.get(aid, 0.0) + rec["seconds"]
        else:
            name = rec.get("name") or key
            o = others.setdefault(name, [0.0, key])
            o[0] += rec["seconds"]
    return groups, sorted(((v[0], k, v[1]) for k, v in others.items()), reverse=True)


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
    ref = lim.soft if lim.soft is not None else lim.hard
    if ref is None:
        return f"{fmt_min(used)} (nessun limite)"
    s = f"{fmt_min(used)} / {fmt_min(ref)}"
    if used - ref >= 0.5 and used > 0:
        s += f" (+{fmt_min(used - ref)})"
    return s


def _detail_rows(groups: dict, others: list, limits: Limits, show_keys: bool) -> list:
    items = [(secs, limits.apps[aid].name, None) for aid, secs in groups.items() if aid in limits.apps]
    items += [(secs, name, key) for secs, name, key in others]
    items.sort(key=lambda x: -x[0])
    rows, small = [], 0.0
    for secs, name, key in items:
        if secs < 60:
            small += secs
            continue
        txt = f"• {name}: {fmt_min(secs / 60)}"
        if show_keys and key and key.startswith("class:"):
            txt += f"  ({key})"
        rows.append(txt)
    if small >= 30:
        rows.append(f"• altre app brevi: {fmt_min(small / 60)}")
    return rows or ["(nessuna attività registrata)"]


def _growth_rows(detail: dict) -> list:
    rows = [f"• {name}: {fmt_min(secs / 60)}" for name, secs in
            sorted(detail.items(), key=lambda kv: -kv[1]) if secs >= 60]
    return ["", "DETTAGLIO CRESCITA"] + rows if rows else []


def _extra_text(extras: dict | None, limits: Limits) -> str | None:
    if not extras or not (extras.get("total") or extras.get("apps")):
        return None
    parts = []
    if extras.get("total"):
        parts.append(f"{extras['total']:+g} min sul totale")
    for aid, m in (extras.get("apps") or {}).items():
        parts.append(f"{m:+g} min a {limits.apps[aid].name if aid in limits.apps else aid}")
    return "Minuti extra concessi: " + ", ".join(parts)


PARENT_EVENT_KINDS = ("soft", "hard", "sim", "kill", "lock", "extra", "cmd", "edit", "alert",
                      "together", "daytype")


def build_report(state: dict, limits: Limits, audience: str, extras: dict | None = None,
                 as_of: str | None = None) -> tuple[str, str]:
    """Report giornaliero: riepilogo compatto in alto (effettivo / concordato), poi i dettagli."""
    date = dt.date.fromisoformat(state["date"])
    dtype = state.get("day_type") or default_day_type(date)
    total_lim, app_lims = limits.day_limits(dtype, extras)
    used = state["total"] / 60
    groups, others = app_breakdown(state, limits)
    day = f"{GIORNI[date.weekday()]} {date:%d/%m}"
    when = f", fino alle {as_of}" if as_of else ""

    if audience == "kid":
        lines = [f"Ciao! Ecco il tuo tempo sulla Deck di {day} (giorno {dtype}{when}).", ""]
    else:
        lines = [f"📊 DeckWatch — {day} (giorno {dtype}{when})", ""]

    lines.append("RIEPILOGO")
    overs = []
    ic = _icon(used, total_lim)
    lines.append(f"{ic} Svago totale: {_vs(used, total_lim)}")
    if ic != "✅":
        overs.append("il tempo totale")
    for aid, r in limits.apps.items():
        lim = app_lims.get(aid, Limit())
        if lim.empty:
            continue
        u = groups.get(aid, 0.0) / 60
        lines.append(f"{_icon(u, lim)} {r.name}: {_vs(u, lim)}")
        if _icon(u, lim) != "✅":
            overs.append(r.name)
    if state.get("growth", 0) >= 60:
        lines.append(f"🌱 Crescita: {fmt_min(state['growth'] / 60)} (non conteggiato)")
    if state.get("together", 0) >= 60:
        lines.append(f"👨‍👦 Insieme: {fmt_min(state['together'] / 60)} (non conteggiato)")

    lines += ["", "DETTAGLIO SVAGO"]
    lines += _detail_rows(groups, others, limits, show_keys=(audience != "kid"))
    lines += _growth_rows(state.get("growth_detail") or {})
    et = _extra_text(extras, limits)
    if et:
        lines.append(et)

    if audience == "kid":
        lines.append("")
        if overs:
            lines.append("Oggi sei andato oltre il tempo concordato per: " + ", ".join(overs) +
                         ". Domani cerchiamo di rispettarlo.")
        elif not total_lim.empty or any(not l.empty for l in app_lims.values()):
            lines.append("Tutto nei tempi concordati, ottimo! 👍")
        subject = f"Il tuo tempo sulla Deck di {GIORNI[date.weekday()]}: {fmt_min(used)}"
    else:
        if state.get("offline", 0) >= 300:
            lines += ["", f"📴 Offline durante l'uso: {fmt_min(state['offline'] / 60)}"]
        evs = [e for e in state.get("events", []) if len(e) > 2 and e[2] in PARENT_EVENT_KINDS]
        if evs:
            lines += ["", "EVENTI"]
            lines += [f"{e[0]} {e[1]}" for e in evs[-40:]]
        subject = f"DeckWatch {date:%d/%m}: {fmt_min(used)}" + (" ❌" if overs else "")
    return subject, "\n".join(lines)


def last_friday_before(today: dt.date) -> dt.date:
    """L'ultimo venerdì strettamente precedente a oggi (la settimana va da sabato a venerdì)."""
    delta = (today.weekday() - 4) % 7 or 7
    return today - dt.timedelta(days=delta)


def week_start(today: dt.date) -> dt.date:
    """Il sabato che apre la settimana (sab→ven) che contiene oggi."""
    return today - dt.timedelta(days=(today.weekday() - 5) % 7)


def build_weekly(states: dict, limits: Limits, extras_map: dict, audience: str,
                 partial: bool = False) -> tuple[str, str]:
    """Riepilogo settimanale. states: {data_iso: stato o None} per i giorni sab→ven."""
    dates = sorted(states)
    d0, d6 = dt.date.fromisoformat(dates[0]), dt.date.fromisoformat(dates[-1])
    period = f"{d0:%d/%m} – {d6:%d/%m}"
    week_total = growth = together = 0.0
    growth_detail: dict[str, float] = {}
    total_overs = 0
    app_week: dict[str, float] = {}
    app_over_days: dict[str, int] = {}
    other_week: dict[str, list] = {}
    day_lines = []
    any_total_limit = False
    limited = set()
    for iso in dates:
        date = dt.date.fromisoformat(iso)
        st = states[iso] or new_day(date)
        dtype = st.get("day_type") or default_day_type(date)
        total_lim, app_lims = limits.day_limits(dtype, extras_map.get(iso))
        used = st["total"] / 60
        week_total += used
        growth += st.get("growth", 0.0)
        for k, v in (st.get("growth_detail") or {}).items():
            growth_detail[k] = growth_detail.get(k, 0.0) + v
        together += st.get("together", 0.0)
        any_total_limit |= not total_lim.empty
        groups, others = app_breakdown(st, limits)
        day_overs = []
        for aid, secs in groups.items():
            app_week[aid] = app_week.get(aid, 0.0) + secs
        for aid, lim in app_lims.items():
            if not lim.empty:
                limited.add(aid)
            if _icon(groups.get(aid, 0.0) / 60, lim) != "✅":
                app_over_days[aid] = app_over_days.get(aid, 0) + 1
                day_overs.append(limits.apps[aid].name)
        if _icon(used, total_lim) != "✅":
            total_overs += 1
        for secs, name, key in others:
            o = other_week.setdefault(name, [0.0, key])
            o[0] += secs
        icon = "❌" if (day_overs and _icon(used, total_lim) == "✅") else _icon(used, total_lim)
        line = (f"{icon} {GIORNI_BREVI[date.weekday()]} {date:%d/%m}"
                f"{' (festivo)' if dtype == 'festivo' else ''}: {_vs(used, total_lim)}")
        if day_overs:
            line += " — oltre: " + ", ".join(day_overs)
        day_lines.append(line)

    n_days = len(dates)
    tag = " (fin qui)" if partial else ""
    if audience == "kid":
        lines = [f"Ciao! Ecco la tua settimana sulla Deck ({period}){tag}.", ""]
    else:
        lines = [f"📅 DeckWatch — settimana {period}{tag}", ""]
    lines.append("RIEPILOGO")
    lines.append(f"Svago nella settimana: {fmt_min(week_total)} "
                 f"(media {fmt_min(week_total / n_days)} al giorno)")
    if any_total_limit:
        lines.append(f"Giorni oltre il tempo concordato: {total_overs} su {n_days}")
    for aid in limits.apps:
        if aid in limited:
            n = app_over_days.get(aid, 0)
            lines.append(f"• {limits.apps[aid].name}: {fmt_min(app_week.get(aid, 0) / 60)} — "
                         f"oltre il concordato {n} {'giorno' if n == 1 else 'giorni'}")
    if growth >= 60:
        lines.append(f"🌱 Crescita: {fmt_min(growth / 60)} (non conteggiato)")
    if together >= 60:
        lines.append(f"👨‍👦 Insieme: {fmt_min(together / 60)} (non conteggiato)")
    lines += ["", "GIORNO PER GIORNO"] + day_lines
    lines += ["", "DETTAGLIO SVAGO (settimana)"]
    lines += _detail_rows(app_week, sorted(((v[0], k, v[1]) for k, v in other_week.items()),
                                           reverse=True), limits, show_keys=(audience != "kid"))
    lines += _growth_rows(growth_detail)
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


FIREFOX_KEYS = {"class:firefox", "class:firefox-esr", "class:org.mozilla.firefox"}

# (comando, descrizione nel menu Telegram, esempio d'uso o None)
COMMANDS = [
    ("report_oggi", "Report di oggi fin qui", None),
    ("report_settimana", "Riepilogo della settimana in corso", None),
    ("info_stato", "Cosa sta usando adesso e tempo di oggi", None),
    ("info_limiti", "Limiti feriali e festivi in vigore", None),
    ("info_app", "App viste di recente e loro identificativo", None),
    ("info_crescita", "Siti e app di crescita (non contano come svago)", None),
    ("info_versione", "Versione installata e aggiornamenti", None),
    ("info_comandi", "Elenco dei comandi con esempi", None),
    ("timer_pausa", "Mette in pausa lo svago (anche: /timer_pausa 60)", "/timer_pausa 60"),
    ("timer_riprendi", "Toglie la pausa", None),
    ("timer_concedi_extra", "Minuti in più solo per oggi", "/timer_concedi_extra 30  oppure  /timer_concedi_extra geometry dash 15"),
    ("timer_considera_oggi_festivo", "Oggi valgono i limiti festivi", None),
    ("timer_considera_oggi_feriale", "Oggi valgono i limiti feriali", None),
    ("timer_spegni_deck", "Spegne la Deck (in monitor: solo avviso simulato)", None),
    ("insieme_inizio", "Gioco insieme: il tempo non conta (anche: /insieme_inizio 60)", "/insieme_inizio 60"),
    ("insieme_fine", "Chiude la modalità insieme", None),
    ("limite_imposta", "Imposta un limite (es. geometry dash 60)", "/limite_imposta geometry dash 60  ·  /limite_imposta geometry dash festivo 60 75  ·  /limite_imposta totale feriale 120"),
    ("limite_rimuovi", "Toglie il limite di un'app", "/limite_rimuovi minecraft  ·  /limite_rimuovi totale festivo"),
    ("config_nome_app", "Dà un nome leggibile a un'app", "/config_nome_app class:game.exe Fortnite"),
    ("config_aggiungi_sito_crescita", "Aggiunge un sito di crescita", "/config_aggiungi_sito_crescita chess.com"),
    ("config_rimuovi_sito_crescita", "Toglie un sito di crescita", "/config_rimuovi_sito_crescita chess.com"),
    ("config_aggiungi_app_crescita", "Aggiunge un'app di crescita", "/config_aggiungi_app_crescita scratch  ·  /config_aggiungi_app_crescita class:code"),
    ("config_rimuovi_app_crescita", "Toglie un'app di crescita", "/config_rimuovi_app_crescita class:code"),
    ("config_aggiorna", "Controlla e installa subito gli aggiornamenti", None),
]


class Engine:
    def __init__(self, platform, store: DayStore, outbox, cfg: Config, policy: Policy,
                 limits: Limits, overrides: JsonFile, meta: JsonFile, save_limits=None):
        self.platform = platform
        self.store = store
        self.outbox = outbox
        self.cfg = cfg
        self.policy = policy
        self.limits = limits
        self.overrides = overrides
        self.meta = meta
        self._save_limits = save_limits
        self.state: dict = new_day(dt.date.today())
        self._pending: dict[str, dict] = {}
        self._last_lock_popup = 0.0
        self._last_save = 0.0
        self._map_cache: tuple = (None, {})
        self._unseen: dict[str, float] = {}
        self.seen: dict = dict(meta.get("seen_apps") or {})
        self.last_fg: Foreground | None = None
        self.last_active = False
        self.last_growth: str | None = None
        self.online = True
        self.on_update_request = None
        self._now: dt.datetime | None = None

    # -- ciclo di vita ----------------------------------------------------- #
    def start(self, now: dt.datetime) -> None:
        self._now = now
        today = now.date().isoformat()
        st = self.store.load(today)
        if st is None:
            st = new_day(now.date())
            prev = self.store.latest_before(today)
            if prev:
                self._close_day(prev, st)
        self.state = st
        if self.meta.get("seen_apps") is None:  # prima installazione: non notifico le app già note
            for d in self.store.dates():
                s = self.store.load(d) or {}
                for k, rec in (s.get("apps") or {}).items():
                    self.seen.setdefault(k, {"name": rec.get("name", k), "last": d})
            self.meta.set("seen_apps", self.seen)
        self._end_together_if_stale(now)
        self._maybe_weekly(now.date())
        self.store.cleanup()
        self.save(now.timestamp())

    def save(self, ts: float | None = None) -> None:
        self.store.save(self.state)
        self.meta.set("seen_apps", self.seen)
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
            subj, body = build_weekly(states, self.limits, extras, "parents")
            self.outbox.to_parents(body, subj)
            subj, body = build_weekly(states, self.limits, extras, "kid")
            self.outbox.to_kid(body, subj)
        self.overrides.set("last_weekly", fri.isoformat())

    def _rollover(self, now: dt.datetime) -> None:
        if now.date().isoformat() == self.state["date"]:
            return
        new = new_day(now.date())
        self._close_day(self.state, new)
        self.state = new
        self._pending.clear()
        self._end_together_if_stale(now)
        self._maybe_weekly(now.date())
        self.save(now.timestamp())

    # -- stato dei comandi (overrides.json) -------------------------------- #
    def extras_for(self, date_iso: str) -> dict:
        return (self.overrides.get("extras") or {}).get(date_iso, {})

    def day_type(self, date: dt.date) -> str:
        return (self.overrides.get("day_types") or {}).get(date.isoformat()) or default_day_type(date)

    def remote_locked(self, now: dt.datetime) -> bool:
        if self.overrides.get("lock"):
            return True
        until = self.overrides.get("lock_until")
        return bool(until and now.timestamp() < until)

    def together(self) -> dict | None:
        return self.overrides.get("together") or None

    def _end_together_if_stale(self, now: dt.datetime) -> None:
        t = self.together()
        if t and dt.datetime.fromtimestamp(t["since"]).date() != now.date():
            self.overrides.set("together", None)
            self.overrides.set("lock_since", None)
            mins = (now.timestamp() - t["since"]) / 60
            self.outbox.to_parents("👨‍👦 Modalità insieme chiusa automaticamente a mezzanotte.")
            self._event("together", f"Modalità insieme chiusa a mezzanotte (iniziata "
                                    f"{fmt_min(mins)} prima)")

    # -- mappature --------------------------------------------------------- #
    def app_id_for(self, key: str, name: str) -> str | None:
        lim = self.limits
        if self._map_cache[0] is not lim:
            self._map_cache = (lim, {})
        cache = self._map_cache[1]
        if key not in cache:
            cache[key] = lim.app_for(key, name)
        return cache[key]

    def app_label(self, key: str, name: str) -> str:
        aid = self.app_id_for(key, name)
        if aid:
            return self.limits.apps[aid].name
        return (self.seen.get(key) or {}).get("name") or name or key

    def usage_by_app(self) -> dict:
        out: dict[str, float] = {}
        for key, rec in self.state["apps"].items():
            aid = self.app_id_for(key, rec.get("name", ""))
            if aid:
                out[aid] = out.get(aid, 0.0) + rec["seconds"]
        return out

    def limits_now(self, date: dt.date):
        return self.limits.day_limits(self.day_type(date), self.extras_for(date.isoformat()))

    def limits_changed(self) -> None:
        self._map_cache = (None, {})
        if self._save_limits:
            self._save_limits(self.limits)

    # -- eventi e notifiche ------------------------------------------------ #
    def _event(self, kind: str, text: str) -> None:
        evs = self.state.setdefault("events", [])
        evs.append([(self._now or dt.datetime.now()).strftime("%H:%M"), text, kind])
        del evs[:-300]

    def _say(self, title: str, body: str, level: str = "info", kind: str = "info",
             event: bool = True) -> None:
        """level: info (notifica normale), soft (notifica critica, senza suono),
        strong (notifica critica + popup + suono). Con un gioco a tutto schermo solo le
        notifiche critiche sono visibili; il popup prende il controllo dello schermo."""
        log.info("Avviso: %s — %s", title, body)
        try:
            self.platform.notify(title, body, level in ("soft", "strong"))
            if level == "strong":
                self.platform.popup(f"{title}\n\n{body}")
            if level == "strong":
                self.platform.alarm()
        except Exception:
            log.exception("Notifica non riuscita")
        if event:
            self._event(kind, title if kind in ("lock",) else f"{title} — {body}")

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
        st["day_type"] = self.day_type(now.date())

        fg = self.platform.foreground()
        idle = self.platform.idle_seconds()
        active = idle is None or idle < pol.idle_minutes * 60
        growth = None   # etichetta (sito o app) se il tempo è "crescita"
        if fg and self.limits.is_growth_app(fg.key, fg.name):
            growth = self.app_label(fg.key, fg.name)
        elif fg and fg.key in FIREFOX_KEYS:
            try:
                growth = self.platform.firefox_growth(self.limits.growth_sites)
            except Exception:
                log.exception("Lettura sessione Firefox")
        self.last_fg, self.last_active, self.last_growth = fg, active, growth
        together = self.together()

        if fg and active and delta > 0 and not pol.is_excluded(fg.key, fg.name):
            self._track_seen(fg, delta)
            if together:
                st["together"] = st.get("together", 0.0) + delta
            elif growth:
                st["growth"] = st.get("growth", 0.0) + delta
                gd = st.setdefault("growth_detail", {})
                gd[growth] = gd.get(growth, 0.0) + delta
            else:
                rec = st["apps"].setdefault(fg.key, {"name": fg.name or fg.key, "seconds": 0.0})
                if fg.name:
                    rec["name"] = fg.name
                rec["seconds"] += delta
                st["total"] += delta
            if not self.online:
                st["offline"] = st.get("offline", 0.0) + delta

        if together:
            self._pending.clear()
            self._together_reminders(ts)
        else:
            total_lim, app_lims = self.limits_now(now.date())
            usage = self.usage_by_app()
            total_status = self._check("total", None, total_lim, st["total"] / 60, ts)
            app_status = {aid: self._check(f"app:{aid}", self.limits.apps[aid].name, lim,
                                           usage.get(aid, 0.0) / 60, ts)
                          for aid, lim in app_lims.items()}
            locked = self.remote_locked(now)
            lock_on = self._update_lock(locked, ts)
            targets = self._collect_targets(fg, bool(growth), total_status, app_status, lock_on)
            if locked and targets and ts - self._last_lock_popup >= 60:
                self._last_lock_popup = ts
                try:
                    self.platform.popup(f"⏸ Deck in pausa\n\n{pol.lock_message}")
                except Exception:
                    log.exception("popup")
            self._apply_targets(targets, ts)
        if ts - self._last_save >= 30:
            self.save(ts)

    def _track_seen(self, fg: Foreground, delta: float) -> None:
        today = self.state["date"]
        info = self.seen.get(fg.key)
        if info is not None:
            info["last"] = today
            if fg.name:
                info["name"] = fg.name
            return
        if self.app_id_for(fg.key, fg.name) or fg.key in FIREFOX_KEYS:
            self.seen[fg.key] = {"name": fg.name, "last": today}
            return
        acc = self._unseen.get(fg.key, 0.0) + delta
        self._unseen[fg.key] = acc
        if acc >= self.policy.new_app_minutes * 60:
            self.seen[fg.key] = {"name": fg.name, "last": today}
            self._unseen.pop(fg.key, None)
            label = fg.name or fg.key
            msg = (f"🆕 Nuova app in uso: {label}\n"
                   + (f"Titolo della finestra: {fg.title}\n" if fg.title and fg.title != label else "")
                   + f"Identificativo: {fg.key}\n\n"
                   f"Per darle un nome:\n/config_nome_app {fg.key} Nome\n"
                   f"Per impostare un limite:\n/limite_imposta {fg.key} 60")
            self.outbox.to_parents(msg, "DeckWatch: nuova app")
            self._event("alert", f"Nuova app: {label} ({fg.key})")

    def _countdown(self, lid: str, kind: str, limit: float, remaining: float, marks: list,
                   text) -> None:
        due = [w for w in marks if remaining <= w]
        if not due:
            return
        fl = self.state["flags"]
        w = min(due)
        k = f"{lid}|{kind}cd|{w:g}|{limit:g}"
        if k in fl:
            return
        for x in due:
            fl[f"{lid}|{kind}cd|{x:g}|{limit:g}"] = 1
        self._say("⏳ Countdown", text(remaining), level="info", kind="warn", event=False)

    def _check(self, lid: str, name: str | None, lim: Limit, used: float, ts: float) -> str:
        fl, pol = self.state["flags"], self.policy
        what = f"per {name}" if name else "per oggi"
        if lim.soft is not None:
            rem = lim.soft - used
            if rem > 0:
                self._countdown(lid, "soft", lim.soft, rem, pol.countdown_soft,
                                lambda r: f"Mancano {fmt_min(r)} al tempo concordato {what}.")
            elif used > 0:
                k = f"{lid}|soft|{lim.soft:g}"
                if k not in fl:
                    fl[k] = ts
                    self._say("⏰ Tempo concordato terminato",
                              f"Il tempo concordato {what} è terminato. Chiudi appena possibile.",
                              level="soft", kind="soft")
        if lim.hard is None:
            return "ok"
        if lim.hard <= 0 and used <= 0:
            return "blocked"
        rem = lim.hard - used
        if rem > 0:
            if pol.enforce:
                msg = lambda r: (f"Tra {fmt_min(r)} {name} verrà chiuso." if name else
                                 f"Tra {fmt_min(r)} le app verranno chiuse.")
            else:
                msg = lambda r: f"Mancano {fmt_min(r)} al limite massimo {what}."
            self._countdown(lid, "hard", lim.hard, rem, pol.countdown_hard, msg)
            return "ok"
        k = f"{lid}|hard|{lim.hard:g}"
        if k not in fl:
            fl[k] = ts
            fl[f"hardhit|{lid}"] = ts
            if pol.enforce:
                body = (f"Il tempo massimo per {name} è terminato: l'applicazione viene chiusa."
                        if name else
                        "Il tempo massimo per oggi è terminato: le applicazioni vengono chiuse. "
                        "Restano disponibili le app e i siti di crescita e le app di sistema.")
            else:
                body = (f"Il tempo concordato per {name} è terminato. L'applicazione continua a "
                        "funzionare, ma in questo momento si sarebbe chiusa automaticamente."
                        if name else
                        "Il tempo concordato per oggi è terminato. Le applicazioni continuano a "
                        "funzionare, ma in questo momento si sarebbero chiuse automaticamente.")
            self._say("⛔ Tempo massimo raggiunto", body, level="strong", kind="hard")
        return "blocked"

    def _update_lock(self, locked: bool, ts: float) -> bool:
        pol = self.policy
        since = self.overrides.get("lock_since")
        if locked:
            if since is None:
                since = ts
                self.overrides.set("lock_since", ts)
                self._say("⏸ Deck in pausa",
                          f"{pol.lock_message} Tra {int(pol.lock_grace_seconds)} secondi le "
                          "applicazioni verranno chiuse: salva adesso. App e siti di crescita "
                          "restano disponibili.", level="strong", kind="lock")
            return ts - since >= pol.lock_grace_seconds
        if since is not None:
            self.overrides.set("lock_since", None)
            if "lock" in self.state["enforced"]:
                self.state["enforced"].remove("lock")
            self._say("▶️ Pausa terminata", "Puoi di nuovo usare la Deck.", kind="lock")
        return False

    def _collect_targets(self, fg, growth_ok, total_status, app_status, lock_on) -> dict:
        pol = self.policy
        targets: dict[str, dict] = {}
        total_block = lock_on or total_status == "blocked"
        total_lid = "lock" if lock_on else "total"
        total_reason = "la Deck è in pausa." if lock_on else "il tempo massimo di oggi è terminato."

        def consider(key, name, fgobj, appid, growth=False):
            if key in targets or growth:
                return
            aid = self.app_id_for(key, name)
            if aid and app_status.get(aid) == "blocked":
                targets[key] = dict(name=name, lid=f"app:{aid}", fg=fgobj, appid=appid,
                                    reason=f"il tempo massimo per {self.limits.apps[aid].name} "
                                           "di oggi è terminato.")
            elif total_block and not pol.is_allowed(key, name):
                targets[key] = dict(name=name, lid=total_lid, fg=fgobj, appid=appid,
                                    reason=total_reason)

        if fg:
            consider(fg.key, fg.name, fg, fg.steam_appid, growth=growth_ok)
        if lock_on or (pol.enforce and (total_status == "blocked" or
                                        any(s == "blocked" for s in app_status.values()))):
            for appid in self.platform.running_steam_games():
                key = f"steam:{appid}"
                name = self.platform.steam_name(appid) or key
                consider(key, name, None, appid, growth=self.limits.is_growth_app(key, name))
        return targets

    def _apply_targets(self, targets: dict, ts: float) -> None:
        pol = self.policy
        for key in list(self._pending):
            if key not in targets:
                del self._pending[key]
        enforced = self.state["enforced"]
        flags = self.state["flags"]
        for key, t in targets.items():
            lid = t["lid"]
            if not pol.enforce and lid != "lock":
                k = f"sim|{lid}"
                if k not in flags:
                    flags[k] = ts
                    self._event("sim", f"(simulazione) {t['name']} sarebbe stato chiuso: "
                                       f"{t['reason']}")
                continue
            immediate = lid not in enforced and (lid == "lock" or f"hardhit|{lid}" in flags)
            p = self._pending.get(key)
            if p is None and not immediate:
                self._pending[key] = {"t": ts}
                self._say(f"{t['name']} non è disponibile ora",
                          f"Motivo: {t['reason']} Verrà chiuso tra {int(pol.relaunch_seconds)} "
                          "secondi.", level="strong", kind="warn")
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
            self._event("kill", f"Spegnimento della Deck: tempo massimo finito ({t['name']})")
            self._alert("shutdown", f"⏻ Deck spenta: tempo massimo di oggi finito "
                                    f"(era aperto {t['name']}).")
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
        if t["lid"] != "lock":
            self._alert(f"kill|{t['lid']}", f"⛔ Chiuso {t['name']}: {t['reason']}")

    def _together_reminders(self, ts: float) -> None:
        t = self.together()
        if not t or ts < t.get("next", 0):
            return
        mins = (ts - t["since"]) / 60
        if not t.get("first_done"):
            self._say("👨‍👦 Modalità insieme", f"Modalità insieme attiva da {fmt_min(mins)}.",
                      event=False)
        self.outbox.to_parents(f"⏰ Modalità insieme attiva da {fmt_min(mins)}. "
                               "Mandate /insieme_fine quando avete finito.")
        t["first_done"] = True
        t["next"] = ts + self.policy.together_repeat_minutes * 60
        self.overrides.set("together", t)

    # -- report ------------------------------------------------------------ #
    def _send_reports(self, st: dict, as_of: str | None) -> None:
        extras = self.extras_for(st["date"])
        subj, body = build_report(st, self.limits, "parents", extras, as_of)
        self.outbox.to_parents(body, subj)
        subj, body = build_report(st, self.limits, "kid", extras)
        self.outbox.to_kid(body, subj)

    def week_so_far(self, today: dt.date, audience: str = "parents") -> str:
        start = week_start(today)
        days = [(start + dt.timedelta(days=i)).isoformat() for i in range((today - start).days + 1)]
        states = {iso: (self.state if iso == self.state["date"] else self.store.load(iso))
                  for iso in days}
        extras = {iso: self.extras_for(iso) for iso in days}
        return build_weekly(states, self.limits, extras, audience, partial=True)[1]

    # -- comandi Telegram -------------------------------------------------- #
    def help_text(self) -> str:
        lines = ["Comandi DeckWatch (tocca il pulsante Menu o scrivi / per l'elenco):", ""]
        for name, desc, example in COMMANDS:
            lines.append(f"/{name} — {desc}")
            if example:
                lines.append(f"   es. {example}")
        lines += ["", "Modalità attuale: " + (
            "enforce (i limiti massimi chiudono le app)." if self.policy.enforce else
            "monitor (nessuna chiusura per i limiti: solo avvisi e simulazione; "
            "la pausa funziona sempre).")]
        return "\n".join(lines)

    def _usage(self, cmd: str) -> str:
        for name, desc, example in COMMANDS:
            if name == cmd:
                return f"{desc}.\nUso: {example or '/' + name}"
        return "Comando non valido."

    def _broadcast_others(self, chat_id: int, text: str) -> None:
        for c in self.cfg.tg_parents:
            if c != chat_id:
                self.outbox.enqueue("telegram", str(c), "DeckWatch", text)

    def _find_app_key(self, text: str) -> tuple[str | None, str | None]:
        """Identificativo di un'app da nome o identificativo. Ritorna (chiave, errore)."""
        t = text.strip().lower()
        if re.match(r"^(steam|class):\S+$", t):
            return t, None
        aid = self.limits.find_app(t)
        if aid and len(self.limits.apps[aid].patterns) == 1:
            return self.limits.apps[aid].patterns[0], None
        cands: dict[str, str] = {}
        try:
            for appid, nm in self.platform.steam_find(t):
                cands[f"steam:{appid}"] = nm
        except Exception:
            log.exception("ricerca giochi Steam")
        known = {k: (v.get("name") or "") for k, v in self.seen.items()}
        known.update({k: rec.get("name") or k for k, rec in self.state["apps"].items()})
        for key, nm in known.items():
            if t in nm.lower() or t in key:
                cands.setdefault(key, nm)
        exact = {k: v for k, v in cands.items() if v.lower() == t}
        cands = exact or cands
        if not cands:
            return None, (f"Non trovo \"{text}\". Apri l'app sulla Deck almeno una volta, poi "
                          "cerca il suo identificativo con /info_app.")
        if len(cands) > 1:
            return None, ("Più app corrispondono, usa l'identificativo:\n" +
                          "\n".join(f"• {v} ({k})" for k, v in list(cands.items())[:10]))
        return next(iter(cands)), None

    def _resolve_app(self, text: str, create: bool) -> tuple[str | None, str | None]:
        """Trova (o crea) la regola per un'app. Ritorna (app_id, messaggio_errore)."""
        L = self.limits
        aid = L.find_app(text)
        if aid:
            return aid, None
        t = text.strip().lower()
        cands: dict[str, str] = {}
        if re.match(r"^(steam|class):\S+$", t):
            cands[t] = (self.seen.get(t) or {}).get("name") or t
        else:
            try:
                for appid, nm in self.platform.steam_find(t):
                    cands[f"steam:{appid}"] = nm
            except Exception:
                log.exception("ricerca giochi Steam")
            known = {k: (v.get("name") or "") for k, v in self.seen.items()}
            known.update({k: rec.get("name") or k for k, rec in self.state["apps"].items()})
            for key, nm in known.items():
                if t == nm.lower() or (t in nm.lower() and key not in cands):
                    cands[key] = nm
            exact = {k: v for k, v in cands.items() if v.lower() == t}
            if exact:
                cands = exact
        if not cands:
            return None, (f"Non trovo \"{text}\". Con /info_app vedi le app usate e il loro "
                          "identificativo (es. steam:322170 o class:firefox).")
        if len(cands) > 1:
            return None, ("Più app corrispondono, scrivi il nome completo o l'identificativo:\n" +
                          "\n".join(f"• {v} ({k})" for k, v in list(cands.items())[:10]))
        if not create:
            return None, f"\"{text}\" non ha regole."
        key, name = next(iter(cands.items()))
        aid = L.new_app_id(name)
        L.apps[aid] = AppRule(aid, name, [key], {d: Limit() for d in DAY_TYPES})
        return aid, None

    @staticmethod
    def _split_limit_args(args: list):
        """[nome..., (feriale|festivo)?, n1, n2?] -> (nome, tipi_giorno, soft, hard)."""
        nums = []
        while args and re.fullmatch(r"\d+(\.\d+)?", args[-1]) and len(nums) < 2:
            nums.insert(0, float(args.pop()))
        days = list(DAY_TYPES)
        if args and args[-1].lower() in ("feriale", "feriali", "festivo", "festivi"):
            days = ["feriale" if args.pop().lower().startswith("ferial") else "festivo"]
        return " ".join(args), days, nums

    def handle_command(self, chat_id: int, sender: str, text: str, msg_ts: float,
                       now: dt.datetime) -> str | None:
        self._now = now
        parts = text.strip().split()
        if not parts:
            return None
        cmd = parts[0].split("@")[0].lower().lstrip("/")
        args = parts[1:]
        age = now.timestamp() - msg_ts
        today = now.date().isoformat()
        pol, L = self.policy, self.limits

        # Comandi arrivati in ritardo (Deck spenta o offline): informazioni e azioni su
        # "adesso" scadono; regole e configurazione restano valide.
        group = "info" if cmd == "start" else cmd.split("_")[0]
        max_age = {"info": 60, "report": 60, "timer": 600, "insieme": 600}.get(group)
        if max_age is not None and age > max_age:
            if group in ("timer", "insieme"):
                return (f"⌛ /{cmd} è arrivato con {fmt_min(age / 60)} di ritardo (Deck spenta "
                        "o offline): ignorato.")
            return None
        if cmd in ("start", "info_comandi"):
            return self.help_text()
        if cmd == "info_stato":
            return self.status_text(now)
        if cmd == "report_oggi":
            return build_report(self.state, L, "parents", self.extras_for(today),
                                now.strftime("%H:%M"))[1]
        if cmd == "report_settimana":
            return self.week_so_far(now.date())
        if cmd == "info_limiti":
            return self.limits_text(now.date())
        if cmd == "info_crescita":
            return self.growth_text()
        if cmd == "info_app":
            return self.apps_text()
        if cmd == "info_versione":
            last = self.meta.get("last_update_check")
            res = self.meta.get("last_update_result") or "—"
            when = dt.datetime.fromtimestamp(last).strftime("%d/%m %H:%M") if last else "mai"
            return f"DeckWatch {__version__}\nUltimo controllo aggiornamenti: {when} ({res})"

        if cmd == "timer_pausa":
            if args:
                try:
                    mins = float(args[0])
                except ValueError:
                    return self._usage(cmd)
                until = msg_ts + mins * 60
                if until <= now.timestamp():
                    return "Comando ricevuto in ritardo (Deck spenta o offline): la pausa sarebbe già finita."
                self.overrides.update(lambda d: d.update(lock=False, lock_until=until))
                hhmm = dt.datetime.fromtimestamp(until).strftime("%H:%M")
                self._event("cmd", f"{sender}: pausa fino alle {hhmm}")
                self._broadcast_others(chat_id, f"⏸ {sender} ha messo in pausa la Deck fino alle {hhmm}.")
                return f"⏸ Pausa fino alle {hhmm}. /timer_riprendi per annullare."
            self.overrides.update(lambda d: d.update(lock=True, lock_until=None))
            self._event("cmd", f"{sender}: pausa")
            self._broadcast_others(chat_id, f"⏸ {sender} ha messo in pausa la Deck.")
            extra = " (La modalità insieme è attiva: la pausa partirà a /insieme_fine.)" \
                if self.together() else ""
            return "⏸ Deck in pausa. Manda /timer_riprendi per togliere la pausa." + extra
        if cmd == "timer_riprendi":
            self.overrides.update(lambda d: d.update(lock=False, lock_until=None))
            self._event("cmd", f"{sender}: pausa tolta")
            self._broadcast_others(chat_id, f"▶️ {sender} ha tolto la pausa.")
            return "▶️ Pausa tolta."
        if cmd == "timer_concedi_extra":
            if not args:
                return self._usage(cmd)
            try:
                mins = float(args[-1])
            except ValueError:
                return self._usage(cmd)
            aid = None
            if len(args) > 1:
                aid = L.find_app(" ".join(args[:-1]))
                if not aid:
                    return "App non trovata tra quelle con un limite. Con /info_limiti vedi l'elenco."
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
            what = L.apps[aid].name if aid else "il totale"
            self._event("extra", f"{sender}: {mins:+g} min per {what}")
            self._say("🎁 Tempo extra", f"I genitori ti hanno dato {mins:+g} minuti per {what}.",
                      kind="extra", event=False)
            self._broadcast_others(chat_id, f"🎁 {sender} ha dato {mins:+g} min per {what}.")
            return f"🎁 Fatto: {mins:+g} min per {what}, solo oggi.\n\n" + self.limits_text(now.date())
        if cmd in ("timer_considera_oggi_festivo", "timer_considera_oggi_feriale"):
            dtype = "festivo" if cmd.endswith("festivo") else "feriale"

            def upd(d):
                m = d.setdefault("day_types", {})
                for k in list(m):
                    if k < (now.date() - dt.timedelta(days=14)).isoformat():
                        del m[k]
                m[today] = dtype
            self.overrides.update(upd)
            self.state["day_type"] = dtype
            self._event("daytype", f"{sender}: oggi vale come giorno {dtype}")
            self._say("📅 Giorno " + dtype, f"Oggi valgono i limiti del giorno {dtype}.",
                      event=False)
            self._broadcast_others(chat_id, f"📅 {sender}: oggi vale come giorno {dtype}.")
            return f"📅 Oggi vale come giorno {dtype}.\n\n" + self.limits_text(now.date())
        if cmd == "timer_spegni_deck":
            if not pol.enforce:
                self._say("⏻ Spegnimento (simulazione)", "I genitori hanno chiesto lo "
                          "spegnimento: la Deck si sarebbe spenta adesso. Per ora è solo una "
                          "simulazione.", level="strong", kind="sim")
                self._broadcast_others(chat_id, f"⏻ {sender} ha chiesto lo spegnimento (simulato).")
                return "Modalità monitor: ho mostrato l'avviso, la Deck NON è stata spenta."
            self._event("cmd", f"{sender}: spegnimento")
            self._broadcast_others(chat_id, f"⏻ {sender} ha spento la Deck.")
            self.save()
            self._say("⏻ Spegnimento", "I genitori stanno spegnendo la Deck: salva subito.",
                      level="strong", event=False)
            threading.Timer(30, self.platform.poweroff).start()
            return "⏻ La Deck si spegnerà tra 30 secondi."

        if cmd == "insieme_inizio":
            mins = self.policy.together_reminder_minutes
            if args:
                try:
                    mins = float(args[0])
                except ValueError:
                    return self._usage(cmd)
            if self.together():
                return "La modalità insieme è già attiva. /insieme_fine per chiuderla."
            ts = now.timestamp()
            self.overrides.set("together", {"since": ts, "next": ts + mins * 60,
                                            "first_done": False, "by": sender})
            self._event("together", f"{sender}: modalità insieme iniziata")
            self._say("👨‍👦 Modalità insieme attiva", "Il tempo non viene conteggiato.",
                      event=False)
            self._broadcast_others(chat_id, f"👨‍👦 {sender} ha attivato la modalità insieme.")
            return (f"👨‍👦 Modalità insieme attiva. Promemoria tra {fmt_min(mins)}. "
                    "/insieme_fine per chiuderla.")
        if cmd == "insieme_fine":
            t = self.together()
            if not t:
                return "La modalità insieme non è attiva."
            self.overrides.set("together", None)
            self.overrides.set("lock_since", None)
            mins = (now.timestamp() - t["since"]) / 60
            self._event("together", f"{sender}: modalità insieme chiusa ({fmt_min(mins)})")
            self._say("👨‍👦 Modalità insieme terminata", "Il tempo torna a contare.", event=False)
            self._broadcast_others(chat_id, f"👨‍👦 {sender} ha chiuso la modalità insieme.")
            return f"👨‍👦 Modalità insieme chiusa dopo {fmt_min(mins)}."

        if cmd == "limite_imposta":
            name, days, nums = self._split_limit_args(list(args))
            if not name or not nums:
                return self._usage(cmd) + "\n\nLimiti attuali:\n" + "\n".join(L.summary_lines())
            soft = nums[0]
            hard = nums[1] if len(nums) > 1 else soft + pol.default_hard_margin
            if hard < soft:
                return "Il secondo numero (massimo) non può essere minore del primo (concordato)."
            if name.lower() == "totale":
                for d in days:
                    L.total[d] = Limit(soft, hard)
                what = "Totale"
            else:
                aid, err = self._resolve_app(name, create=True)
                if err:
                    return err
                for d in days:
                    L.apps[aid].limits[d] = Limit(soft, hard)
                what = L.apps[aid].name
            self.limits_changed()
            dtxt = " e ".join(days) if len(days) == 1 else "feriale e festivo"
            desc = f"{what} ({dtxt}): concordato {fmt_min(soft)}, massimo {fmt_min(hard)}"
            self._event("edit", f"{sender}: {desc}")
            self._broadcast_others(chat_id, f"✏️ {sender} ha impostato: {desc}.")
            return f"✏️ {desc}. Vale da subito.\n\n" + self.limits_text(now.date())
        if cmd == "limite_rimuovi":
            name, days, nums = self._split_limit_args(list(args))
            if not name or nums:
                return self._usage(cmd)
            if name.lower() == "totale":
                for d in days:
                    L.total[d] = Limit()
                what = "Totale"
            else:
                aid = L.find_app(name)
                if not aid:
                    return "App non trovata tra quelle con un limite. Con /info_limiti vedi l'elenco."
                for d in days:
                    L.apps[aid].limits[d] = Limit()
                what = L.apps[aid].name
            self.limits_changed()
            desc = f"{what}: limite rimosso ({' e '.join(days)})"
            self._event("edit", f"{sender}: {desc}")
            self._broadcast_others(chat_id, f"✏️ {sender}: {desc}.")
            return f"✏️ {desc}.\n\n" + self.limits_text(now.date())
        if cmd == "config_nome_app":
            if len(args) < 2 or not re.match(r"^(steam|class):\S+$", args[0].lower()):
                return self._usage(cmd) + "\nGli identificativi li trovi con /info_app."
            key, new_name = args[0].lower(), " ".join(args[1:])
            aid = L.app_for(key, "")
            if aid:
                L.apps[aid].name = new_name
            else:
                aid = L.new_app_id(new_name)
                L.apps[aid] = AppRule(aid, new_name, [key], {d: Limit() for d in DAY_TYPES})
            self.limits_changed()
            self._event("edit", f"{sender}: {key} si chiama \"{new_name}\"")
            self._broadcast_others(chat_id, f"✏️ {sender}: {key} ora si chiama \"{new_name}\".")
            return f"✏️ {key} ora si chiama \"{new_name}\"."
        if cmd in ("config_aggiungi_sito_crescita", "config_rimuovi_sito_crescita"):
            if not args:
                return self._usage(cmd) + "\n\n" + self.growth_text()
            try:
                site = _norm_site(args[0])
            except PolicyError as e:
                return str(e)
            if cmd.startswith("config_aggiungi"):
                if site in L.growth_sites:
                    return f"{site} è già nella lista."
                L.growth_sites.append(site)
                desc = f"sito di crescita aggiunto: {site}"
            else:
                if site not in L.growth_sites:
                    return f"{site} non è nella lista.\n\n" + self.growth_text()
                L.growth_sites.remove(site)
                desc = f"sito di crescita rimosso: {site}"
            self.limits_changed()
            self._event("edit", f"{sender}: {desc}")
            self._broadcast_others(chat_id, f"🌱 {sender}: {desc}.")
            return f"🌱 Fatto: {desc}."
        if cmd in ("config_aggiungi_app_crescita", "config_rimuovi_app_crescita"):
            if not args:
                return self._usage(cmd) + "\n\n" + self.growth_text()
            text = " ".join(args)
            if cmd.startswith("config_aggiungi"):
                key, err = self._find_app_key(text)
                if err:
                    return err
                if key in L.growth_apps:
                    return f"{self.app_label(key, '')} è già un'app di crescita."
                L.growth_apps.append(key)
                desc = f"app di crescita aggiunta: {self.app_label(key, '')} ({key})"
            else:
                t = text.lower()
                match = [k for k in L.growth_apps
                         if k == t or self.app_label(k, "").lower() == t]
                if not match:
                    return "Non è tra le app di crescita.\n\n" + self.growth_text()
                key = match[0]
                L.growth_apps.remove(key)
                desc = f"app di crescita rimossa: {self.app_label(key, '')} ({key})"
            self.limits_changed()
            self._event("edit", f"{sender}: {desc}")
            self._broadcast_others(chat_id, f"🌱 {sender}: {desc}.")
            return f"🌱 Fatto: {desc}."
        if cmd == "config_aggiorna":
            if self.on_update_request:
                self.on_update_request(chat_id)
                return "🔄 Controllo aggiornamenti in corso…"
            return "Aggiornamento non disponibile."
        return "Comando sconosciuto. /info_comandi per l'elenco."

    def growth_text(self) -> str:
        L = self.limits
        lines = ["🌱 Crescita (non conta come svago).",
                 "Siti (valgono solo se in Firefox TUTTE le schede aperte sono di questa lista):"]
        lines += [f"• {s}" for s in L.growth_sites] or ["(nessuno)"]
        lines.append("App:")
        lines += [f"• {self.app_label(k, '')} ({k})" for k in L.growth_apps] or ["(nessuna)"]
        lines.append(f"\nOggi: {fmt_min(self.state.get('growth', 0) / 60)}")
        for name, secs in sorted((self.state.get("growth_detail") or {}).items(),
                                 key=lambda kv: -kv[1]):
            if secs >= 60:
                lines.append(f"• {name}: {fmt_min(secs / 60)}")
        return "\n".join(lines)

    def limits_text(self, date: dt.date) -> str:
        dtype = self.day_type(date)
        total, apps = self.limits.day_limits(dtype, self.extras_for(date.isoformat()))
        lines = [f"Oggi ({GIORNI[date.weekday()]} {date:%d/%m}) è un giorno {dtype}."]
        et = _extra_text(self.extras_for(date.isoformat()), self.limits)
        if et:
            lines.append(et)
        lines += ["", "Limiti in vigore (concordato / massimo):"] + self.limits.summary_lines()
        return "\n".join(lines)

    def apps_text(self) -> str:
        cutoff = (dt.date.fromisoformat(self.state["date"]) - dt.timedelta(days=7)).isoformat()
        rows = []
        for key, info in sorted(self.seen.items(), key=lambda kv: kv[1].get("last", ""),
                                reverse=True):
            if info.get("last", "") < cutoff:
                continue
            aid = self.app_id_for(key, info.get("name", ""))
            name = self.limits.apps[aid].name if aid else info.get("name") or key
            secs = (self.state["apps"].get(key) or {}).get("seconds", 0)
            rows.append(f"• {name} — {key}" + (f" — oggi {fmt_min(secs / 60)}" if secs >= 60 else ""))
        if not rows:
            return "Nessuna app vista negli ultimi 7 giorni."
        return "App viste negli ultimi 7 giorni (nome — identificativo):\n" + "\n".join(rows[:40])

    def status_text(self, now: dt.datetime) -> str:
        fg = self.last_fg
        dtype = self.day_type(now.date())
        total_lim, app_lims = self.limits_now(now.date())
        lines = []
        if self.together():
            lines.append("👨‍👦 Modalità insieme ATTIVA (il tempo non conta)")
        if self.remote_locked(now):
            lines.append("⏸ Pausa ATTIVA")
        if fg:
            extra = f" — crescita ✓ ({self.last_growth})" if self.last_growth else ""
            lines.append(f"In primo piano: {fg.name}{extra}" + ("" if self.last_active else " (inattivo)"))
        else:
            lines.append("In primo piano: niente")
        lines.append(f"Giorno {dtype}. Svago oggi: {_vs(self.state['total'] / 60, total_lim)}")
        usage = self.usage_by_app()
        for aid, l in app_lims.items():
            if not l.empty or usage.get(aid):
                lines.append(f"• {self.limits.apps[aid].name}: {_vs(usage.get(aid, 0) / 60, l)}")
        if self.state.get("growth", 0) >= 60:
            lines.append(f"🌱 Crescita: {fmt_min(self.state['growth'] / 60)}")
        if self.state.get("together", 0) >= 60:
            lines.append(f"👨‍👦 Insieme: {fmt_min(self.state['together'] / 60)}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Piattaforma Linux / SteamOS
# --------------------------------------------------------------------------- #
def lz4_block_decompress(src: bytes) -> bytes:
    """Decompressione di un blocco LZ4 (formato usato da Firefox per i file .jsonlz4)."""
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off == 0 or start < 0:
            raise ValueError("dati LZ4 non validi")
        while ml > 0:
            take = min(off, ml)
            dst += dst[start:start + take]
            start += take
            ml -= take
    return bytes(dst)


def read_mozlz4(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:8] != b"mozLz40\0":
        raise ValueError("formato mozlz4 non riconosciuto")
    return lz4_block_decompress(raw[12:])


def growth_site_label(url: str, sites: list) -> str:
    """Nome da mostrare nel report per la scheda attiva: la voce della lista che corrisponde."""
    u = (url or "").strip()
    low = u.lower()
    if low.startswith("about:reader"):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).get("url")
        return growth_site_label(q[0], sites) if q else "Firefox"
    if low.startswith(("http://", "https://")):
        host = (urllib.parse.urlsplit(u).hostname or "").lower()
        for s in sites:
            if site_allowed(host, [s]):
                return s[2:] if s.startswith("*.") else s
    return "Firefox"


def firefox_url_ok(url: str, sites: list) -> bool:
    """True se l'indirizzo è consentito (siti di crescita o pagine interne di Firefox)."""
    u = (url or "").strip()
    if u.lower().startswith("view-source:"):
        u = u[12:]
    low = u.lower()
    if low.startswith("about:reader"):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).get("url")
        return bool(q) and firefox_url_ok(q[0], sites)
    if low.startswith(("about:", "chrome:")):
        return True
    if low.startswith(("http://", "https://")):
        return site_allowed(urllib.parse.urlsplit(u).hostname or "", sites)
    return False


class FirefoxSession:
    """Legge le schede aperte dal file di sessione di Firefox (aggiornato ogni ~15 s).
    Gli indirizzi vengono solo confrontati con la whitelist: non vengono salvati."""

    def __init__(self):
        home = Path.home()
        self.roots = [home / ".mozilla/firefox",
                      home / ".var/app/org.mozilla.firefox/.mozilla/firefox",
                      home / ".var/app/org.mozilla.firefox/config/mozilla/firefox",
                      home / ".config/mozilla/firefox"]
        self._cache: tuple = (None, None, None)   # (file, mtime, risultato)

    def _running_profile(self) -> Path | None:
        best, best_m = None, -1.0
        for r in self.roots:
            if not r.is_dir():
                continue
            for p in r.iterdir():
                if not (p / "prefs.js").exists() or not os.path.lexists(p / "lock"):
                    continue
                f = p / "sessionstore-backups" / "recovery.jsonlz4"
                try:
                    m = f.stat().st_mtime
                except OSError:
                    m = 0.0
                if m > best_m:
                    best, best_m = p, m
        return best

    def read(self):
        """(indirizzi di tutte le schede, numero finestre, indirizzo della scheda attiva)
        oppure None se il file non è affidabile."""
        prof = self._running_profile()
        if prof is None:
            return None
        f = prof / "sessionstore-backups" / "recovery.jsonlz4"
        try:
            m = f.stat().st_mtime
            if m < os.lstat(prof / "lock").st_mtime - 2:
                return None   # file della sessione precedente: Firefox non l'ha ancora riscritto
        except OSError:
            return None
        if self._cache[0] == f and self._cache[1] == m:
            return self._cache[2]
        try:
            js = json.loads(read_mozlz4(f))
        except (OSError, ValueError):
            return None
        urls = []
        active = ""
        windows = js.get("windows") or []
        sel_win = int(js.get("selectedWindow") or 1)
        for wi, w in enumerate(windows, 1):
            tabs = w.get("tabs") or []
            sel_tab = int(w.get("selected") or 1)
            for ti, tab in enumerate(tabs, 1):
                entries = tab.get("entries") or []
                if entries:
                    idx = int(tab.get("index") or len(entries))
                    url = entries[max(1, min(idx, len(entries))) - 1].get("url", "")
                else:
                    url = tab.get("userTypedValue") or "about:blank"
                urls.append(url)
                if wi == sel_win and ti == sel_tab:
                    active = url
        res = (urls, len(windows), active)
        self._cache = (f, m, res)
        return res


PRIVATE_TITLE = re.compile(r"private browsing|navigazione anonima|navigazione privata|anonim", re.I)


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

    def client_windows(self) -> list | None:
        v = self.get_prop(self.root, "_NET_CLIENT_LIST")
        return v if isinstance(v, list) else None

    def is_normal_window(self, win: int) -> bool:
        types = self.get_prop(win, "_NET_WM_WINDOW_TYPE")
        normal = self.atom("_NET_WM_WINDOW_TYPE_NORMAL")
        if isinstance(types, list) and types and normal not in types:
            return False
        return not self.get_prop(win, "WM_TRANSIENT_FOR")

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
    # Dal più "allarmante" al più neutro: si usa il primo presente.
    SOUNDS = ["/usr/share/sounds/ocean/stereo/alarm-clock-elapsed.oga",
              "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
              "/usr/share/sounds/ocean/stereo/phone-incoming-call.oga",
              "/usr/share/sounds/freedesktop/stereo/phone-incoming-call.oga",
              "/usr/share/sounds/ocean/stereo/dialog-warning.oga",
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

    def popup(self, text: str) -> None:
        if self._popup is not None and self._popup.poll() is None:
            return
        if shutil.which("kdialog"):
            self._popup = self._spawn(["kdialog", "--title", "DeckWatch", "--sorry", text])
        elif shutil.which("zenity"):
            self._popup = self._spawn(["zenity", "--warning", "--title", "DeckWatch", "--text", text])

    def alarm_file(self) -> str | None:
        return next((s for s in self.SOUNDS if os.path.exists(s)), None)

    def alarm(self) -> None:
        player = shutil.which("paplay") or shutil.which("pw-play")
        f = self.alarm_file()
        if player and f:
            self._spawn([player, f])


class LinuxPlatform:
    def __init__(self, on_x_io_error=None):
        self.session = SessionEnv()
        self.notifier = Notifier(self.session)
        self.input = InputActivity()
        self.steam = SteamInfo()
        self.firefox = FirefoxSession()
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

    def alarm(self) -> None:
        self.notifier.alarm()

    def steam_find(self, text: str) -> list:
        self.steam.name("")
        t = text.lower().strip()
        found = [(a, n) for a, n in self.steam.names.items() if t in n.lower()]
        exact = [x for x in found if x[1].lower() == t]
        return exact or found

    def firefox_growth(self, sites: list) -> str | None:
        """Nome del sito della scheda attiva se TUTTE le schede di TUTTE le finestre di
        Firefox sono consentite e non ci sono finestre che la sessione non conosce (es.
        anonime); altrimenti None (il tempo conta come svago)."""
        res = self.firefox.read()
        if not res:
            return None
        urls, n_windows, active = res
        if not urls or not all(firefox_url_ok(u, sites) for u in urls):
            return None
        x = self.x
        if x and x.dpy:
            wins = x.client_windows()
            if wins is None:
                return None
            ff = 0
            for w in wins:
                wi = x.window_info(w)
                if f"class:{(wi.cls or '').lower()}" not in FIREFOX_KEYS:
                    continue
                if PRIVATE_TITLE.search(wi.title or ""):
                    return None
                if x.is_normal_window(w):
                    ff += 1
            if ff > n_windows:
                return None
        return growth_site_label(active, sites)

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
# File di regole: modello di policy.toml, archivio di limiti.toml
# --------------------------------------------------------------------------- #
def render_policy(v: dict | None = None) -> str:
    v = dict(v or {})
    q = lambda s: json.dumps(s, ensure_ascii=False)
    lst = lambda xs: "[\n" + "".join(f"  {q(x)},\n" for x in xs) + "]"
    nums = lambda xs: "[" + ", ".join(f"{x:g}" for x in xs) + "]"
    g = lambda k, d: v.get(k, d)
    return f'''# =====================================================================
#  DeckWatch — IMPOSTAZIONI (policy.toml)
#  Percorso sulla Deck: ~/.config/deckwatch/policy.toml
#  Lo modificano solo i genitori, a mano; viene ricaricato da solo.
#  I LIMITI (tempi per giorno e per app, siti e app di crescita) NON stanno qui:
#  si gestiscono da Telegram e vengono salvati in limiti.toml.
#  Tempi in minuti, salvo dove indicato "secondi".
# =====================================================================

# "monitor" = misura, avvisa e manda i report, ma NON chiude nulla per i limiti:
#             nel report dei genitori compare cosa sarebbe stato chiuso.
#             La pausa da Telegram (/timer_pausa) funziona comunque.
# "enforce" = al limite massimo l'app si chiude.
mode = {q(g("mode", "monitor"))}

# Minuti senza toccare nulla dopo i quali il tempo smette di contare.
idle_minutes = {g("idle_minutes", 2):g}

# Se da Telegram si indica solo il tempo concordato, il massimo è concordato + questo.
default_hard_margin = {g("default_hard_margin", 5):g}

# Avvisi "mancano N minuti" prima del tempo concordato e del massimo (vuoto = nessuno).
countdown_soft = {nums(g("countdown_soft", []))}
countdown_hard = {nums(g("countdown_hard", []))}

# Pausa da Telegram: messaggio e secondi per salvare prima che le app si chiudano.
lock_message = {q(g("lock_message", "Mamma e papà hanno messo in pausa la Deck."))}
lock_grace_seconds = {g("lock_grace_seconds", 60):g}

# Se riapre un'app bloccata: avviso e chiusura dopo questi secondi (enforce e pausa).
relaunch_seconds = {g("relaunch_seconds", 20):g}

# Avviso ai genitori quando un'app mai vista resta in primo piano per questi minuti.
new_app_minutes = {g("new_app_minutes", 5):g}

# Gioco insieme: primo promemoria dopo questi minuti, poi ogni N minuti su Telegram.
together_reminder_minutes = {g("together_reminder_minutes", 90):g}
together_repeat_minutes = {g("together_repeat_minutes", 30):g}

# Avvisi ai genitori quando un'app viene chiusa.
alerts = {str(bool(g("alerts", True))).lower()}

# Cosa fare quando finisce il tempo massimo di oggi (solo enforce):
# "kill" = chiude le app di svago; "shutdown" = spegne la Deck.
on_total_exceeded = {q(g("on_total_exceeded", "kill"))}

# App che NON contano nel tempo (e non vengono mai chiuse).
# Il nome di un'app si vede con:  deckwatch fg --watch
exclude = {lst(g("exclude", DEFAULT_EXCLUDE))}

# App che restano usabili anche in pausa o a tempo finito (contano comunque nel tempo).
always_allowed = {lst(g("always_allowed", DEFAULT_ALLOWED))}
'''


def migrate_policy_values(raw: dict) -> dict:
    keep = {k: raw[k] for k in Policy.KEYS if k in raw}
    if raw.get("idle_minutes") == 3:   # vecchio valore predefinito
        keep["idle_minutes"] = 2
    g = raw.get("grace") or {}
    if "relaunch_seconds" in g:
        keep["relaunch_seconds"] = g["relaunch_seconds"]
    if "on_total_exceeded" in g:
        keep["on_total_exceeded"] = g["on_total_exceeded"]
    return keep


class LimitsStore:
    def __init__(self, path: Path, meta: JsonFile):
        self.path, self.meta = path, meta
        self.mtime = 0.0

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def save(self, L: Limits) -> None:
        text = L.to_toml()
        atomic_write(self.path, text)
        self.meta.set("limits_sha", self._sha(text))
        self.mtime = self.path.stat().st_mtime

    def current_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def is_own(self, text: str) -> bool:
        return self._sha(text) == self.meta.get("limits_sha")


# --------------------------------------------------------------------------- #
# Servizio systemd e avviatore (ritorno automatico alla versione precedente)
# --------------------------------------------------------------------------- #
INSTALL_DIR = Path.home() / ".local" / "share" / APP
UNIT_FILE = Path.home() / ".config" / "systemd" / "user" / f"{APP}.service"

LAUNCHER_CODE = '''#!/usr/bin/env python3
# DeckWatch — avviatore. Avvia deckwatch.py; se una versione appena installata va in
# crash ripetutamente nei primi 10 minuti, ripristina quella precedente.
import json, os, runpy, sys, time
from pathlib import Path

D = Path(__file__).resolve().parent
S = Path(os.environ.get("DECKWATCH_STATE_DIR") or
         Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "deckwatch")
U = S / "update.json"
try:
    info = json.loads(U.read_text())
except Exception:
    info = {}
if info.get("pending"):
    if time.time() - info.get("installed_at", 0) < 600:
        info["starts"] = info.get("starts", 0) + 1
        prev = D / "deckwatch.py.prev"
        if info["starts"] >= 4 and prev.exists():
            os.replace(prev, D / "deckwatch.py")
            info = {"rolled_back": info.get("version"), "at": time.time()}
    else:
        info["pending"] = False
    try:
        S.mkdir(parents=True, exist_ok=True)
        U.write_text(json.dumps(info))
    except Exception:
        pass
sys.argv = [str(D / "deckwatch.py"), "run"]
runpy.run_path(str(D / "deckwatch.py"), run_name="__main__")
'''


def unit_text() -> str:
    return f"""[Unit]
Description=DeckWatch - monitor del tempo di gioco
After=graphical-session.target

[Service]
ExecStart=/usr/bin/env python3 {INSTALL_DIR}/launcher.py
Restart=always
RestartSec=5
Nice=19
IOSchedulingClass=idle

[Install]
WantedBy=default.target
"""


def ensure_service_files() -> bool:
    """Scrive avviatore e unità systemd se mancano o sono vecchi. True se l'unità è cambiata."""
    launcher = INSTALL_DIR / "launcher.py"
    try:
        if not launcher.exists() or launcher.read_text() != LAUNCHER_CODE:
            atomic_write(launcher, LAUNCHER_CODE, 0o755)
        if not UNIT_FILE.exists() or UNIT_FILE.read_text() != unit_text():
            atomic_write(UNIT_FILE, unit_text())
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except OSError:
        log.exception("Impossibile aggiornare i file del servizio")
    return False


def fetch_remote_version(cfg: Config) -> str:
    return http_request(f"{cfg.update_url.rstrip('/')}/VERSION").decode().strip()


def install_version(cfg: Config, version: str, paths: Paths) -> None:
    target = INSTALL_DIR / "deckwatch.py"
    code = http_request(f"{cfg.update_url.rstrip('/')}/deckwatch.py").decode("utf-8")
    compile(code, "deckwatch.py", "exec")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', code, re.M)
    if not m or m.group(1) != version:
        raise RuntimeError("versione nel file scaricato non coerente")
    if target.exists():
        shutil.copy2(target, INSTALL_DIR / "deckwatch.py.prev")
    atomic_write(target, code, 0o755)
    atomic_write(paths.update_file, json.dumps({"pending": True, "version": version,
                                                "installed_at": time.time(), "starts": 0}))


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def journal_last_monotonic(boot: str) -> float | None:
    """Secondi di accensione (sospensioni escluse) dell'ultima voce del registro di quell'avvio."""
    bid = boot.replace("-", "")
    for extra in ([], ["--user"]):
        try:
            r = subprocess.run(["journalctl", *extra, "-b", bid, "-n", "1", "-o", "json",
                                "--no-pager"], capture_output=True, text=True, timeout=15)
            line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
            if line:
                return int(json.loads(line)["__MONOTONIC_TIMESTAMP"]) / 1e6
        except (OSError, ValueError, KeyError, subprocess.SubprocessError, IndexError):
            continue
    return None


# --------------------------------------------------------------------------- #
# Demone
# --------------------------------------------------------------------------- #
class Daemon:
    def __init__(self, paths: Paths, cfg: Config):
        self.paths, self.cfg = paths, cfg
        self.stop = threading.Event()
        self.commands: queue.Queue = queue.Queue()
        self.meta = JsonFile(paths.meta_file)
        self.platform = LinuxPlatform(on_x_io_error=self._emergency_save)
        self.outbox = Outbox(paths.outbox_dir, cfg)
        self.limits_store = LimitsStore(paths.limits_file, self.meta)
        self._update_event = threading.Event()
        self._update_chat = None
        self._last_policy_err = None
        self._last_limits_err = None
        try:
            policy = load_policy_file(paths.policy_file)
        except Exception as e:
            log.error("policy.toml non valido (%s): uso i valori predefiniti", e)
            self.outbox.to_parents(f"⚠️ policy.toml non valido: {e}\nUso i valori predefiniti.")
            policy = Policy({})
        limits = self._initial_limits(policy)
        if policy.legacy:
            self._migrate_policy(policy)
            policy = load_policy_file(paths.policy_file)
        self._policy_mtime = self._mtime(paths.policy_file)
        self.engine = Engine(self.platform, DayStore(paths.days_dir), self.outbox, cfg, policy,
                             limits, JsonFile(paths.overrides_file), self.meta,
                             save_limits=self.limits_store.save)
        self.engine.on_update_request = self._request_update

    # -- regole ------------------------------------------------------------ #
    @staticmethod
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    def _initial_limits(self, policy: Policy) -> Limits:
        p = self.paths.limits_file
        if p.exists():
            try:
                L = Limits(load_toml_text(self.limits_store.read()), policy.default_hard_margin)
                self.limits_store.mtime = self._mtime(p)
                return L
            except Exception as e:
                log.error("limiti.toml non valido: %s", e)
                self.outbox.to_parents(f"⚠️ limiti.toml non valido: {e}\n"
                                       "Finché non viene corretto non ci sono limiti attivi.")
                self.limits_store.mtime = self._mtime(p)
                return Limits({"crescita": {"siti": DEFAULT_GROWTH_SITES}}, policy.default_hard_margin)
        if policy.legacy:
            L = migrate_legacy_limits(policy.raw, policy.default_hard_margin)
            msg = "📦 Ho spostato i limiti da policy.toml al nuovo file limiti.toml."
        else:
            L = Limits({}, policy.default_hard_margin)
            msg = None
        self.limits_store.save(L)
        if msg:
            self.outbox.to_parents(msg + "\n\n" + "\n".join(L.summary_lines()))
        return L

    def _migrate_policy(self, policy: Policy) -> None:
        p = self.paths.policy_file
        try:
            shutil.copy2(p, p.with_name("policy.toml.bak"))
            atomic_write(p, render_policy(migrate_policy_values(policy.raw)))
            log.info("policy.toml convertito al nuovo formato (copia in policy.toml.bak)")
        except OSError:
            log.exception("Conversione di policy.toml non riuscita")

    def _reload_rules(self) -> None:
        eng = self.engine
        m = self._mtime(self.paths.policy_file)
        if m != self._policy_mtime:
            self._policy_mtime = m
            try:
                pol = load_policy_file(self.paths.policy_file)
                eng.policy = pol
                self._last_policy_err = None
                self.limits_store.mtime = 0.0   # ricalcolo i limiti con il nuovo margine
                eng._event("edit", "policy.toml modificato")
                self.outbox.to_parents(f"✅ policy.toml ricaricato (modalità {pol.mode}).")
            except Exception as e:
                if str(e) != self._last_policy_err:
                    self._last_policy_err = str(e)
                    self.outbox.to_parents(f"⚠️ policy.toml non valido, continuo con la "
                                           f"versione precedente.\nErrore: {e}")
        m = self.limits_store.current_mtime()
        if m and m != self.limits_store.mtime:
            self.limits_store.mtime = m
            try:
                text = self.limits_store.read()
                new = Limits(load_toml_text(text), eng.policy.default_hard_margin)
            except Exception as e:
                if str(e) != self._last_limits_err:
                    self._last_limits_err = str(e)
                    self.outbox.to_parents(f"⚠️ limiti.toml non valido, continuo con la "
                                           f"versione precedente.\nErrore: {e}")
                return
            self._last_limits_err = None
            if not self.limits_store.is_own(text):
                diff = limits_diff(eng.limits, new)
                if diff:
                    eng._event("edit", "limiti.toml modificato a mano: " + "; ".join(diff))
            eng.limits = new
            eng._map_cache = (None, {})

    # -- anti-manomissione ------------------------------------------------- #
    def _tamper_check(self) -> None:
        hb = JsonFile(self.paths.heartbeat_file)
        prev_boot, prev_mono, prev_wall = hb.get("boot"), hb.get("mono"), hb.get("wall")
        cur = boot_id()
        mono = time.monotonic()
        msgs = []
        if prev_boot and prev_boot == cur and prev_mono is not None:
            gap = mono - prev_mono
            if gap > 180:
                hhmm = dt.datetime.fromtimestamp(prev_wall).strftime("%H:%M") if prev_wall else "?"
                msgs.append(f"⚠️ DeckWatch è rimasto fermo per {fmt_min(gap / 60)} mentre la Deck "
                            f"era accesa (ultimo segnale alle {hhmm}).")
        elif cur and prev_boot:
            if mono > 300:
                msgs.append(f"⚠️ DeckWatch è partito {fmt_min(mono / 60)} dopo l'accensione "
                            "della Deck.")
            if prev_mono is not None:
                last = journal_last_monotonic(prev_boot)
                if last and last - prev_mono > 300:
                    msgs.append(f"⚠️ Nell'accensione precedente la Deck è rimasta accesa "
                                f"{fmt_min(last / 60)}, ma DeckWatch si è fermato "
                                f"{fmt_min((last - prev_mono) / 60)} prima dello spegnimento.")
        for m in msgs:
            log.warning(m)
            self.outbox.to_parents(m, "DeckWatch: avviso")
            self.engine._event("alert", m)
        self._heartbeat()

    def _heartbeat(self) -> None:
        try:
            atomic_write(self.paths.heartbeat_file, json.dumps(
                {"boot": boot_id(), "mono": time.monotonic(), "wall": time.time()}))
        except OSError:
            pass

    def _check_rollback(self) -> None:
        try:
            info = json.loads(self.paths.update_file.read_text())
        except (OSError, ValueError):
            return
        if info.get("rolled_back"):
            v = info["rolled_back"]
            self.meta.set("bad_version", v)
            self.outbox.to_parents(f"⚠️ La versione {v} non funzionava: sono tornato alla "
                                   f"{__version__}.")
            atomic_write(self.paths.update_file, json.dumps({}))

    def _emergency_save(self) -> None:
        try:
            self.engine.save()
        except Exception:
            pass

    # -- thread ------------------------------------------------------------ #
    def _outbox_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.outbox.flush()
            except Exception:
                log.exception("outbox")
            self.outbox.wake.wait(60)
            self.outbox.wake.clear()

    def _register_bot(self) -> None:
        cmds = [{"command": c, "description": d[:256]} for c, d, _ in COMMANDS]
        desc = self.cfg.tg_description
        sig = hashlib.sha256(json.dumps([cmds, desc, self.cfg.tg_parents]).encode()).hexdigest()
        if self.meta.get("bot_sig") == sig:
            return
        tok = self.cfg.tg_token
        telegram_call(tok, "deleteMyCommands", {})
        for c in self.cfg.tg_parents:
            telegram_call(tok, "setMyCommands", {"commands": cmds,
                                                 "scope": {"type": "chat", "chat_id": c}})
        telegram_call(tok, "setMyDescription", {"description": desc})
        telegram_call(tok, "setMyShortDescription", {"short_description": desc[:120]})
        self.meta.set("bot_sig", sig)

    def _telegram_loop(self) -> None:
        if not self.cfg.telegram_enabled:
            return
        offset = self.meta.get("tg_offset")
        registered = False
        while not self.stop.is_set():
            try:
                if not registered:
                    self._register_bot()
                    registered = True
                data = {"timeout": 25, "allowed_updates": ["message"]}
                if offset is not None:
                    data["offset"] = offset
                updates = telegram_call(self.cfg.tg_token, "getUpdates", data, timeout=40)
                self.engine.online = True
                self._check_clock()
            except Exception as e:
                self.engine.online = False
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

    def _check_clock(self) -> None:
        srv = HTTP_CLOCK.get("server")
        if srv is None:
            return
        drift = HTTP_CLOCK["local"] - srv
        if abs(drift) > 300:
            key = f"clock|{self.engine.state['date']}"
            if not self.meta.get(key):
                self.meta.set(key, True)
                m = (f"⚠️ L'orologio della Deck è sfasato di {fmt_min(abs(drift) / 60)} "
                     f"({'avanti' if drift > 0 else 'indietro'}) rispetto all'ora esatta.")
                self.outbox.to_parents(m, "DeckWatch: avviso")
                self.engine._event("alert", m)

    def _request_update(self, chat) -> None:
        self._update_chat = chat
        self._update_event.set()

    def _safe_to_restart(self) -> bool:
        e = self.engine
        fg = e.last_fg
        return (fg is None or not e.last_active or fg.key in FIREFOX_KEYS
                or e.policy.is_allowed(fg.key, fg.name))

    def _reply_update(self, chat, text: str) -> None:
        if chat:
            self.outbox.enqueue("telegram", str(chat), "DeckWatch", text)

    def _update_loop(self) -> None:
        if not self.cfg.update_url:
            return
        self._update_event.wait(300)
        if self.stop.is_set():
            return
        while not self.stop.is_set():
            manual = self._update_event.is_set()
            chat = self._update_chat if manual else None
            self._update_event.clear()
            last = self.meta.get("last_update_check", 0)
            if manual or (self.cfg.auto_update and time.time() - last >= 86400):
                try:
                    if INSTALL_DIR.resolve() != Path(__file__).resolve().parent:
                        raise RuntimeError("DeckWatch non è avviato dall'installazione standard")
                    remote = fetch_remote_version(self.cfg)
                    self.meta.set("last_update_check", time.time())
                    newer = parse_version(remote) > parse_version(__version__)
                    bad = self.meta.get("bad_version") == remote
                    if not newer or (bad and not manual):
                        res = f"nessuna novità (remota {remote})" + (" — scartata, non funzionava"
                                                                      if bad else "")
                        self.meta.set("last_update_result", res)
                        self._reply_update(chat, f"Nessun aggiornamento: la versione "
                                                 f"installata è {__version__}.")
                    else:
                        while not manual and not self._safe_to_restart() and not self.stop.is_set():
                            if self._update_event.wait(600):
                                break   # richiesta manuale arrivata nel frattempo
                        install_version(self.cfg, remote, self.paths)
                        self.meta.set("last_update_result", f"installata {remote}")
                        self.outbox.to_parents(f"🔄 DeckWatch aggiornato alla versione {remote}.")
                        log.info("Aggiornato a %s: riavvio", remote)
                        self.stop.set()
                        return
                except Exception as e:
                    self.meta.set("last_update_result", f"errore: {e}")
                    log.warning("Aggiornamento non riuscito: %s", e)
                    self._reply_update(chat, f"⚠️ Aggiornamento non riuscito: {e}")
            self._update_event.wait(3600)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        log.info("DeckWatch %s avviato", __version__)
        self.platform.refresh(force=True)
        self.engine.start(dt.datetime.now())
        self._check_rollback()
        self._tamper_check()
        for fn in (self._outbox_loop, self._telegram_loop, self._update_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
        last = time.monotonic()
        last_reload = last_hb = 0.0
        while not self.stop.is_set():
            now_m = time.monotonic()
            delta = now_m - last
            last = now_m
            if delta > TICK * 3:
                delta = TICK
            try:
                if now_m - last_reload > 15:
                    last_reload = now_m
                    self._reload_rules()
                if now_m - last_hb > 60:
                    last_hb = now_m
                    self._heartbeat()
                self.platform.refresh()
                while not self.commands.empty():
                    chat, sender, text, ts = self.commands.get_nowait()
                    reply = self.engine.handle_command(chat, sender, text, ts, dt.datetime.now())
                    if reply:
                        self.outbox.enqueue("telegram", str(chat), "DeckWatch", reply)
                self.engine.tick(dt.datetime.now(), delta)
            except Exception:
                log.exception("Errore nel ciclo principale")
            end = time.monotonic() + TICK
            while not self.stop.is_set() and time.monotonic() < end:
                self.stop.wait(1.0)
                self.platform.input.poll()
                if not self.commands.empty():
                    break
        self.engine.save()
        self._heartbeat()
        log.info("DeckWatch fermato")


# --------------------------------------------------------------------------- #
# Riga di comando
# --------------------------------------------------------------------------- #
def _load_rules(paths: Paths) -> tuple[Policy, Limits]:
    pol = load_policy_file(paths.policy_file)
    if paths.limits_file.exists():
        L = Limits(load_toml_text(paths.limits_file.read_text(encoding="utf-8")),
                   pol.default_hard_margin)
    elif pol.legacy:
        L = migrate_legacy_limits(pol.raw, pol.default_hard_margin)
    else:
        L = Limits({}, pol.default_hard_margin)
    return pol, L


def cmd_run(paths: Paths, args) -> int:
    if Path(__file__).resolve().parent == INSTALL_DIR.resolve():
        ensure_service_files()
    Daemon(paths, Config.load(paths.config_file)).run()
    return 0


def cmd_install_service(paths: Paths, args) -> int:
    ensure_service_files()
    print(f"Servizio pronto: {UNIT_FILE}")
    return 0


def cmd_status(paths: Paths, args) -> int:
    pol, L = _load_rules(paths)
    st = DayStore(paths.days_dir).load(dt.date.today().isoformat()) or new_day(dt.date.today())
    ov = JsonFile(paths.overrides_file)
    extras = (ov.get("extras") or {}).get(st["date"], {})
    print(build_report(st, L, "parents", extras, dt.datetime.now().strftime("%H:%M"))[1])
    if ov.get("lock") or (ov.get("lock_until") or 0) > time.time():
        print("\n⏸ Pausa attiva")
    if ov.get("together"):
        print("\n👨‍👦 Modalità insieme attiva")
    return 0


def cmd_check(paths: Paths, args) -> int:
    try:
        pol = load_policy_file(paths.policy_file)
        print(f"✅ policy.toml valido (modalità {pol.mode}, inattività {pol.idle_minutes:g} min).")
    except Exception as e:
        print(f"❌ policy.toml: {e}")
        return 1
    try:
        _, L = _load_rules(paths)
        print("✅ limiti.toml valido.\n")
    except Exception as e:
        print(f"❌ limiti.toml: {e}")
        return 1
    print("\n".join(L.summary_lines()))
    print("\nCrescita — siti: " + ", ".join(L.growth_sites))
    print("Crescita — app: " + (", ".join(L.growth_apps) or "nessuna"))
    return 0


def cmd_fg(paths: Paths, args) -> int:
    p = LinuxPlatform()
    p.refresh(force=True)
    print(f"Modalità: {p.session.mode}  DISPLAY={p.session.env.get('DISPLAY')}  "
          f"input leggibili: {len(p.input.fds)}")
    _, L = _load_rules(paths)
    try:
        while True:
            fg = p.foreground()
            idle = p.idle_seconds()
            idle_s = "?" if idle is None else f"{idle:.0f}s"
            if fg:
                extra = ""
                if L.is_growth_app(fg.key, fg.name):
                    extra = "  crescita=sì (app)"
                elif fg.key in FIREFOX_KEYS:
                    g = p.firefox_growth(L.growth_sites)
                    extra = f"  crescita={'sì (' + g + ')' if g else 'no'}"
                print(f"{fg.key:<30} {fg.name!r:<28} inattivo da {idle_s}{extra}")
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
    _, L = _load_rules(paths)
    date = args.date or dt.date.today().isoformat()
    st = DayStore(paths.days_dir).load(date)
    if not st:
        print(f"Nessun dato per {date}")
        return 1
    extras = (JsonFile(paths.overrides_file).get("extras") or {}).get(date, {})
    for aud in ("parents", "kid"):
        subj, body = build_report(st, L, aud, extras)
        print(f"=== {aud}: {subj}\n{body}\n")
    if args.send:
        ob = Outbox(paths.outbox_dir, cfg)
        subj, body = build_report(st, L, "parents", extras)
        ob.to_parents(body, subj)
        subj, body = build_report(st, L, "kid", extras)
        ob.to_kid(body, subj)
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


def cmd_test_notifications(paths: Paths, args) -> int:
    p = LinuxPlatform()
    p.refresh(force=True)
    print(f"Modalità: {p.session.mode}. Suono di allarme: {p.notifier.alarm_file() or 'nessuno trovato'}")
    print("Metti un gioco a tutto schermo: i test partono tra 15 secondi.")
    for i in range(15, 0, -1):
        print(f"  {i}…", end="\r", flush=True)
        time.sleep(1)
    steps = [
        ("1) notifica normale", lambda: p.notify("DeckWatch — prova 1", "Notifica normale", False)),
        ("2) notifica critica", lambda: p.notify("DeckWatch — prova 2", "Notifica critica", True)),
        ("3) popup", lambda: p.popup("DeckWatch — prova 3\n\nPopup: premi OK per chiudere")),
        ("4) suono di allarme", p.alarm),
        ("5) notifica critica senza suono (come al tempo concordato)",
         lambda: p.notify("⏰ Tempo concordato terminato", "Prova: chiudi appena possibile.", True)),
    ]
    for label, fn in steps:
        print(label)
        fn()
        time.sleep(6)
    print("Fine. Cosa hai visto o sentito per ciascun passo? Il gioco si è interrotto?")
    return 0


def cmd_debug_firefox(paths: Paths, args) -> int:
    """Diagnosi dei siti di crescita: mostra solo i domini delle schede, non gli indirizzi."""
    _, L = _load_rules(paths)
    p = LinuxPlatform()
    p.refresh(force=True)
    fs = p.firefox
    print("Profili Firefox trovati:")
    found = False
    for r in fs.roots:
        if not r.is_dir():
            continue
        for prof in sorted(r.iterdir()):
            if not (prof / "prefs.js").exists():
                continue
            found = True
            f = prof / "sessionstore-backups" / "recovery.jsonlz4"
            lock = os.path.lexists(prof / "lock")
            try:
                age = f"aggiornata {time.time() - f.stat().st_mtime:.0f} s fa"
            except OSError:
                age = "file di sessione assente"
            print(f"  {prof}\n     in uso (lock): {'sì' if lock else 'no'} — sessione {age}")
    if not found:
        print("  nessuno (cartelle cercate: " + ", ".join(str(r) for r in fs.roots) + ")")
    prof = fs._running_profile()
    print(f"Profilo scelto: {prof}")
    res = fs.read()
    if not res:
        print("Sessione non leggibile o vecchia → crescita=no")
        return 0
    urls, nwin, active = res
    print(f"Finestre nella sessione: {nwin}, schede: {len(urls)}")
    for i, u in enumerate(urls, 1):
        host = urllib.parse.urlsplit(u).hostname or u.split("?")[0][:40]
        print(f"  scheda {i}: {host} → {'consentito' if firefox_url_ok(u, L.growth_sites) else 'NON consentito'}"
              + ("  ← attiva" if u == active else ""))
    x = p.x
    if x and x.dpy:
        wins = x.client_windows()
        if wins is None:
            print("Elenco finestre dello schermo non disponibile → crescita=no")
        else:
            for w in wins:
                wi = x.window_info(w)
                if f"class:{(wi.cls or '').lower()}" in FIREFOX_KEYS:
                    print(f"  finestra Firefox: normale={'sì' if x.is_normal_window(w) else 'no'} "
                          f"anonima={'sì' if PRIVATE_TITLE.search(wi.title or '') else 'no'}")
    else:
        print("Display non disponibile: controllo finestre saltato")
    g = p.firefox_growth(L.growth_sites)
    print(f"Risultato: crescita={'sì (' + g + ')' if g else 'no'}")
    return 0


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
    if Path(__file__).resolve().parent != INSTALL_DIR.resolve():
        print(f"L'aggiornamento funziona solo per l'installazione in {INSTALL_DIR}")
        return 1
    remote = fetch_remote_version(cfg)
    if parse_version(remote) <= parse_version(__version__):
        print(f"Nessun aggiornamento (versione attuale {__version__}).")
        return 0
    install_version(cfg, remote, paths)
    print(f"Aggiornato alla versione {remote}. Riavvio servizio...")
    subprocess.run(["systemctl", "--user", "restart", APP], check=False)
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog=APP, description="Monitor del tempo di gioco per Steam Deck")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="avvia il servizio (usato da systemd)")
    sub.add_parser("install-service", help="scrive avviatore e servizio systemd")
    sub.add_parser("status", help="mostra il tempo di oggi")
    sub.add_parser("check", help="verifica policy.toml e limiti.toml")
    p = sub.add_parser("fg", help="mostra l'app in primo piano (identificativi per le regole)")
    p.add_argument("--watch", action="store_true")
    sub.add_parser("games", help="elenca i giochi Steam installati con il loro appid")
    p = sub.add_parser("report", help="mostra (e opzionalmente invia) il report di un giorno")
    p.add_argument("--date")
    p.add_argument("--send", action="store_true")
    sub.add_parser("test", help="invia messaggi di prova (Telegram ed email)")
    sub.add_parser("test-notifiche", help="prova notifiche, popup e suono sulla Deck")
    sub.add_parser("debug-firefox", help="diagnosi dei siti di crescita su Firefox")
    p = sub.add_parser("telegram-setup", help="trova i chat id dei genitori")
    p.add_argument("--save", action="store_true")
    sub.add_parser("update", help="aggiorna DeckWatch ora")
    args = ap.parse_args(argv)
    paths = Paths.default()
    fn = {"run": cmd_run, "install-service": cmd_install_service, "status": cmd_status,
          "check": cmd_check, "fg": cmd_fg, "games": cmd_games, "report": cmd_report,
          "test": cmd_test, "test-notifiche": cmd_test_notifications,
          "debug-firefox": cmd_debug_firefox,
          "telegram-setup": cmd_telegram_setup, "update": cmd_update}[args.cmd]
    return fn(paths, args)


if __name__ == "__main__":
    sys.exit(main())
