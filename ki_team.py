#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KI-Team - mehrere KIs (ueber OpenRouter) planen, bauen und debuggen gemeinsam
an einem Projekt in einem Ordner. Die KIs verstaendigen sich ueber CHAT.md,
PLAN.md und tasks.json im Projektordner.

Nur Python-Standardbibliothek (+ tkinter). Start:  python3 ki_team.py
"""
import datetime
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, font as tkfont
except ImportError:  # ohne GUI (z. B. Engine-Test) trotzdem importierbar
    tk = None

APP_NAME = "KI-Team"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "ki-team")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
API_BASE = "https://openrouter.ai/api/v1"
NUM_SLOTS = 4
NUM_KEYS = 3
RESERVED = {"tasks.json", "chat.md", "plan.md"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv"}
MAX_FILE_BYTES = 400000


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------
def default_config():
    return {
        "keys": [""] * NUM_KEYS,
        "save_keys": True,
        # Erstbelegung passend zur vom Benutzer gezeigten KI-Aufstellung.
        # Sobald eine config.json existiert, haben bereits gespeicherte Werte Vorrang.
        "slots": [
            {"name": "KI 1", "model": "nvidia/nemotron-3.5-super-vl:free", "key": 0, "role": ""},
            {"name": "KI 2", "model": "nvidia/nemotron-3-ultra:free", "key": 1, "role": ""},
            {"name": "KI 3", "model": "baidu/ernie-5.5-flash:free", "key": 2, "role": ""},
            {"name": "KI 4", "model": "thinkingmachines/inkling-2-small:free", "key": 2, "role": ""},
        ],
        "planner": 0,
        "builders": [0, 1, 2, 3],
        "debuggers": [1],
        "folder": "",
        "task": "",
        "run_cmd": "",
        "do_plan": True,
        "do_build": True,
        "do_debug": True,
        "parallel": False,
        "only_free": True,
        "pause": 2.0,
        "max_calls": 150,
        "debug_rounds": 4,
        "run_timeout": 30,
        "planning_mode": "guided",  # guided: ask user; auto: AI decides
        "team_meeting": True,
        "dark_mode": False,
    }


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    for k, v in data.items():
        if k == "slots" and isinstance(v, list):
            for i, s in enumerate(v[:NUM_SLOTS]):
                if isinstance(s, dict):
                    cfg["slots"][i].update(s)
        elif k == "keys" and isinstance(v, list):
            for i, s in enumerate(v[:NUM_KEYS]):
                cfg["keys"][i] = str(s)
        elif k in cfg:
            cfg[k] = v
    return cfg


def save_config(cfg):
    """Speichert die gesamte Einrichtung atomar und mit privaten Dateirechten."""
    data = json.loads(json.dumps(cfg))
    if not cfg.get("save_keys", True):
        # API-Keys werden nur gespeichert, wenn der Benutzer das aktiviert.
        # Modell-Auswahl, Rollen und alle übrigen Einstellungen bleiben erhalten.
        data["keys"] = [""] * NUM_KEYS
    os.makedirs(CONFIG_DIR, exist_ok=True)
    temp_file = CONFIG_FILE + ".tmp"
    fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, CONFIG_FILE)
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temp_file)
        except OSError:
            pass
        raise


def normalize_api_key(value):
    """Normalisiert typische Copy/Paste-Varianten eines OpenRouter-Keys.

    Im Eingabefeld soll nur der Key stehen. Falls versehentlich auch
    'Bearer ' oder 'Authorization:' mitkopiert wurde, entfernen wir das.
    """
    key = str(value or "").strip().strip("\"'")
    if key.lower().startswith("authorization:"):
        key = key.split(":", 1)[1].strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    key = key.strip().strip("\"'")
    if not key:
        return ""
    if any(ch.isspace() for ch in key):
        raise ApiError("Der API-Key enthält innere Leerzeichen oder Zeilenumbrüche. Bitte den Key erneut ohne Zusatztext einfügen.")
    if not key.startswith("sk-or-"):
        raise ApiError("Das sieht nicht wie ein OpenRouter-API-Key aus. Erwartet wird ein Key, der mit sk-or- beginnt.")
    return key


def slot_key(cfg, i):
    try:
        k = int(cfg["slots"][i].get("key", 0))
    except (TypeError, ValueError):
        k = 0
    k = min(max(k, 0), NUM_KEYS - 1)
    return normalize_api_key(cfg["keys"][k])


def slot_name(cfg, i):
    return (cfg["slots"][i].get("name") or "KI %d" % (i + 1)).strip()


def slot_ready(cfg, i):
    try:
        return bool(str(cfg["slots"][i].get("model", "")).strip() and slot_key(cfg, i))
    except ApiError:
        return False


def validate_cfg(cfg):
    """Gibt eine Fehlermeldung zurueck oder None, wenn alles passt."""
    if not (cfg["do_plan"] or cfg["do_build"] or cfg["do_debug"]):
        return "Wähle mindestens eine Phase aus (Planen, Bauen oder Debuggen)."
    if not cfg["folder"]:
        return "Bitte wähle einen Projektordner."
    if cfg["do_plan"]:
        if not cfg["task"]:
            return "Bitte beschreibe, was gebaut werden soll."
        p = cfg["planner"]
        if not slot_ready(cfg, p):
            return "Die Planer-KI (%s) braucht ein Modell und einen Key mit Inhalt." % slot_name(cfg, p)

    def check(group, label):
        sel = list(group)
        if not sel:
            return "Für „%s“ ist keine KI ausgewählt." % label
        for i in sel:
            try:
                slot_key(cfg, i)
            except ApiError as exc:
                return "API-Key von %s: %s" % (slot_name(cfg, i), exc)
        bad = [slot_name(cfg, i) for i in sel if not slot_ready(cfg, i)]
        if bad:
            return "%s: Modell oder Key fehlt bei %s. Trage es ein oder nimm die KI aus der Auswahl." % (
                label, ", ".join(bad))
        return None

    if cfg["do_build"]:
        err = check(cfg["builders"], "Bauen")
        if err:
            return err
    if cfg["do_debug"]:
        err = check(cfg["debuggers"], "Debuggen")
        if err:
            return err
    if not cfg["do_plan"] and cfg["do_build"]:
        if not os.path.exists(os.path.join(cfg["folder"], "tasks.json")):
            return "Im Projektordner gibt es noch keine tasks.json. Lass zuerst die Phase „Planen“ laufen."
    return None


# --------------------------------------------------------------------------
# OpenRouter-Client (nur Standardbibliothek)
# --------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, msg, status=None, transient=False):
        super().__init__(msg)
        self.status = status
        self.transient = transient


def _error_detail(raw):
    try:
        data = json.loads(raw)
        err = data.get("error", data)
        if isinstance(err, dict):
            msg = err.get("message") or json.dumps(err, ensure_ascii=False)
            return str(msg)[:300]
        return str(err)[:300]
    except (ValueError, AttributeError):
        return (raw or "")[:300]


def http_json(url, key=None, body=None, timeout=60):
    headers = {
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost/ki-team",
        "X-Title": "KI-Team",
    }
    if key is not None:
        normalized_key = normalize_api_key(key)
        if not normalized_key:
            raise ApiError("Kein OpenRouter-API-Key für diese Anfrage ausgewählt. Bitte in Einrichtung → API-Keys eintragen.")
        # Genau ein Bearer-Präfix; keine versehentlich doppelte Authentifizierung.
        headers["Authorization"] = "Bearer " + normalized_key
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        raise ApiError("HTTP %s: %s" % (e.code, _error_detail(raw)), status=e.code,
                       transient=e.code in (408, 409, 425, 429, 500, 502, 503, 504))
    except (urllib.error.URLError, OSError) as e:
        raise ApiError("Netzwerkfehler: %s" % e, transient=True)
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError("Antwort war kein gültiges JSON", transient=True)


def api_chat(key, model, messages, timeout=300):
    body = {"model": model, "messages": messages, "temperature": 0.3}
    data = http_json(API_BASE + "/chat/completions", key=key, body=body, timeout=timeout)
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        code = code if isinstance(code, int) else None
        raise ApiError(str(msg), status=code, transient=code in (408, 429, 500, 502, 503, 504))
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ApiError("Unerwartete Antwort von OpenRouter", transient=True)
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def fetch_models():
    """Lädt kostenlose Chat-LLMs und sortiert Coding-Modelle nach oben.

    OpenRouter hat auch kostenlose Spezial-Endpunkte (Embedding, Reranking,
    Bild-/Audio-Generierung und Decision APIs). Diese gehören nicht in die
    Chat-Auswahl, weil Engine.call_ai den Chat-Completions-Endpunkt nutzt.
    Nur echte :free-Varianten gelten hier als kostenlos ohne Guthabenkauf.
    """
    data = http_json(API_BASE + "/models", timeout=60)
    out = []
    excluded = ("embed", "rerank", "decider", "span-01", "recraft", "heygen", "fish-audio")
    preferred = (
        "cohere/north-mini-code", "particle-ai/particle-1", "recursive/flint",
        "nvidia/nemotron-3-ultra", "nvidia/nemotron-3.5-super-vl",
        "inclusionai/ling-3.2-flash", "baidu/ernie-5.5-flash",
        "dots-studio/dots-3-note", "thinkingmachines/inkling-2-small",
        "nvidia/nemotron-3.5-lightning", "google/gemma-4-31b",
        "google/gemma-4-26b", "liquid/lfm-3", "nvidia/nemotron-3-nano-omni",
    )

    for model in data.get("data", []):
        mid = str(model.get("id") or "").strip()
        if not mid or not mid.endswith(":free"):
            continue
        lowered = mid.lower()
        if any(term in lowered for term in excluded):
            continue
        arch = model.get("architecture") or {}
        output_modalities = arch.get("output_modalities") or []
        # Spezial-Endpunkte ohne Textausgabe sind keine Chatmodelle.
        if output_modalities and "text" not in output_modalities:
            continue
        out.append((mid, True))

    def rank(item):
        mid = item[0].lower()
        for index, marker in enumerate(preferred):
            if marker in mid:
                return (index, mid)
        # Weitere kostenlose Chatmodelle bleiben verfügbar, Coding-/Agenten-
        # Modelle werden aber vor allgemeinen Modellen angezeigt.
        description = ""
        for model in data.get("data", []):
            if model.get("id") == item[0]:
                description = str(model.get("description") or "").lower()
                break
        coding_terms = ("coding", "programming", "software", "code agent", "code generation")
        coding_rank = 0 if any(term in description for term in coding_terms) else 1
        return (len(preferred) + coding_rank, mid)

    out.sort(key=rank)
    # Die Oberfläche zeigt maximal 25 geprüfte Gratis-Chatmodelle.
    return out[:25]


def explain(e):
    hints = {
        401: "OpenRouter erkennt den Authorization-Header nicht korrekt. Im Key-Feld nur den eigentlichen sk-or-…-Key eintragen – ohne 'Bearer', Anführungszeichen oder Leerzeichen.",
        402: "Kein Guthaben oder Tageslimit erreicht.",
        403: "Zugriff verweigert (Key oder Modell nicht erlaubt).",
        404: "Modell nicht gefunden oder gerade ohne Anbieter – Modellname prüfen.",
        429: "Zu viele Anfragen (Limit der Gratis-Modelle) – Pause erhöhen oder später erneut starten.",
    }
    h = hints.get(getattr(e, "status", None))
    return str(e) + ((" → " + h) if h else "")


# --------------------------------------------------------------------------
# Parser fuer Modell-Antworten (robuster als JSON bei schwaecheren Modellen)
# --------------------------------------------------------------------------
THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
FILE_RE = re.compile(r"<<<FILE:\s*(.+?)\s*>>>[ \t]*\r?\n(.*?)\r?\n?[ \t]*<<<END>>>", re.S)
PLAN_RE = re.compile(r"<<<PLAN>>>\s*(.*?)<<<END>>>", re.S)
TASK_RE = re.compile(r"<<<TASK>>>\s*(.*?)<<<END>>>", re.S)


def clean(text):
    return THINK_RE.sub("", text or "").strip()


def strip_fence(content):
    lines = content.rstrip("\n").split("\n")
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return content


def parse_work(text):
    t = clean(text)
    files = []
    for p, c in FILE_RE.findall(t):
        p = p.strip().strip("`'\"")
        files.append((p, strip_fence(c)))
    msg = ""
    m = re.search(r"^[ \t]*MESSAGE:\s*(.*?)(?=^[ \t]*(?:STATUS:|<<<FILE:)|\Z)", t, re.S | re.M | re.I)
    if m:
        msg = m.group(1).strip()
    st = re.search(r"^[ \t]*STATUS:\s*(\w+)", t, re.M | re.I)
    status = st.group(1).upper() if st else ""
    return {"files": files, "message": msg, "status": status, "raw": t}


def parse_plan(text):
    t = clean(text)
    m = PLAN_RE.search(t)
    plan_md = m.group(1).strip() if m else ""
    tasks = []
    for block in TASK_RE.findall(t):
        title = re.search(r"^[ \t]*TITLE:[ \t]*(.*)$", block, re.M | re.I)
        files = re.search(r"^[ \t]*FILES:[ \t]*(.*)$", block, re.M | re.I)
        deps = re.search(r"^[ \t]*DEPENDS:[ \t]*(.*)$", block, re.M | re.I)
        desc = re.search(r"^[ \t]*DESCRIPTION:[ \t]*(.*)\Z", block, re.S | re.M | re.I)
        tid = len(tasks) + 1
        d = [int(x) for x in re.findall(r"\d+", deps.group(1))] if deps else []
        tasks.append({
            "id": tid,
            "title": (title.group(1).strip() if title else "Aufgabe %d" % tid) or "Aufgabe %d" % tid,
            "description": desc.group(1).strip() if desc else block.strip(),
            "files": [f.strip().strip("`'\"") for f in (files.group(1).split(",") if files else []) if f.strip()],
            "depends": sorted(set(x for x in d if 0 < x < tid)),
            "status": "offen",
            "owner": "",
            "attempts": 0,
        })
        if len(tasks) >= 20:
            break
    return plan_md, tasks


def parse_clarifying_questions(text):
    """Liest bis zu drei sinnvolle Planungsfragen aus einer JSON-Antwort."""
    raw = clean(text).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("questions", [])
    if not isinstance(data, list):
        return []
    questions = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        if not question:
            continue
        options = item.get("options", [])
        if not isinstance(options, list):
            options = []
        options = [str(x).strip()[:180] for x in options if str(x).strip()][:4]
        recommendation = str(item.get("recommendation", "")).strip()[:500]
        if not recommendation and options:
            recommendation = options[0]
        questions.append({
            "question": question[:500],
            "recommendation": recommendation,
            "options": options,
        })
        if len(questions) >= 3:
            break
    return questions


# --------------------------------------------------------------------------
# Dateien im Projektordner
# --------------------------------------------------------------------------
def safe_path(root, rel):
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    if not rel or rel.startswith("~"):
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if len(parts) == 1 and parts[0].lower() in RESERVED:
        return None
    full = os.path.realpath(os.path.join(root, *parts))
    rootr = os.path.realpath(root)
    if not (full == rootr or full.startswith(rootr + os.sep)):
        return None
    return full


def list_project_files(root):
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(fn):
            rel = os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
            if rel.lower() in RESERVED:
                continue
            out.append(rel)
    return out


def read_text(path, limit):
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit * 4 + 1)
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    s = raw.decode("utf-8", "ignore")
    if len(s) > limit:
        s = s[:limit] + "\n... [gekürzt]"
    return s


def gather_context(root, priority, budget=40000, per_file=12000):
    files = list_project_files(root)
    if not files:
        return "(Der Projektordner enthält noch keine Dateien.)"
    prio_set = set(p.lower() for p in priority)
    prio = [f for f in files if f.lower() in prio_set]
    rest = [f for f in files if f.lower() not in prio_set]
    parts = ["Dateien im Projektordner:\n" + "\n".join("- " + f for f in files), ""]
    used = 0
    skipped = False
    for rel in prio + rest:
        txt = read_text(os.path.join(root, *rel.split("/")), per_file)
        if txt is None:
            continue
        block = "<<<FILE: %s>>>\n%s\n<<<END>>>" % (rel, txt)
        if used + len(block) > budget:
            skipped = True
            continue
        parts.append(block)
        used += len(block)
    if skipped:
        parts.append("(Weitere Dateien wurden aus Platzgründen weggelassen.)")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
SYSTEM_TMPL = (
    "Du bist {name}, Mitglied eines Teams aus mehreren KIs, die gemeinsam ein Software-Projekt "
    "in einem Ordner bauen. Das Team verständigt sich über die Datei CHAT.md (Auszug im Auftrag). "
    "Halte dich EXAKT an das verlangte Antwortformat und schreibe nichts davor oder danach. "
    "Nachrichten ans Team schreibst du auf Deutsch. {role}"
)

PLAN_FORMAT = (
    "<<<PLAN>>>\n"
    "(Plan als Markdown: Ziel, Technik und Struktur, Dateiliste, Hinweise fürs Team)\n"
    "<<<END>>>\n"
    "<<<TASK>>>\n"
    "TITLE: Kurzer Titel\n"
    "FILES: datei1.py, ordner/datei2.py\n"
    "DEPENDS: (Nummern früherer Aufgaben, z. B. 1, 2 – oder leer lassen)\n"
    "DESCRIPTION: Genaue Beschreibung, was in diesen Dateien umgesetzt werden soll.\n"
    "<<<END>>>\n"
    "(weitere <<<TASK>>>-Blöcke im selben Aufbau)"
)

WORK_FORMAT = (
    "MESSAGE: (1-3 Sätze ans Team: was du getan hast und was die anderen wissen müssen)\n"
    "STATUS: DONE   (oder STATUS: MORE, wenn du noch nicht fertig bist)\n"
    "<<<FILE: relativer/pfad/datei.ext>>>\n"
    "(kompletter Inhalt der Datei)\n"
    "<<<END>>>\n"
    "(weitere <<<FILE>>>-Blöcke, falls nötig)"
)

FILE_RULES = (
    "Regeln: Gib jede geänderte oder neue Datei VOLLSTÄNDIG aus (keine Auslassungen wie '...'). "
    "Nur relative Pfade innerhalb des Projektordners. Die Dateien CHAT.md, PLAN.md und tasks.json "
    "verwaltet die App, schreibe sie nicht."
)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class RunAborted(Exception):
    pass


class StopRequested(RunAborted):
    pass


class Limiter:
    """Sorgt fuer einen Mindestabstand zwischen API-Anfragen (Gratis-Limits)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self, pause, stop):
        with self.lock:
            delay = self.last + pause - time.time()
            while delay > 0:
                if stop.is_set():
                    raise StopRequested("Vom Nutzer gestoppt.")
                time.sleep(min(0.25, delay))
                delay = self.last + pause - time.time()
            self.last = time.time()


def ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


class Engine:
    def __init__(self, cfg, emit):
        self.cfg = cfg
        self.emit = emit
        self.root = os.path.abspath(os.path.expanduser(cfg["folder"].strip()))
        self.stop_ev = threading.Event()
        self.lock = threading.RLock()
        self.limiter = Limiter()
        self.tasks = []
        self.calls = 0
        self.fail = {}
        self.proc = None

    # ---- Hilfen ----
    def name(self, i):
        return slot_name(self.cfg, i)

    def ready(self, i):
        return slot_ready(self.cfg, i)

    @staticmethod
    def tag(i):
        return "slot%d" % i

    def log(self, text, tag="sys"):
        self.emit("log", (tag, text))

    def check_stop(self):
        if self.stop_ev.is_set():
            raise StopRequested("Vom Nutzer gestoppt.")

    def sleep(self, sec):
        end = time.time() + sec
        while time.time() < end:
            self.check_stop()
            time.sleep(0.25)

    def stop(self):
        self.stop_ev.set()
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass

    def path(self, name):
        return os.path.join(self.root, name)

    def chat(self, author, text, tag="sys"):
        text = (text or "").strip()
        entry = "### [%s] %s\n%s\n\n" % (ts(), author, text)
        with self.lock:
            with open(self.path("CHAT.md"), "a", encoding="utf-8") as f:
                f.write(entry)
        self.log("%s: %s" % (author, text), tag)

    def chat_tail(self, limit=3500):
        try:
            with open(self.path("CHAT.md"), "r", encoding="utf-8") as f:
                s = f.read()
        except OSError:
            return ""
        return s[-limit:]

    def save_tasks(self):
        with self.lock:
            with open(self.path("tasks.json"), "w", encoding="utf-8") as f:
                json.dump(self.tasks, f, ensure_ascii=False, indent=2)
            self.emit("tasks", [dict(t) for t in self.tasks])

    def load_tasks(self):
        try:
            with open(self.path("tasks.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            raise RunAborted("tasks.json fehlt oder ist unlesbar. Lass zuerst die Phase „Planen“ laufen.")
        tasks = []
        for n, t in enumerate(data if isinstance(data, list) else [], 1):
            if not isinstance(t, dict):
                continue
            st = t.get("status", "offen")
            if st in ("in Arbeit", "Fehler"):
                st = "offen"
            tasks.append({
                "id": t.get("id", n),
                "title": str(t.get("title", "Aufgabe %d" % n)),
                "description": str(t.get("description", "")),
                "files": [str(x) for x in t.get("files", [])] if isinstance(t.get("files"), list) else [],
                "depends": [x for x in t.get("depends", []) if isinstance(x, int)] if isinstance(t.get("depends"), list) else [],
                "status": st,
                "owner": str(t.get("owner", "")),
                "attempts": 0 if st == "offen" else int(t.get("attempts", 0) or 0),
            })
        self.tasks = tasks
        self.save_tasks()


    def provide_user_answers(self, answer_text):
        """Wird vom GUI-Thread aufgerufen, nachdem der Benutzer geantwortet hat."""
        self.user_answers = answer_text
        event = getattr(self, "_questions_event", None)
        if event is not None:
            event.set()

    def request_user_answers(self, questions):
        self.user_answers = ""
        self._questions_event = threading.Event()
        self.emit("questions", questions)
        while not self._questions_event.wait(0.2):
            self.check_stop()
        self.check_stop()
        return self.user_answers or ""

    def team_meeting(self, task):
        """Kurzes, echtes Multi-Agent-Meeting: Jede KI antwortet auf die vorherigen Beiträge."""
        if not self.cfg.get("team_meeting", True):
            return
        participants = []
        for idx in [self.cfg.get("planner", 0)] + list(self.cfg.get("builders", [])) + list(self.cfg.get("debuggers", [])):
            if isinstance(idx, int) and 0 <= idx < NUM_SLOTS and idx not in participants and self.ready(idx):
                participants.append(idx)
        if len(participants) < 2:
            self.log("Team-Meeting übersprungen: Mindestens zwei einsatzbereite KIs werden benötigt.", "warn")
            return
        self.log("phase", "✦ Team-Meeting – die KIs beraten sich gegenseitig …")
        self.chat("KI-Team", "Team-Meeting gestartet. Jede KI liest die bisherigen Beiträge und baut darauf auf.")
        for turn, idx in enumerate(participants, 1):
            self.check_stop()
            prompt = (
                "Wir führen ein kurzes Architektur- und Planungsmeeting für dieses Projekt.\n"
                "Projektwunsch des Benutzers:\n%s\n\n"
                "Bisherige Beiträge im Team-Chat (du sollst darauf eingehen):\n%s\n\n"
                "Du bist %s. Antworte auf Deutsch in höchstens 5 knappen Stichpunkten. "
                "Beziehe dich ausdrücklich auf mindestens einen vorherigen Beitrag, wenn vorhanden: "
                "stimme zu, ergänze oder widersprich sachlich. Nenne Risiken, sinnvolle Prioritäten und "
                "eine konkrete Empfehlung. Stelle noch keine Rückfragen an den Benutzer und schreibe keinen Code."
            ) % (task, self.chat_tail(5500), self.name(idx))
            try:
                reply = self.call_ai(idx, prompt)
                self.chat(self.name(idx), "Meeting-Beitrag %d/%d:\n%s" % (turn, len(participants), reply[:1800]),
                          self.tag(idx))
            except (ApiError, RunAborted) as exc:
                if isinstance(exc, StopRequested):
                    raise
                self.log("%s konnte am Meeting nicht teilnehmen: %s" % (self.name(idx), explain(exc)
                          if isinstance(exc, ApiError) else exc), "warn")
        self.chat("KI-Team", "Team-Meeting beendet. Der Planner übernimmt die abgestimmten Vorschläge in den Plan.")

    # ---- KI-Aufruf ----
    def call_ai(self, i, user):
        slot = self.cfg["slots"][i]
        role = (slot.get("role") or "").strip()
        system = SYSTEM_TMPL.format(name=self.name(i), role=("Dein Schwerpunkt: " + role) if role else "")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        attempt = 0
        while True:
            self.check_stop()
            with self.lock:
                if self.calls >= int(self.cfg["max_calls"]):
                    raise RunAborted("Anfrage-Limit (%s) erreicht – Lauf beendet. Das Limit kannst du in der "
                                     "Einrichtung erhöhen." % self.cfg["max_calls"])
                self.calls += 1
                n = self.calls
            self.emit("calls", n)
            self.limiter.wait(float(self.cfg["pause"]), self.stop_ev)
            try:
                text = clean(api_chat(slot_key(self.cfg, i), str(slot["model"]).strip(), messages))
                if not text:
                    raise ApiError("Leere Antwort", transient=True)
                self.fail[i] = 0
                return text
            except ApiError as e:
                attempt += 1
                if e.transient and attempt < 4:
                    wait = 8 * attempt * (2 if e.status == 429 else 1)
                    self.log("%s: %s – neuer Versuch in %ds" % (self.name(i), explain(e), wait), "warn")
                    self.sleep(wait)
                    continue
                raise

    def write_files(self, files, author):
        written = []
        for rel, content in files:
            full = safe_path(self.root, rel)
            if full is None:
                self.log("%s wollte „%s“ schreiben – Pfad nicht erlaubt, übersprungen." % (author, rel), "warn")
                continue
            if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                self.log("Datei „%s“ ist zu groß, übersprungen." % rel, "warn")
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content if content.endswith("\n") else content + "\n")
            written.append(rel)
        return written

    # ---- Ablauf ----
    def run(self):
        msg = "Fertig."
        try:
            os.makedirs(self.root, exist_ok=True)
            if not os.path.exists(self.path("CHAT.md")):
                with open(self.path("CHAT.md"), "w", encoding="utf-8") as f:
                    f.write("# Team-Chat\n\nHier verständigen sich die KIs.\n\n")
            if self.cfg["do_plan"]:
                self.phase_plan()
            else:
                self.load_tasks()
            if self.cfg["do_build"]:
                self.phase_build()
            if self.cfg["do_debug"]:
                self.phase_debug()
            done = len([t for t in self.tasks if t["status"] == "fertig"])
            msg = "Fertig – %d von %d Aufgaben erledigt. Ergebnis liegt in: %s" % (done, len(self.tasks), self.root)
        except StopRequested:
            msg = "Gestoppt."
        except RunAborted as e:
            msg = str(e)
        except Exception as e:
            self.log(traceback.format_exc(), "err")
            msg = "Abgebrochen wegen eines Fehlers: %s" % e
        finally:
            self.emit("finished", msg)

    # Phase 1
    def phase_plan(self):
        self.emit("phase", "plan")
        idx = self.cfg["planner"]
        task = self.cfg["task"].strip()
        mode = self.cfg.get("planning_mode", "guided")
        self.log("phase", "Phase 1 · Planen – %s startet die Planung …" % self.name(idx))

        # Erst diskutiert das Team die Architektur und baut aufeinander auf.
        self.team_meeting(task)

        answers = ""
        if mode == "guided":
            self.log("phase", "Der Planner prüft, ob wichtige Entscheidungen mit dir abgestimmt werden sollten …")
            question_prompt = (
                "Du bist der Planner eines KI-Entwicklungsteams. Prüfe den Projektwunsch und die bisherigen "
                "Team-Meeting-Beiträge. Gibt es wirklich wichtige, noch offene Entscheidungen, bei denen die "
                "Präferenz des Benutzers den Plan deutlich verändern würde? Wenn ja, stelle maximal 3 kurze, "
                "konkrete Fragen. Zu jeder Frage gib eine klare Empfehlung mit kurzer Begründung und 2-4 "
                "mögliche Optionen. Frage NICHT nach Dingen, die sich sinnvoll standardmäßig entscheiden lassen. "
                "Wenn der Auftrag bereits klar genug ist, gib eine leere Liste zurück.\n"
                "Antworte ausschließlich als gültiges JSON-Array, z. B.: "
                '[{"question":"Welche Art von Navigation?","recommendation":"Obere Navigation, weil ...",'
                '"options":["Oben","Seitlich"]}]'
                "\nProjektwunsch:\n%s\n\nTeam-Chat:\n%s"
            ) % (task, self.chat_tail(5000))
            try:
                qtext = self.call_ai(idx, question_prompt)
                questions = parse_clarifying_questions(qtext)
            except ApiError as exc:
                self.log("Rückfragen konnten nicht ermittelt werden; der Planner arbeitet mit den vorhandenen Angaben weiter: %s"
                         % explain(exc), "warn")
                questions = []
            if questions:
                self.log("phase", "Der Planner hat %d Rückfrage(n) mit Empfehlung vorbereitet." % len(questions))
                answers = self.request_user_answers(questions)
                if answers == "__AUTO__":
                    answers = "Der Benutzer hat den Automatikmodus für diesen Auftrag gewählt. Triff die aus deiner Sicht besten Entscheidungen und dokumentiere sie im Plan."
                elif answers:
                    self.chat("Benutzer", "Antworten auf die Planungsfragen:\n" + answers)
            else:
                self.log("ok", "Keine zusätzlichen Rückfragen nötig – der Auftrag ist ausreichend klar.")

        n_build = max(1, len([i for i in self.cfg["builders"] if self.ready(i)]))
        run_hint = ("Das Projekt wird später mit diesem Befehl getestet: %s" % self.cfg["run_cmd"]) \
            if self.cfg["run_cmd"] else ""
        mode_hint = (
            "AUTOMATIKMODUS: Stelle keine weiteren Rückfragen. Entscheide technische Details selbst anhand "
            "bewährter, zum Auftrag passender Lösungen. Nenne wichtige Annahmen und begründe sie kurz im Plan."
            if mode == "auto" or answers.startswith("Der Benutzer hat den Automatikmodus") else
            "Berücksichtige die folgenden Antworten und Präferenzen des Benutzers ausdrücklich: " + answers
            if answers else
            "Der Auftrag ist klar genug. Triff nur notwendige Standardentscheidungen und dokumentiere wichtige Annahmen."
        )
        prompt = (
            "Wunsch des Auftraggebers:\n%s\n\n"
            "Team-Meeting: Lies CHAT.md und integriere die Vorschläge, Einwände und Empfehlungen der anderen KIs. "
            "Begründe bei unterschiedlichen Vorschlägen kurz deine Entscheidung.\n"
            "%s\n\n"
            "Deine Aufgabe: Entwirf einen umsetzbaren Plan für ein Team aus %d KIs. Zerlege das Projekt in "
            "4 bis 12 kleine Aufgaben, jede in einer einzigen Antwort lösbar. Jede Aufgabe nennt die Dateien, "
            "die sie schreibt; verschiedene Aufgaben sollen möglichst verschiedene Dateien haben. "
            "Gib Reihenfolge-Abhängigkeiten über DEPENDS an. %s\n\n"
            "Bereits vorhandene Projektdateien:\n%s\n\n"
            "Antworte exakt in diesem Format:\n%s"
        ) % (task, mode_hint, n_build, run_hint, gather_context(self.root, [], budget=12000), PLAN_FORMAT)
        plan_md, tasks = "", []
        for attempt in range(2):
            try:
                text = self.call_ai(idx, prompt if attempt == 0 else
                                    prompt + "\n\nWICHTIG: Deine letzte Antwort hatte nicht das verlangte Format. "
                                             "Nutze genau die Blöcke <<<PLAN>>> und <<<TASK>>> mit <<<END>>>.")
            except ApiError as e:
                raise RunAborted("Planen fehlgeschlagen: %s" % explain(e))
            plan_md, tasks = parse_plan(text)
            if tasks:
                break
            self.log("Der Plan hatte kein lesbares Format – zweiter Versuch …", "warn")
        if not tasks:
            self.log("Kein lesbarer Plan erhalten – ich lege eine einzige Gesamtaufgabe an.", "warn")
            tasks = [{"id": 1, "title": "Projekt umsetzen", "description": task, "files": [], "depends": [],
                      "status": "offen", "owner": "", "attempts": 0}]
            plan_md = plan_md or "(kein Plan erhalten)"
        with open(self.path("PLAN.md"), "w", encoding="utf-8") as f:
            f.write("# Auftrag\n\n%s\n\n# Plan (%s)\n\n%s\n" % (task, self.name(idx), plan_md or "(kein Plan-Text)"))
        self.tasks = tasks
        self.save_tasks()
        lines = "\n".join("%d. %s" % (t["id"], t["title"]) for t in tasks)
        self.chat(self.name(idx), "Plan steht (PLAN.md, tasks.json). Aufgaben:\n" + lines, self.tag(idx))

    # Phase 2
    def pick_wave(self, n):
        def deps_done(t):
            by_id = dict((x["id"], x) for x in self.tasks)
            return all(by_id.get(d, {}).get("status") in ("fertig", "Fehler") for d in t["depends"])
        open_tasks = [t for t in self.tasks if t["status"] == "offen"]
        eligible = [t for t in open_tasks if deps_done(t)] or open_tasks
        picked, used = [], set()
        for t in eligible:
            fs = set(f.lower() for f in t["files"])
            if fs and fs & used:
                continue
            picked.append(t)
            used |= fs
            if len(picked) >= n:
                break
        return picked

    def phase_build(self):
        self.emit("phase", "build")
        builders = [i for i in self.cfg["builders"] if self.ready(i)]
        if not builders:
            raise RunAborted("Keine einsatzbereite Bau-KI (Modell/Key fehlt).")
        size = len(builders) if self.cfg["parallel"] else 1
        names = ", ".join(self.name(i) for i in builders)
        self.log("Phase 2 · Bauen – %s arbeiten den Plan ab%s." % (
            names, " (parallel)" if size > 1 else " (reihum)"), "phase")
        rot, guard = 0, 0
        while True:
            self.check_stop()
            healthy = [b for b in builders if self.fail.get(b, 0) < 3]
            if not healthy:
                raise RunAborted("Alle Bau-KIs sind ausgefallen (je 3 Fehler in Folge). Prüfe Keys und Modelle.")
            picks = self.pick_wave(min(size, len(healthy)))
            if not picks:
                break
            guard += 1
            if guard > 300:
                break
            jobs = []
            for t in picks:
                b = healthy[rot % len(healthy)]
                rot += 1
                with self.lock:
                    t["status"] = "in Arbeit"
                    t["owner"] = self.name(b)
                    t["attempts"] += 1
                jobs.append((t, b))
            self.save_tasks()
            if len(jobs) == 1:
                self.do_task(*jobs[0])
            else:
                with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                    futs = [ex.submit(self.do_task, t, b) for t, b in jobs]
                    for f in futs:
                        f.result()
            self.save_tasks()
        done = len([t for t in self.tasks if t["status"] == "fertig"])
        self.log("Bauen beendet: %d von %d Aufgaben fertig." % (done, len(self.tasks)), "ok")

    def build_prompt(self, t):
        try:
            with open(self.path("PLAN.md"), "r", encoding="utf-8") as f:
                plan = f.read()[:8000]
        except OSError:
            plan = "(kein PLAN.md vorhanden)"
        overview = "\n".join("- [%s] %s %s%s" % (
            x["status"], x["id"], x["title"], (" (" + x["owner"] + ")") if x["owner"] else "") for x in self.tasks)
        priority = list(t["files"])
        for d in t["depends"]:
            for x in self.tasks:
                if x["id"] == d:
                    priority += x["files"]
        retry = ""
        if t["attempts"] > 1:
            retry = "\nHinweis: Das ist Versuch %d für diese Aufgabe. Schau in die Dateien, was schon da ist, " \
                    "und mache sie fertig.\n" % t["attempts"]
        return (
            "=== PLAN.md ===\n%s\n\n=== Aufgabenstand ===\n%s\n\n=== Letzte Team-Nachrichten (CHAT.md) ===\n%s\n\n"
            "=== Projektdateien ===\n%s\n\n=== DEINE AUFGABE (Nr. %s): %s ===\nDateien: %s\n%s\n%s\n%s\n\n"
            "Antworte exakt in diesem Format:\n%s"
        ) % (plan, overview, self.chat_tail(), gather_context(self.root, priority),
             t["id"], t["title"], ", ".join(t["files"]) or "(frei wählbar)", t["description"], retry,
             FILE_RULES, WORK_FORMAT)

    def do_task(self, t, b):
        name = self.name(b)
        self.log("%s übernimmt Aufgabe %s: %s" % (name, t["id"], t["title"]), self.tag(b))
        try:
            text = self.call_ai(b, self.build_prompt(t))
        except ApiError as e:
            self.fail[b] = self.fail.get(b, 0) + 1
            self.log("%s: %s" % (name, explain(e)), "err")
            with self.lock:
                t["status"] = "offen" if t["attempts"] < 3 else "Fehler"
            return
        res = parse_work(text)
        if not res["files"] and res["status"] not in ("DONE", "MORE"):
            self.fail[b] = self.fail.get(b, 0) + 1
            self.log("%s: Antwort nicht lesbar (Anfang: %r)" % (name, res["raw"][:160]), "warn")
            with self.lock:
                t["status"] = "offen" if t["attempts"] < 3 else "Fehler"
            return
        with self.lock:
            written = self.write_files(res["files"], name)
            note = res["message"] or "(keine Nachricht)"
            self.chat(name, "Aufgabe %s (%s): %s\nDateien: %s" % (
                t["id"], t["title"], note, ", ".join(written) or "keine"), self.tag(b))
            finished = res["status"] == "DONE" or (res["status"] == "" and written)
            if finished:
                t["status"] = "fertig"
            elif t["attempts"] >= 3:
                t["status"] = "fertig"
                self.log("Aufgabe %s nach 3 Versuchen als erledigt markiert – das Debuggen soll den Rest richten."
                         % t["id"], "warn")
            else:
                t["status"] = "offen"
            self.fail[b] = 0

    # Phase 3
    def run_command(self):
        cmd = self.cfg["run_cmd"].strip()
        timeout = max(3, int(self.cfg["run_timeout"]))
        self.log("Teste: %s" % cmd, "sys")
        timed_out = False
        p = subprocess.Popen(cmd, shell=True, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, errors="replace", start_new_session=True)
        self.proc = p
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass
            out, _ = p.communicate()
        finally:
            self.proc = None
        self.check_stop()
        out = (out or "")[-6000:]
        if timed_out:
            ok = "Traceback" not in out
            out += "\n[Test nach %ds beendet – Programm lief weiter]" % timeout
        else:
            ok = p.returncode == 0
            out += "\n[Exit-Code %s]" % p.returncode
        return ok, out

    def phase_debug(self):
        self.emit("phase", "debug")
        dbg = [i for i in self.cfg["debuggers"] if self.ready(i)]
        if not dbg:
            raise RunAborted("Keine einsatzbereite Debug-KI (Modell/Key fehlt).")
        first, second = dbg[0], (dbg[1] if len(dbg) > 1 else dbg[0])
        cmd = self.cfg["run_cmd"].strip()
        rounds = max(1, int(self.cfg["debug_rounds"])) if cmd else 1
        self.log("Phase 3 · Debuggen – %s und %s%s." % (
            self.name(first), self.name(second), "" if cmd else " (ohne Testbefehl: ein Review-Durchgang)"), "phase")
        success = False
        for r in range(rounds + 1 if cmd else 1):
            self.check_stop()
            if cmd:
                ok, out = self.run_command()
                if ok:
                    success = True
                    self.chat("App", "Test erfolgreich.\n" + out[-800:], "ok")
                    break
                self.log("Test fehlgeschlagen:\n" + out[-1500:], "err")
                if r >= rounds:
                    break
            else:
                out = "(Kein Testbefehl eingestellt. Prüfe den Code auf Fehler, fehlende Teile und " \
                      "Widersprüche zum PLAN.md.)"
            analyst, fixer = (first, second) if r % 2 == 0 else (second, first)
            ctx = gather_context(self.root, [])
            try:
                analysis = self.call_ai(analyst, (
                    "Das Projekt wurde getestet.\nTestausgabe:\n%s\n\n=== Letzte Team-Nachrichten ===\n%s\n\n"
                    "=== Projektdateien ===\n%s\n\nAnalysiere kurz (höchstens 15 Zeilen) die wahrscheinliche "
                    "Ursache und nenne konkret, welche Datei wie geändert werden muss. Schreibe noch KEINEN "
                    "kompletten Code.") % (out, self.chat_tail(), ctx))
                self.chat(self.name(analyst), "Analyse (Runde %d):\n%s" % (r + 1, analysis[:1500]),
                          self.tag(analyst))
                text = self.call_ai(fixer, (
                    "Analyse von %s:\n%s\n\nTestausgabe:\n%s\n\n=== Projektdateien ===\n%s\n\n"
                    "Behebe die Fehler. %s\n\nAntworte exakt in diesem Format:\n%s") % (
                    self.name(analyst), analysis[:3000], out, gather_context(self.root, []), FILE_RULES, WORK_FORMAT))
            except ApiError as e:
                self.log("Debuggen: %s" % explain(e), "err")
                self.sleep(2)
                continue
            res = parse_work(text)
            written = self.write_files(res["files"], self.name(fixer))
            self.chat(self.name(fixer), "Fix (Runde %d): %s\nDateien: %s" % (
                r + 1, res["message"] or "(keine Nachricht)", ", ".join(written) or "keine"), self.tag(fixer))
        if cmd:
            if success:
                self.log("Debuggen beendet: Test läuft durch.", "ok")
            else:
                self.log("Debuggen beendet: Test ist noch nicht grün. Schau in CHAT.md und die letzte "
                         "Testausgabe oben.", "warn")
        else:
            self.log("Review-Durchgang beendet.", "ok")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
# Claude-inspirierte Farbwelten mit umschaltbarem Dunkelmodus.
PALETTES = {
    "light": {"BG":"#F6F4EF", "PANEL":"#F0EDE6", "CARD":"#FFFEFB", "FIELD":"#F7F5F0",
              "BORDER":"#E6E1D8", "FG":"#302E2A", "MUTED":"#817B72", "ACCENT":"#C66B4E",
              "ACCENT_DARK":"#A9553B", "ACCENT2":"#738C78", "WARN":"#B77932", "ERR":"#B84F4F",
              "OK":"#64866A"},
    "dark": {"BG":"#191816", "PANEL":"#24221F", "CARD":"#2C2925", "FIELD":"#211F1C",
             "BORDER":"#403B35", "FG":"#F0ECE5", "MUTED":"#AAA196", "ACCENT":"#D27A5C",
             "ACCENT_DARK":"#B96549", "ACCENT2":"#91AA96", "WARN":"#D7A056", "ERR":"#E17D78",
             "OK":"#8EB294"},
}
SLOT_COLORS = ["#B76F57", "#6F8E7A", "#B58B4E", "#8C7C9B"]


def apply_palette_globals(dark=False):
    values = PALETTES["dark" if dark else "light"]
    globals().update(values)


def to_float(s, default):
    try:
        return float(str(s).replace(",", "."))
    except ValueError:
        return default


def to_int(s, default):
    try:
        return int(float(str(s).replace(",", ".")))
    except ValueError:
        return default


_Base = tk.Tk if tk is not None else object

AUTO_MODEL_LABEL = "OpenRouter · Auto-Free"
MODEL_LABELS = (
    ("cohere/north-mini-code", "Cohere · North Mini Code"),
    ("particle-ai/particle-1", "Particle AI · Particle-1"),
    ("recursive/flint", "Recursive · Flint"),
    ("nvidia/nemotron-3-ultra", "NVIDIA · Nemotron 3 Ultra"),
    ("nvidia/nemotron-3.5-super-vl", "NVIDIA · Nemotron 3.5 Super VL"),
    ("inclusionai/ling-3.2-flash", "InclusionAI · Ling 3.2 Flash"),
    ("baidu/ernie-5.5-flash", "Baidu · ERNIE 5.5 Flash"),
    ("dots-studio/dots-3-note", "Dots Studio · Dots3-Note Preview"),
    ("thinkingmachines/inkling-2-small", "Thinking Machines · Inkling 2 Small"),
    ("nvidia/nemotron-3.5-lightning", "NVIDIA · Nemotron 3.5 Lightning"),
    ("google/gemma-4-31b", "Google · Gemma 4 31B"),
    ("google/gemma-4-26b", "Google · Gemma 4 26B A4B"),
    ("liquid/lfm-3", "Liquid AI · LFM3-1.7B"),
    ("nvidia/nemotron-3-nano-omni", "NVIDIA · Nemotron 3 Nano Omni"),
)


def display_model_name(model_id):
    lowered = model_id.lower()
    for marker, label in MODEL_LABELS:
        if marker in lowered:
            return label
    provider, _, slug = model_id.partition("/")
    slug = slug.split(":", 1)[0]
    slug = re.sub(r"-20\d{6}$", "", slug)
    return "%s · %s" % (provider.title(), slug.replace("-", " ").title())


class App(_Base):
    def __init__(self):
        super().__init__()
        self.title("KI-Team · Studio")
        self.geometry("1320x860")
        self.minsize(1080, 720)
        self.cfg = load_config()
        self.dark_mode = bool(self.cfg.get("dark_mode", False))
        apply_palette_globals(self.dark_mode)
        self.configure(bg=BG)
        self._anim_index = 0
        self._anim_symbols = ["◐", "◓", "◑", "◒"]
        self.q = queue.Queue()
        self.engine = None
        self.models = []
        self.model_label_to_id = {AUTO_MODEL_LABEL: "openrouter/free"}
        self.model_id_to_label = {"openrouter/free": AUTO_MODEL_LABEL}
        self.running = False
        self.pick_fonts()
        self.setup_style()
        self.build_ui()
        self.load_into_ui()
        self.enable_autosave()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self.poll)
        self.after(160, self.animate_ui)
        self.after(700, self.load_models)

    # ---- Aussehen ----
    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        apply_palette_globals(self.dark_mode)
        self.cfg["dark_mode"] = self.dark_mode
        try:
            save_config(self.collect_cfg())
        except (OSError, tk.TclError, ValueError):
            pass
        self.apply_theme_to_widgets()

    def apply_theme_to_widgets(self):
        self.configure(bg=BG)
        self.setup_style()
        self.theme_btn.configure(text="☀  Light Mode" if self.dark_mode else "☾  Dark Mode")
        self.task_text.configure(bg=FIELD, fg=FG, insertbackground=FG)
        self.logbox.configure(bg=PANEL, fg=FG, insertbackground=FG)
        for i, dot in enumerate(getattr(self, "dot_labels", [])):
            dot.configure(bg=CARD, fg=SLOT_COLORS[i])
        for key, label in getattr(self, "phase_lbls", {}).items():
            label.configure(bg=CARD)
        self.logbox.tag_configure("sys", foreground=("#C5C0B8" if self.dark_mode else "#aab0c6"))
        self.logbox.tag_configure("warn", foreground=WARN)
        self.logbox.tag_configure("err", foreground=ERR)
        self.logbox.tag_configure("ok", foreground=OK)
        self.logbox.tag_configure("phase", foreground=ACCENT2, font=(self.fm, 10, "bold"))
        self.logbox.tag_configure("time", foreground=MUTED)
        self.logbox.tag_configure("slot0", foreground=SLOT_COLORS[0])
        self.logbox.tag_configure("slot1", foreground=SLOT_COLORS[1])
        self.logbox.tag_configure("slot2", foreground=SLOT_COLORS[2])
        self.logbox.tag_configure("slot3", foreground=SLOT_COLORS[3])

    def animate_ui(self):
        """Dezente Tkinter-Mikroanimation: animierter Aktivitätsindikator statt störender Effekte."""
        try:
            self._anim_index = (self._anim_index + 1) % len(self._anim_symbols)
            if self.running:
                color = ACCENT if self._anim_index % 2 else ACCENT2
                self.activity_lbl.configure(text="%s  KI-TEAM ARBEITET" % self._anim_symbols[self._anim_index],
                                            foreground=color)
            else:
                self.activity_lbl.configure(text="✦  STUDIO BEREIT", foreground=ACCENT2)
        except tk.TclError:
            return
        self.after(220, self.animate_ui)

    def pick_fonts(self):
        fams = set(tkfont.families())

        def first(cands, default):
            for c in cands:
                if c in fams:
                    return c
            return default
        self.ff = first(["Ubuntu", "Noto Sans", "DejaVu Sans", "Cantarell"], "TkDefaultFont")
        self.fh = first(["Georgia", "Liberation Serif", "DejaVu Serif"], self.ff)
        self.fm = first(["Ubuntu Mono", "DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono"], "TkFixedFont")

    def setup_style(self):
        ff = self.ff
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER, troughcolor=PANEL, font=(ff, 10), focuscolor=ACCENT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD, borderwidth=1, relief="solid")
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Card.TLabel", background=CARD, foreground=FG)
        st.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=(ff, 9))
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(ff, 9))
        st.configure("Eyebrow.TLabel", background=BG, foreground=ACCENT, font=(ff, 9, "bold"))
        st.configure("CardH.TLabel", background=CARD, foreground=FG, font=(ff, 12, "bold"))
        st.configure("H1.TLabel", background=BG, foreground=FG, font=(self.fh, 27, "bold"))
        st.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, bordercolor=BORDER,
                     padding=7, relief="flat")
        st.configure("TSpinbox", fieldbackground=FIELD, foreground=FG, insertcolor=FG, bordercolor=BORDER,
                     arrowcolor=FG, background=FIELD, padding=6)
        st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG, arrowcolor=MUTED,
                     bordercolor=BORDER, insertcolor=FG, padding=7)
        st.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
               foreground=[("readonly", FG), ("disabled", MUTED)],
               selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", (ff, 10))
        st.configure("TButton", background=PANEL, foreground=FG, padding=(14, 9), borderwidth=0,
                     focusthickness=0, relief="flat", font=(ff, 10))
        st.map("TButton", background=[("active", "#E6E0D6"), ("disabled", PANEL)],
               foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", font=(ff, 11, "bold"),
                     padding=(22, 11))
        st.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#D9B6A8")],
               foreground=[("disabled", "#FFF8F4")])
        st.configure("Stop.TButton", background="#F3E5E1", foreground="#9C493A", padding=(18, 10))
        st.map("Stop.TButton", background=[("active", "#EBD3CC"), ("disabled", PANEL)],
               foreground=[("disabled", MUTED)])
        for name in ("Card.TCheckbutton", "Card.TRadiobutton"):
            st.configure(name, background=CARD, foreground=FG, focusthickness=0)
        st.map("Card.TCheckbutton", background=[("active", CARD)],
               indicatorcolor=[("selected", ACCENT), ("!selected", "#D5CFC5")])
        st.map("Card.TRadiobutton", background=[("active", CARD)],
               indicatorcolor=[("selected", ACCENT), ("!selected", "#D5CFC5")])
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(22, 11), borderwidth=0,
                     font=(ff, 10, "bold"))
        st.map("TNotebook.Tab", background=[("selected", CARD), ("active", PANEL)],
               foreground=[("selected", ACCENT), ("active", FG)])
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=32,
                     borderwidth=0, font=(ff, 10))
        st.configure("Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat",
                     font=(ff, 9, "bold"), padding=8)
        st.map("Treeview", background=[("selected", "#F0DED6")], foreground=[("selected", FG)])
        st.configure("Horizontal.TProgressbar", troughcolor=PANEL, background=ACCENT, borderwidth=0,
                     lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, arrowcolor=MUTED,
                     bordercolor=BG, lightcolor=PANEL, darkcolor=PANEL)

    def card(self, parent, title, subtitle=None):
        outer = ttk.Frame(parent, style="Card.TFrame", padding=18)
        ttk.Label(outer, text=title, style="CardH.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(outer, text=subtitle, style="CardMuted.TLabel").pack(anchor="w")
        inner = ttk.Frame(outer, style="Card.TFrame")
        inner.pack(fill="x", pady=(10, 0))
        return outer, inner

    # ---- Aufbau ----
    def build_ui(self):
        head = ttk.Frame(self, padding=(26, 20, 26, 10))
        head.pack(fill="x")
        self.theme_btn = ttk.Button(
            head, text=("☀  Light Mode" if self.dark_mode else "☾  Dark Mode"), command=self.toggle_theme
        )
        self.theme_btn.pack(side="right", anchor="ne", padx=(12, 0))
        self.activity_lbl = ttk.Label(head, text="✦  STUDIO BEREIT", style="Eyebrow.TLabel")
        self.activity_lbl.pack(side="right", anchor="ne", padx=(12, 14), pady=(7, 0))
        ttk.Label(head, text="OPENROUTER  ·  MULTI-AGENT WORKSPACE", style="Eyebrow.TLabel").pack(anchor="w")
        ttk.Label(head, text="KI-Team", style="H1.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(head, text="Ein Team aus KIs. Ein gemeinsames Projekt. Ein klarer Workflow.",
                  style="Muted.TLabel").pack(anchor="w", pady=(1, 0))
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=24, pady=(8, 20))
        self.tab_setup = ttk.Frame(nb, padding=(0, 14, 0, 0))
        self.tab_run = ttk.Frame(nb, padding=(0, 14, 0, 0))
        nb.add(self.tab_setup, text="  1 · Einrichtung  ")
        nb.add(self.tab_run, text="  2 · Projekt & Lauf  ")
        self.nb = nb
        self.build_setup()
        self.build_run()

    def build_setup(self):
        p = self.tab_setup
        # Keys
        outer, inner = self.card(p, "API-Keys",
                                 "Drei Key-Felder. Du kannst überall denselben Key eintragen – ein OpenRouter-Key "
                                 "reicht für alle Modelle. Keys erstellst du auf openrouter.ai/keys.")
        outer.pack(fill="x", pady=(0, 12))
        self.key_vars = []
        self.key_entries = []
        for i in range(NUM_KEYS):
            ttk.Label(inner, text="Key %d" % (i + 1), style="Card.TLabel", width=7).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar()
            e = ttk.Entry(inner, textvariable=v, show="•", width=70)
            e.grid(row=i, column=1, sticky="we", pady=3, padx=(0, 10))
            self.key_vars.append(v)
            self.key_entries.append(e)
        inner.columnconfigure(1, weight=1)
        self.show_keys = False
        self.btn_show = ttk.Button(inner, text="Keys anzeigen", command=self.toggle_keys)
        self.btn_show.grid(row=0, column=2, padx=(0, 0))
        self.save_keys_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inner, text="Keys auf diesem Rechner speichern (~/.config/ki-team, nur für dich lesbar)",
                        variable=self.save_keys_var, style="Card.TCheckbutton").grid(
            row=NUM_KEYS, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # KI-Slots
        outer, inner = self.card(p, "KIs und ihre Aufgaben",
                                 "Pro KI: Modell und Key wählen, dann ankreuzen, wofür sie eingesetzt wird. "
                                 "Planer ist genau eine KI, Bauen und Debuggen können mehrere sein.")
        outer.pack(fill="x", pady=(0, 12))
        heads = ["", "Name", "Modell auswählen", "Key", "Zusatzhinweis (optional)", "Planer", "Bauen", "Debuggen"]
        for c, h in enumerate(heads):
            ttk.Label(inner, text=h, style="CardMuted.TLabel").grid(row=0, column=c, sticky="w", padx=4, pady=(0, 4))
        self.name_vars, self.model_vars, self.keysel_vars, self.role_vars = [], [], [], []
        self.build_vars, self.debug_vars = [], []
        self.planner_var = tk.IntVar(value=0)
        self.model_boxes = []
        self.dot_labels = []
        for i in range(NUM_SLOTS):
            r = i + 1
            dot = tk.Label(inner, text="●", bg=CARD, fg=SLOT_COLORS[i], font=(self.ff, 14))
            dot.grid(row=r, column=0, padx=(0, 4))
            self.dot_labels.append(dot)
            nv = tk.StringVar()
            ttk.Entry(inner, textvariable=nv, width=13).grid(row=r, column=1, padx=4, pady=4, sticky="w")
            mv = tk.StringVar()
            cb = ttk.Combobox(inner, textvariable=mv, width=42, values=[AUTO_MODEL_LABEL], state="readonly")
            cb.grid(row=r, column=2, padx=4, pady=5, sticky="we")
            # Zusätzlich zum StringVar-Trace direkt auf eine bestätigte Auswahl reagieren.
            cb.bind("<<ComboboxSelected>>", self.schedule_autosave)
            kv = tk.StringVar()
            ttk.Combobox(inner, textvariable=kv, width=7, state="readonly",
                         values=["Key %d" % (k + 1) for k in range(NUM_KEYS)]).grid(row=r, column=3, padx=4, pady=4)
            rv = tk.StringVar()
            ttk.Entry(inner, textvariable=rv, width=28).grid(row=r, column=4, padx=4, pady=4, sticky="we")
            ttk.Radiobutton(inner, variable=self.planner_var, value=i, style="Card.TRadiobutton").grid(row=r, column=5, padx=14)
            bv = tk.BooleanVar()
            ttk.Checkbutton(inner, variable=bv, style="Card.TCheckbutton").grid(row=r, column=6, padx=14)
            dv = tk.BooleanVar()
            ttk.Checkbutton(inner, variable=dv, style="Card.TCheckbutton").grid(row=r, column=7, padx=14)
            self.name_vars.append(nv)
            self.model_vars.append(mv)
            self.keysel_vars.append(kv)
            self.role_vars.append(rv)
            self.build_vars.append(bv)
            self.debug_vars.append(dv)
            self.model_boxes.append(cb)
        inner.columnconfigure(2, weight=1)
        bar = ttk.Frame(inner, style="Card.TFrame")
        bar.grid(row=NUM_SLOTS + 1, column=0, columnspan=8, sticky="we", pady=(10, 0))
        self.btn_models = ttk.Button(bar, text="↻  Gratis-Modelle aktualisieren", command=self.load_models)
        self.btn_models.pack(side="left")
        self.models_lbl = ttk.Label(bar, text="Lade aktuelle kostenlose Chat-Modelle von OpenRouter …",
                                    style="CardMuted.TLabel")
        self.models_lbl.pack(side="left")
        self.autosave_lbl = ttk.Label(bar, text="KI-Auswahl wird automatisch gespeichert",
                                      style="CardMuted.TLabel")
        self.autosave_lbl.pack(side="right", padx=(10, 12))
        ttk.Button(bar, text="✓  Auswahl speichern", command=self.save_setup_now).pack(side="right")

        # Optionen
        outer, inner = self.card(p, "Ablauf-Optionen")
        outer.pack(fill="x")
        self.parallel_var = tk.BooleanVar()
        ttk.Checkbutton(inner, text="Beim Bauen mehrere KIs gleichzeitig arbeiten lassen (schneller, aber mehr Anfragen auf einmal)",
                        variable=self.parallel_var, style="Card.TCheckbutton").grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 8))
        self.pause_var = tk.StringVar()
        self.maxcalls_var = tk.StringVar()
        self.rounds_var = tk.StringVar()
        self.timeout_var = tk.StringVar()
        opts = [("Pause zwischen Anfragen (Sek.)", self.pause_var, 0, 120, 0.5),
                ("Max. Anfragen pro Lauf", self.maxcalls_var, 1, 2000, 10),
                ("Debug-Runden", self.rounds_var, 1, 20, 1),
                ("Test-Zeitlimit (Sek.)", self.timeout_var, 3, 600, 5)]
        for n, (label, var, lo, hi, step) in enumerate(opts):
            ttk.Label(inner, text=label, style="Card.TLabel").grid(row=1, column=n * 2, sticky="w", padx=(0, 8))
            ttk.Spinbox(inner, from_=lo, to=hi, increment=step, width=7, textvariable=var).grid(
                row=1, column=n * 2 + 1, padx=(0, 24))

        # Planungsstil und Multi-Agent-Meeting
        outer, inner = self.card(
            p, "Planung & Team-Meeting",
            "Die KIs beraten sich vor dem Plan. Du entscheidest, ob der Planner Rückfragen stellen oder selbstständig handeln soll."
        )
        outer.pack(fill="x", pady=(12, 0))
        self.planning_mode_var = tk.StringVar(value=self.cfg.get("planning_mode", "guided"))
        ttk.Radiobutton(
            inner, text="Geführte Planung – Fragen mit KI-Empfehlung an mich stellen",
            variable=self.planning_mode_var, value="guided", style="Card.TRadiobutton"
        ).pack(anchor="w", pady=3)
        ttk.Radiobutton(
            inner, text="Automatikmodus – die KI entscheidet selbst und dokumentiert ihre Annahmen",
            variable=self.planning_mode_var, value="auto", style="Card.TRadiobutton"
        ).pack(anchor="w", pady=3)
        self.team_meeting_var = tk.BooleanVar(value=self.cfg.get("team_meeting", True))
        ttk.Checkbutton(
            inner, text="Team-Meeting vor der Planung aktivieren (alle ausgewählten KIs beraten sich nacheinander)",
            variable=self.team_meeting_var, style="Card.TCheckbutton"
        ).pack(anchor="w", pady=(6, 2))

    def build_run(self):
        p = self.tab_run
        outer, inner = self.card(p, "Projekt")
        outer.pack(fill="x", pady=(0, 12))
        inner.columnconfigure(1, weight=1)
        ttk.Label(inner, text="Projektordner", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=4)
        self.folder_var = tk.StringVar()
        ttk.Entry(inner, textvariable=self.folder_var).grid(row=0, column=1, sticky="we", pady=4)
        fb = ttk.Frame(inner, style="Card.TFrame")
        fb.grid(row=0, column=2, padx=(10, 0))
        ttk.Button(fb, text="Auswählen …", command=self.pick_folder).pack(side="left")
        ttk.Button(fb, text="Öffnen", command=self.open_folder).pack(side="left", padx=(6, 0))
        ttk.Label(inner, text="Was soll gebaut werden?", style="Card.TLabel").grid(row=1, column=0, sticky="nw", padx=(0, 12), pady=4)
        self.task_text = tk.Text(inner, height=5, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", wrap="word",
                                 padx=10, pady=8, font=(self.ff, 10), highlightthickness=0)
        self.task_text.grid(row=1, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Label(inner, text="Testbefehl (optional)", style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=4)
        self.cmd_var = tk.StringVar()
        ttk.Entry(inner, textvariable=self.cmd_var).grid(row=2, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Label(inner, text="Muss sich selbst beenden, z. B.  python3 main.py --test  oder  python3 -m py_compile *.py. "
                              "Leer = in Phase 3 nur ein Code-Review. Er läuft im Projektordner.",
                  style="CardMuted.TLabel").grid(row=3, column=1, columnspan=2, sticky="w")

        ctl = ttk.Frame(p)
        ctl.pack(fill="x", pady=(0, 12))
        self.do_plan_var = tk.BooleanVar(value=True)
        self.do_build_var = tk.BooleanVar(value=True)
        self.do_debug_var = tk.BooleanVar(value=True)
        box = ttk.Frame(ctl, style="Card.TFrame", padding=(14, 8))
        box.pack(side="left")
        self.phase_lbls = {}
        for key, text, var in (("plan", "1 Planen", self.do_plan_var), ("build", "2 Bauen", self.do_build_var),
                               ("debug", "3 Debuggen", self.do_debug_var)):
            f = ttk.Frame(box, style="Card.TFrame")
            f.pack(side="left", padx=(0, 18))
            ttk.Checkbutton(f, variable=var, style="Card.TCheckbutton").pack(side="left")
            lbl = tk.Label(f, text=text, bg=CARD, fg=MUTED, font=(self.ff, 10, "bold"))
            lbl.pack(side="left")
            self.phase_lbls[key] = lbl
        self.start_btn = ttk.Button(ctl, text="▶  Start", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left", padx=(16, 8))
        self.stop_btn = ttk.Button(ctl, text="■  Stopp", style="Stop.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        right = ttk.Frame(ctl)
        right.pack(side="right", fill="x", expand=True, padx=(20, 0))
        self.status_lbl = ttk.Label(right, text="Bereit.", style="Muted.TLabel")
        self.status_lbl.pack(anchor="e")
        self.progress = ttk.Progressbar(right, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(4, 0))
        self.calls_lbl = ttk.Label(right, text="Anfragen: 0", style="Muted.TLabel")
        self.calls_lbl.pack(anchor="e", pady=(2, 0))

        pan = ttk.PanedWindow(p, orient="horizontal")
        pan.pack(fill="both", expand=True)
        left = ttk.Frame(pan)
        pan.add(left, weight=2)
        self.tree = ttk.Treeview(left, columns=("id", "title", "status", "owner"), show="headings", selectmode="browse")
        for c, t, w in (("id", "#", 36), ("title", "Aufgabe", 250), ("status", "Status", 80), ("owner", "KI", 100)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w", stretch=(c == "title"))
        self.tree.tag_configure("fertig", foreground=OK)
        self.tree.tag_configure("in Arbeit", foreground=ACCENT2)
        self.tree.tag_configure("Fehler", foreground=ERR)
        self.tree.tag_configure("offen", foreground=MUTED)
        self.tree.pack(fill="both", expand=True)
        rightf = ttk.Frame(pan)
        pan.add(rightf, weight=3)
        self.logbox = tk.Text(rightf, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", wrap="word",
                              padx=12, pady=10, font=(self.fm, 10), state="disabled", highlightthickness=0)
        sb = ttk.Scrollbar(rightf, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(side="left", fill="both", expand=True)
        for i, c in enumerate(SLOT_COLORS):
            self.logbox.tag_configure("slot%d" % i, foreground=c)
        self.logbox.tag_configure("sys", foreground="#aab0c6")
        self.logbox.tag_configure("warn", foreground=WARN)
        self.logbox.tag_configure("err", foreground=ERR)
        self.logbox.tag_configure("ok", foreground=OK)
        self.logbox.tag_configure("phase", foreground=ACCENT2, font=(self.fm, 10, "bold"))
        self.logbox.tag_configure("time", foreground=MUTED)

    # ---- Konfiguration <-> Oberflaeche ----
    def enable_autosave(self):
        """Speichert Änderungen an Modell-Auswahl und Einrichtung automatisch."""
        self._autosave_enabled = True
        self._autosave_job = None
        variables = []
        for group_name in ("key_vars", "name_vars", "model_vars", "keysel_vars", "role_vars",
                           "build_vars", "debug_vars"):
            variables.extend(getattr(self, group_name, []))
        variables.extend([
            self.save_keys_var, self.planner_var, self.parallel_var,
            self.pause_var, self.maxcalls_var, self.rounds_var, self.timeout_var,
            self.folder_var, self.cmd_var, self.do_plan_var, self.do_build_var, self.do_debug_var,
            self.planning_mode_var, self.team_meeting_var,
        ])
        for variable in variables:
            variable.trace_add("write", self.schedule_autosave)
        self.task_text.bind("<<Modified>>", self.on_task_text_modified)
        self.task_text.edit_modified(False)

    def schedule_autosave(self, *_args):
        if not getattr(self, "_autosave_enabled", False):
            return
        pending = getattr(self, "_autosave_job", None)
        if pending is not None:
            try:
                self.after_cancel(pending)
            except tk.TclError:
                pass
        self._autosave_job = self.after(350, self._autosave_now)
        if hasattr(self, "autosave_lbl"):
            self.autosave_lbl.configure(text="Änderungen …")

    def on_task_text_modified(self, _event=None):
        if self.task_text.edit_modified():
            self.task_text.edit_modified(False)
            self.schedule_autosave()

    def _autosave_now(self):
        self._autosave_job = None
        if not getattr(self, "_autosave_enabled", False):
            return
        try:
            self.cfg = self.collect_cfg()
            save_config(self.cfg)
            if hasattr(self, "autosave_lbl") and self.autosave_lbl.winfo_exists():
                self.autosave_lbl.configure(text="Auswahl automatisch gespeichert ✓")
        except (OSError, tk.TclError, ValueError, AttributeError) as exc:
            # Details bleiben in der Konsole nachvollziehbar; keine Popup-Flut beim Tippen.
            print("KI-Team: automatische Speicherung fehlgeschlagen: %s" % exc, file=sys.stderr)
            try:
                if hasattr(self, "autosave_lbl") and self.autosave_lbl.winfo_exists():
                    self.autosave_lbl.configure(text="Speichern fehlgeschlagen")
            except tk.TclError:
                pass

    def save_setup_now(self):
        try:
            self.cfg = self.collect_cfg()
            save_config(self.cfg)
            self.autosave_lbl.configure(text="Auswahl gespeichert ✓")
        except (OSError, tk.TclError, ValueError) as exc:
            messagebox.showwarning(APP_NAME, "Die Einrichtung konnte nicht gespeichert werden:\n%s" % exc)

    def load_into_ui(self):
        c = self.cfg
        for i in range(NUM_KEYS):
            self.key_vars[i].set(c["keys"][i])
        self.save_keys_var.set(bool(c["save_keys"]))
        for i in range(NUM_SLOTS):
            s = c["slots"][i]
            self.name_vars[i].set(s.get("name", "KI %d" % (i + 1)))
            saved_model = s.get("model", "")
            self.model_vars[i].set(self.model_id_to_label.get(saved_model, saved_model))
            k = min(max(to_int(s.get("key", 0), 0), 0), NUM_KEYS - 1)
            self.keysel_vars[i].set("Key %d" % (k + 1))
            self.role_vars[i].set(s.get("role", ""))
            self.build_vars[i].set(i in c["builders"])
            self.debug_vars[i].set(i in c["debuggers"])
        self.planner_var.set(to_int(c["planner"], 0))
        self.folder_var.set(c["folder"])
        self.task_text.delete("1.0", "end")
        self.task_text.insert("1.0", c["task"])
        self.cmd_var.set(c["run_cmd"])
        self.do_plan_var.set(bool(c["do_plan"]))
        self.do_build_var.set(bool(c["do_build"]))
        self.do_debug_var.set(bool(c["do_debug"]))
        self.parallel_var.set(bool(c["parallel"]))
        self.pause_var.set(str(c["pause"]))
        self.maxcalls_var.set(str(c["max_calls"]))
        self.rounds_var.set(str(c["debug_rounds"]))
        self.timeout_var.set(str(c["run_timeout"]))
        self.planning_mode_var.set(c.get("planning_mode", "guided"))
        self.team_meeting_var.set(bool(c.get("team_meeting", True)))

    def collect_cfg(self):
        c = self.cfg
        c["keys"] = [v.get().strip() for v in self.key_vars]
        c["save_keys"] = self.save_keys_var.get()
        for i in range(NUM_SLOTS):
            s = c["slots"][i]
            s["name"] = self.name_vars[i].get().strip() or "KI %d" % (i + 1)
            selected_model = self.model_vars[i].get().strip()
            s["model"] = self.model_label_to_id.get(selected_model, selected_model)
            s["key"] = min(max(to_int(self.keysel_vars[i].get().replace("Key", ""), 1) - 1, 0), NUM_KEYS - 1)
            s["role"] = self.role_vars[i].get().strip()
        c["planner"] = self.planner_var.get()
        c["builders"] = [i for i in range(NUM_SLOTS) if self.build_vars[i].get()]
        c["debuggers"] = [i for i in range(NUM_SLOTS) if self.debug_vars[i].get()]
        c["folder"] = os.path.expanduser(self.folder_var.get().strip())
        c["task"] = self.task_text.get("1.0", "end").strip()
        c["run_cmd"] = self.cmd_var.get().strip()
        c["do_plan"] = self.do_plan_var.get()
        c["do_build"] = self.do_build_var.get()
        c["do_debug"] = self.do_debug_var.get()
        c["parallel"] = self.parallel_var.get()
        c["pause"] = max(0.0, to_float(self.pause_var.get(), 2.0))
        c["max_calls"] = max(1, to_int(self.maxcalls_var.get(), 150))
        c["debug_rounds"] = max(1, to_int(self.rounds_var.get(), 4))
        c["run_timeout"] = max(3, to_int(self.timeout_var.get(), 30))
        c["planning_mode"] = self.planning_mode_var.get() if self.planning_mode_var.get() in ("guided", "auto") else "guided"
        c["team_meeting"] = bool(self.team_meeting_var.get())
        c["dark_mode"] = bool(self.dark_mode)
        return c

    # ---- Aktionen ----
    def toggle_keys(self):
        self.show_keys = not self.show_keys
        for e in self.key_entries:
            e.configure(show="" if self.show_keys else "•")
        self.btn_show.configure(text="Keys verbergen" if self.show_keys else "Keys anzeigen")

    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or os.path.expanduser("~"),
                                    title="Projektordner wählen")
        if d:
            self.folder_var.set(d)

    def open_folder(self):
        d = self.folder_var.get().strip()
        if d and os.path.isdir(os.path.expanduser(d)):
            try:
                subprocess.Popen(["xdg-open", os.path.expanduser(d)])
            except OSError:
                messagebox.showinfo(APP_NAME, "Ordner: " + d)

    def model_pool(self):
        # openrouter/free ist ein echter Router-Endpunkt: Er wählt bei jeder
        # Anfrage automatisch ein dazu passendes kostenloses Chatmodell.
        self.model_label_to_id = {AUTO_MODEL_LABEL: "openrouter/free"}
        self.model_id_to_label = {"openrouter/free": AUTO_MODEL_LABEL}
        labels = [AUTO_MODEL_LABEL]
        for model_id, is_free in self.models:
            if not is_free:
                continue
            label = display_model_name(model_id)
            if label in self.model_label_to_id:
                label += " · " + model_id.split(":", 1)[0].split("/", 1)[-1]
            self.model_label_to_id[label] = model_id
            self.model_id_to_label[model_id] = label
            labels.append(label)
        return labels

    def refresh_model_lists(self):
        pool = self.model_pool()
        for cb in self.model_boxes:
            cb["values"] = pool[:25]
        # Bestehende Einstellungen speichern Modell-IDs; in der Oberfläche
        # zeigen wir nach dem Laden die besser lesbaren Modellnamen.
        for var in getattr(self, "model_vars", []):
            current = var.get().strip()
            if current in self.model_id_to_label:
                var.set(self.model_id_to_label[current])
        if self.models:
            count = len([1 for _, free in self.models if free])
            self.models_lbl.configure(
                text="%d kostenlose Chat-Modelle + Auto-Free-Router · keine Spezial-Endpunkte" % count)
        else:
            self.models_lbl.configure(text="Noch keine Modelle geladen. Bitte OpenRouter-Verbindung prüfen.")

    def load_models(self):
        self.btn_models.configure(state="disabled")
        self.models_lbl.configure(text="Hole aktuelle Gratis-Modelle von OpenRouter …")

        def work():
            try:
                self.q.put(("models", fetch_models()))
            except ApiError as e:
                self.q.put(("models_err", explain(e)))
        threading.Thread(target=work, daemon=True).start()

    def show_planning_questions(self, questions):
        """Modaler Rückfrage-Dialog: Empfehlung wird vorausgefüllt, bleibt aber editierbar."""
        win = tk.Toplevel(self)
        win.title("KI-Team · Planungsfragen")
        win.transient(self)
        win.grab_set()
        win.geometry("760x560")
        win.minsize(640, 440)
        win.configure(bg=BG)
        outer = ttk.Frame(win, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="KURZES PLANUNGSGESPRÄCH", style="Eyebrow.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Der Planner hat ein paar Entscheidungen vorbereitet.",
                  style="CardH.TLabel").pack(anchor="w", pady=(5, 2))
        ttk.Label(outer, text="Die KI-Empfehlung ist bereits eingetragen. Du kannst sie übernehmen oder ändern.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        entries = []
        for n, item in enumerate(questions, 1):
            card, inner = self.card(body, "%d · %s" % (n, item["question"]))
            card.pack(fill="x", pady=(0, 10))
            ttk.Label(inner, text="✦ Empfehlung des Planners: " + (item.get("recommendation") or "Keine feste Empfehlung"),
                      style="CardMuted.TLabel", wraplength=680).pack(anchor="w", pady=(0, 5))
            if item.get("options"):
                ttk.Label(inner, text="Mögliche Optionen: " + "  ·  ".join(item["options"]),
                          style="CardMuted.TLabel", wraplength=680).pack(anchor="w", pady=(0, 5))
            var = tk.StringVar(value=item.get("recommendation", ""))
            ttk.Entry(inner, textvariable=var).pack(fill="x")
            entries.append(var)
        result = {"value": None}

        def finish(use_auto=False):
            if use_auto:
                result["value"] = "__AUTO__"
            else:
                lines = []
                for n, (item, var) in enumerate(zip(questions, entries), 1):
                    value = var.get().strip() or item.get("recommendation", "")
                    lines.append("%d. %s\nAntwort: %s" % (n, item["question"], value or "(keine Präferenz)") )
                result["value"] = "\n\n".join(lines)
            win.grab_release()
            win.destroy()

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Automatik für diesen Auftrag", command=lambda: finish(True)).pack(side="left")
        ttk.Button(buttons, text="Empfehlungen übernehmen", command=finish).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Antworten übernehmen", style="Accent.TButton", command=finish).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", finish)
        win.wait_window()
        return result["value"] or ""

    def start(self):
        if self.running:
            return
        cfg = self.collect_cfg()
        try:
            save_config(cfg)
        except OSError as e:
            messagebox.showwarning(APP_NAME, "Einstellungen konnten nicht gespeichert werden: %s" % e)
        err = validate_cfg(cfg)
        if err:
            messagebox.showwarning(APP_NAME, err)
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_lbl.configure(text="Läuft …")
        self.progress.configure(value=0)
        self.calls_lbl.configure(text="Anfragen: 0")
        self.engine = Engine(cfg, lambda kind, payload: self.q.put((kind, payload)))
        self.log("phase", "—— Start: %s ——" % self.engine.root)
        threading.Thread(target=self.engine.run, daemon=True).start()

    def stop(self):
        if self.engine and self.running:
            self.status_lbl.configure(text="Stoppe …")
            self.engine.stop()

    def on_close(self):
        if self.running:
            if not messagebox.askyesno(APP_NAME, "Es läuft noch ein Projekt. Wirklich beenden?"):
                return
            if self.engine:
                self.engine.stop()
        pending = getattr(self, "_autosave_job", None)
        if pending is not None:
            try:
                self.after_cancel(pending)
            except tk.TclError:
                pass
        try:
            save_config(self.collect_cfg())
        except OSError as exc:
            messagebox.showwarning(APP_NAME, "Einstellungen konnten nicht gespeichert werden:\n%s" % exc)
        self._autosave_enabled = False
        self.destroy()

    # ---- Ereignisse aus dem Hintergrund ----
    def poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self.handle(kind, payload)
        except queue.Empty:
            pass
        self.after(120, self.poll)

    def handle(self, kind, payload):
        if kind == "log":
            self.log(payload[0], payload[1])
        elif kind == "phase":
            self.set_phase(payload)
        elif kind == "tasks":
            self.show_tasks(payload)
        elif kind == "calls":
            self.calls_lbl.configure(text="Anfragen: %d" % payload)
        elif kind == "finished":
            self.running = False
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.status_lbl.configure(text=payload)
            self.log("ok" if payload.startswith("Fertig") else "warn", "—— %s ——" % payload)
        elif kind == "models":
            self.models = payload
            self.btn_models.configure(state="normal")
            self.refresh_model_lists()
        elif kind == "questions":
            answer = self.show_planning_questions(payload)
            if self.engine:
                self.engine.provide_user_answers(answer)
        elif kind == "models_err":
            self.btn_models.configure(state="normal")
            self.models_lbl.configure(text="OpenRouter nicht erreichbar: %s" % payload)

    def log(self, tag, text):
        t = self.logbox
        t.configure(state="normal")
        at_end = t.yview()[1] >= 0.98
        t.insert("end", ts() + "  ", "time")
        t.insert("end", text.rstrip() + "\n", tag)
        if at_end:
            t.see("end")
        t.configure(state="disabled")

    def set_phase(self, name):
        order = ["plan", "build", "debug"]
        enabled = {"plan": self.do_plan_var.get(), "build": self.do_build_var.get(), "debug": self.do_debug_var.get()}
        for n, lbl in self.phase_lbls.items():
            if name == "done":
                lbl.configure(fg=OK if enabled[n] else MUTED)
            elif n == name:
                lbl.configure(fg=ACCENT2)
            elif name in order and order.index(n) < order.index(name) and enabled[n]:
                lbl.configure(fg=OK)
            else:
                lbl.configure(fg=MUTED)
        labels = {"plan": "Plant …", "build": "Baut …", "debug": "Debuggt …", "done": ""}
        if name != "done":
            self.status_lbl.configure(text=labels.get(name, ""))

    def show_tasks(self, tasks):
        self.tree.delete(*self.tree.get_children())
        for t in tasks:
            self.tree.insert("", "end", values=(t["id"], t["title"], t["status"], t["owner"]), tags=(t["status"],))
        total = len(tasks)
        done = len([t for t in tasks if t["status"] in ("fertig", "Fehler")])
        self.progress.configure(value=(100.0 * done / total) if total else 0)


def main():
    if tk is None:
        print("tkinter fehlt. Installieren mit:  sudo apt install python3-tk")
        sys.exit(1)
    App().mainloop()


if __name__ == "__main__":
    main()