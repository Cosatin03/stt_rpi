#!/usr/bin/env python3
import json
import logging
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from typing import Literal

from flask import Flask, Response, jsonify, request
import needle
import sounddevice as sd
import vosk
from vosk import KaldiRecognizer, Model

# Standard-Logs drosseln
vosk.SetLogLevel(-1)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BASE_DIR, "tool_scripte")
os.makedirs(SCRIPTS_DIR, exist_ok=True)

DEVICES_FILE = "devices.json"
STATE_FILE = "device_states.json"
LAYOUT_FILE = "layout.json"
BLACKLIST_FILE = "blacklist.txt"
REPLACEMENTS_FILE = "replacements.txt"
NEEDLE_CONFIG_FILE = "needle_config.json"
LOG_CONFIG_FILE = "log_settings.json"
VOSK_MODEL_PATH = "model"
VENV_PATH = "/home/pi/stt/venv"

WAKEWORDS = ["computer", "hey computer", "jarvis"]
WAKE_TIMEOUT_SECONDS = 6.0

_hardware_lock = threading.Lock()
_log_lock = threading.Lock()
audio_queue = queue.Queue()
system_logs = []
_log_counter = 0

# --- Standard Log-Einstellungen ---
DEFAULT_LOG_CONFIG = {
    "log_level": "INFO",          # DEBUG, INFO, WARN, ERROR, OFF
    "terminal_logging": True,     # Logs im Terminal ausgeben
    "vosk_verbose": False,        # Vosk C++ Logs aktivieren
    "log_stt_raw": False,         # Standard: AUS
    "log_wakeword_checks": False
}

LEVEL_WEIGHTS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "OFF": 999}


def load_json(filepath, default_val):
    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(default_val, f, indent=2, ensure_ascii=False)
        return default_val
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if data is not None else default_val
    except Exception:
        return default_val


def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_log_config():
    cfg = load_json(LOG_CONFIG_FILE, DEFAULT_LOG_CONFIG)
    for k, v in DEFAULT_LOG_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    return cfg


def apply_vosk_log_level():
    cfg = get_log_config()
    if cfg.get("vosk_verbose", False) and cfg.get("log_level", "INFO") != "OFF":
        vosk.SetLogLevel(0)
    else:
        vosk.SetLogLevel(-1)


apply_vosk_log_level()


def push_log(author, text, extra="", level="INFO"):
    global _log_counter
    cfg = get_log_config()
    current_min_level = cfg.get("log_level", "INFO").upper()

    if current_min_level == "OFF":
        return

    msg_weight = LEVEL_WEIGHTS.get(level.upper(), 20)
    min_weight = LEVEL_WEIGHTS.get(current_min_level, 20)

    if msg_weight < min_weight:
        return

    if cfg.get("terminal_logging", True):
        color_prefix = {
            "DEBUG": "\033[90m",
            "INFO": "\033[36m",
            "WARN": "\033[33m",
            "ERROR": "\033[31m"
        }.get(level.upper(), "\033[0m")
        reset_c = "\033[0m"
        timestamp_str = time.strftime("%H:%M:%S")
        print(f"{color_prefix}[{timestamp_str}][{level.upper()}][{author}] {text}{reset_c}")
        if extra:
            print(f"       -> Details: {extra}")

    with _log_lock:
        _log_counter += 1
        entry = {
            "id": _log_counter,
            "author": author,
            "text": text,
            "extra": extra,
            "level": level.upper(),
            "time": time.strftime("%H:%M:%S")
        }
        system_logs.append(entry)
        if len(system_logs) > 300:
            system_logs.pop(0)


vosk_model = None
if os.path.exists(VOSK_MODEL_PATH):
    try:
        vosk_model = Model(VOSK_MODEL_PATH)
        push_log("System", f"Vosk-Modell '{VOSK_MODEL_PATH}' geladen.", level="INFO")
    except Exception as e:
        push_log("Fehler", f"Vosk-Modell Fehler: {e}", level="ERROR")
else:
    push_log("System", f"Vosk-Pfad '{VOSK_MODEL_PATH}' nicht gefunden. STT inaktiv.", level="WARN")


def get_devices():
    devices = load_json(DEVICES_FILE, [])
    if not isinstance(devices, list):
        devices = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        if "actions" not in d or not d["actions"]:
            d["actions"] = ["on", "off", "status"]
        if "command" not in d or not d["command"]:
            d["command"] = f"python3 tool_scripte/steckdose.py --device {d.get('id', 'dev')} --action {{action}}"
        if "description" not in d:
            d["description"] = ""
    return devices


def get_states():
    return load_json(STATE_FILE, {})


def get_layout():
    data = load_json(LAYOUT_FILE, {"rooms": [], "placements": []})
    if not isinstance(data, dict):
        data = {"rooms": [], "placements": []}
    if "rooms" not in data or not isinstance(data["rooms"], list):
        data["rooms"] = []
    if "placements" not in data or not isinstance(data["placements"], list):
        data["placements"] = []
    return data


def get_needle_config():
    default_cfg = {
        "tool_description": "Switch, query or control smart-home devices, rooms, or categories."
    }
    return load_json(NEEDLE_CONFIG_FILE, default_cfg)


def update_state(device_id, action):
    if action not in ["on", "off"]:
        return
    states = get_states()
    states[device_id] = action
    save_json(STATE_FILE, states)


def load_replacements(filepath=REPLACEMENTS_FILE):
    replacements = []
    if not os.path.exists(filepath):
        return replacements
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "->" in line:
                s, t = line.split("->", 1)
                replacements.append((s.strip().lower(), t.strip()))
    replacements.sort(key=lambda x: len(x[0]), reverse=True)
    return replacements


def load_blacklist(filepath=BLACKLIST_FILE):
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


def clean_and_correct(text):
    text = text.strip().lower()
    if not text:
        return ""

    bl = load_blacklist()
    rep = load_replacements()

    if text in bl:
        return ""

    for src, target in rep:
        pattern = r"\b" + re.escape(src) + r"\b"
        text = re.sub(pattern, target, text)

    words = text.split()
    while words and words[0].lower() in bl:
        words.pop(0)
    words = [w for w in words if w.lower() not in bl]
    return " ".join(words).strip()


def get_venv_env():
    env = os.environ.copy()
    if os.path.exists(VENV_PATH):
        env["VIRTUAL_ENV"] = VENV_PATH
        env["PATH"] = f"{os.path.join(VENV_PATH, 'bin')}:{env.get('PATH', '')}"
    return env


def execute_device_command(device_id, action, custom_params=None):
    devices = get_devices()
    dev = next((d for d in devices if d.get("id") == device_id), None)
    cmd_tpl = dev.get("command") if dev and dev.get("command") else f"python3 tool_scripte/steckdose.py --device {device_id} --action {{action}}"

    params = {"action": action, "device": device_id}
    if custom_params and isinstance(custom_params, dict):
        params.update(custom_params)

    formatted_cmd = cmd_tpl
    for k, v in params.items():
        formatted_cmd = formatted_cmd.replace(f"{{{k}}}", str(v))

    if "{action}" in formatted_cmd:
        formatted_cmd = formatted_cmd.replace("{action}", action)

    push_log("Hardware", f"Ausführung: {formatted_cmd}", level="INFO")
    try:
        proc = subprocess.run(
            shlex.split(formatted_cmd),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env=get_venv_env(),
            cwd=BASE_DIR
        )
        stdout = proc.stdout.strip()
        try:
            data = json.loads(stdout)
        except Exception:
            data = stdout or proc.stderr.strip()

        if proc.returncode == 0:
            if isinstance(data, dict) and "state" in data and data["state"] in ["on", "off"]:
                update_state(device_id, data["state"])
            elif action in ["on", "off"]:
                update_state(device_id, action)
            push_log("Hardware", f"Erfolg ({device_id} -> {action})", extra=str(data), level="DEBUG")
        else:
            push_log("Hardware", f"Fehlercode {proc.returncode} bei {device_id}", extra=proc.stderr.strip(), level="WARN")

        return {"success": proc.returncode == 0, "device": device_id, "action": action, "output": data}
    except Exception as ex:
        push_log("Hardware", f"Ausnahmefehler bei {device_id}: {ex}", level="ERROR")
        return {"success": False, "device": device_id, "error": str(ex)}


def get_devices_in_room(room_obj):
    layout = get_layout()
    placements = layout.get("placements", [])
    all_devices = {d["id"]: d for d in get_devices()}

    rx = room_obj.get("x", 0)
    ry = room_obj.get("y", 0)
    rw = room_obj.get("w", 100)
    rh = room_obj.get("h", 100)

    room_devices = []
    for p in placements:
        dev_id = p.get("device_id")
        if dev_id not in all_devices:
            continue
        px = p.get("x", 0) + 32
        py = p.get("y", 0) + 32
        if rx <= px <= rx + rw and ry <= py <= ry + rh:
            room_devices.append(all_devices[dev_id])

    r_name = room_obj.get("name", "").lower().strip()
    if r_name:
        for dev in all_devices.values():
            if dev not in room_devices:
                aliases = [a.lower() for a in dev.get("aliases", [])]
                d_name = dev.get("name", "").lower()
                d_id = dev.get("id", "").lower()
                if r_name in d_name or r_name in d_id or any(r_name in a for a in aliases):
                    room_devices.append(dev)

    return room_devices


def resolve_devices(device_query: str, full_text: str = ""):
    devices = get_devices()
    combined = f"{device_query} {full_text}".lower().strip()
    layout = get_layout()
    rooms = layout.get("rooms", [])

    matched_room = None
    for r in rooms:
        r_name = r.get("name", "").strip().lower()
        if not r_name:
            continue
        if r_name in combined:
            matched_room = r
            break
        if "wohn" in r_name and ("wohn" in combined or "living" in combined):
            matched_room = r
            break
        if "schlaf" in r_name and ("schlaf" in combined or "bed" in combined or "bett" in combined):
            matched_room = r
            break
        if "bad" in r_name and ("bad" in combined or "bath" in combined):
            matched_room = r
            break
        if "kuech" in r_name and ("kuech" in combined or "küch" in combined or "kitchen" in combined):
            matched_room = r
            break
        if "flur" in r_name and ("flur" in combined or "gang" in combined or "hall" in combined):
            matched_room = r
            break

    target_type = None
    if any(w in combined for w in ["licht", "lichter", "lampe", "lampen", "leuchte", "leuchten", "light", "lights"]):
        target_type = "light"
    elif any(w in combined for w in ["ventilator", "ventilatoren", "luefter", "lüfter", "fan", "blower"]):
        target_type = "fan"
    elif any(w in combined for w in ["steckdose", "steckdosen", "plug", "socket", "outlet"]):
        target_type = "socket"
    elif any(w in combined for w in ["heizung", "heizkoerper", "heizkörper", "thermostat", "heater"]):
        target_type = "heater"
    elif any(w in combined for w in ["tv", "fernseher"]):
        target_type = "tv"
    elif any(w in combined for w in ["rollo", "rollos", "jalousie"]):
        target_type = "rollo"

    is_all_requested = any(w in combined for w in ["alle", "alles", "all", "every", "sachen", "geraete", "geräte", "jedes"])

    if matched_room:
        push_log("Auflösung", f"Raum gematcht: '{matched_room.get('name')}'", level="DEBUG")
        room_devs = get_devices_in_room(matched_room)
        if room_devs:
            if target_type:
                filtered = [d for d in room_devs if d.get("type") == target_type]
                if filtered:
                    return filtered
            elif is_all_requested:
                return room_devs
            else:
                for d in room_devs:
                    d_name = d.get("name", "").lower()
                    d_id = d.get("id", "").lower()
                    aliases = [a.lower() for a in d.get("aliases", [])]
                    if d_name in combined or d_id in combined or any(a in combined for a in aliases):
                        return [d]
                return room_devs

    if target_type and is_all_requested:
        matched = [d for d in devices if d.get("type") == target_type]
        if matched:
            return matched

    if is_all_requested and not target_type:
        return devices

    if target_type and not is_all_requested:
        matched = [d for d in devices if d.get("type") == target_type]
        if len(matched) == 1:
            return matched

    q = device_query.lower().strip()
    matched = []
    for d in devices:
        d_id = d.get("id", "").lower()
        d_name = d.get("name", "").lower()
        if d_id == q or d_name == q:
            matched.append(d)
            continue
        aliases = [a.lower().strip() for a in d.get("aliases", [])]
        if q in aliases or any(a in q for a in aliases if len(a) > 2):
            matched.append(d)

    if not matched:
        for d in devices:
            if d.get("id", "").lower() in combined or d.get("name", "").lower() in combined:
                matched.append(d)

    seen = set()
    unique = []
    for d in matched:
        if d["id"] not in seen:
            seen.add(d["id"])
            unique.append(d)
    return unique


def get_all_configured_actions(devices):
    actions = set(["on", "off", "status", "toggle"])
    for d in devices:
        for a in d.get("actions", []):
            clean_a = str(a).strip().lower()
            if clean_a:
                actions.add(clean_a)
    return sorted(list(actions))


def run_needle_command(raw_text):
    devices = get_devices()
    executed_results = []

    all_actions = get_all_configured_actions(devices)
    ActionType = Literal[tuple(all_actions)]

    action_words = set(all_actions) | {"an", "aus", "auf", "zu"}
    action_regex = r"\b(" + "|".join(re.escape(w) for w in action_words) + r")\b"

    needle_cfg = get_needle_config()
    main_desc = needle_cfg.get("tool_description", "Switch, query or control smart-home devices, rooms, or categories.").strip()

    dev_details = []
    for d in devices:
        if d.get("description", "").strip():
            dev_details.append(f"- {d['id']}: {d['description'].strip()}")

    dynamic_docstring = main_desc
    if dev_details:
        dynamic_docstring += "\n\nAvailable devices and descriptions:\n" + "\n".join(dev_details)

    @needle.tool(
        triggers=[
            r"\b(turn|switch|power|flip|mach|schalte|fahre|drehe|stelle|wie|status|set)\b",
            action_regex,
        ]
    )
    def control_device(device: str, action: ActionType):
        resolved = resolve_devices(device, full_text=raw_text)
        if not resolved:
            res = {"error": f"Kein passendes Geraet fuer '{device}' gefunden."}
            push_log("Needle 3", res["error"], level="WARN")
            executed_results.append(res)
            return res

        current_states = get_states()
        for target in resolved:
            dev_id = target["id"]
            curr = current_states.get(dev_id, "unknown")

            eff_action = action
            if action == "toggle":
                eff_action = "off" if curr == "on" else "on"

            if eff_action in ["on", "off"] and curr == eff_action:
                res = {"device": dev_id, "action": eff_action, "skipped": True, "message": f"bereits {curr}"}
                executed_results.append(res)
                continue

            res = execute_device_command(dev_id, eff_action)
            executed_results.append(res)

        return executed_results

    control_device.__doc__ = dynamic_docstring

    t = raw_text.lower()
    t = re.sub(r"\bauf\b", "on", t)
    t = re.sub(r"\ban\b", "on", t)
    t = re.sub(r"\baus\b", "off", t)
    t = re.sub(r"\bliving room\b", "livingroom", t)
    t = re.sub(r"\bbett zimmer\b", "bedroom", t)

    agent = needle.Needle(tools=[control_device])
    agent_output = agent.run(t)

    return {
        "input_command": raw_text,
        "normalized": t,
        "needle_output": agent_output,
        "actions_executed": executed_results,
        "states": get_states(),
    }


def start_continuous_stt():
    if not vosk_model:
        return

    def stt_loop():
        try:
            device_info = sd.query_devices(kind="input")
            samplerate = int(device_info.get("default_samplerate", 44100))
        except Exception:
            samplerate = 44100

        rec = KaldiRecognizer(vosk_model, samplerate)
        blocksize = int(samplerate * 0.2)

        def audio_callback(indata, frames, time_info, status):
            audio_queue.put(bytes(indata))

        waiting_for_command = False
        wake_time = 0.0

        try:
            with sd.RawInputStream(
                samplerate=samplerate,
                blocksize=blocksize,
                dtype="int16",
                channels=1,
                callback=audio_callback,
            ):
                while True:
                    if waiting_for_command and (time.time() - wake_time) > WAKE_TIMEOUT_SECONDS:
                        waiting_for_command = False
                        push_log("Pi Mikrofon", "[Timeout]: Kein Befehl nach Wakeword.", level="INFO")

                    data = audio_queue.get()
                    if rec.AcceptWaveform(data):
                        result = json.loads(rec.Result())
                        raw_text = result.get("text", "").strip()

                        if not raw_text:
                            continue

                        cfg = get_log_config()
                        if cfg.get("log_stt_raw", False):
                            push_log("Pi STT (Roh)", f"\"{raw_text}\"", level="DEBUG")

                        cleaned_text = clean_and_correct(raw_text)
                        if not cleaned_text:
                            continue

                        command_to_run = None

                        if waiting_for_command:
                            command_to_run = cleaned_text
                            waiting_for_command = False
                            push_log("Pi Mikrofon", f"Befehl: \"{command_to_run}\"", level="INFO")
                        else:
                            matched_ww = None
                            for ww in WAKEWORDS:
                                pattern = r"\b" + re.escape(ww) + r"\b"
                                match = re.search(pattern, cleaned_text)
                                if match:
                                    matched_ww = ww
                                    command_part = cleaned_text[match.end():].strip()
                                    if command_part:
                                        command_to_run = command_part
                                        push_log("Pi Mikrofon", f"Befehl: \"{command_to_run}\"", level="INFO")
                                    else:
                                        waiting_for_command = True
                                        wake_time = time.time()
                                        push_log("Pi Mikrofon", "Wakeword erkannt. Höre zu...", level="INFO")
                                    break

                            if not matched_ww:
                                if cfg.get("log_wakeword_checks", False):
                                    push_log("Pi STT", f"Ignoriert (kein Wakeword): '{cleaned_text}'", level="DEBUG")
                                continue

                        if command_to_run:
                            with _hardware_lock:
                                outcome = run_needle_command(command_to_run)

                            summary = outcome.get("actions_executed") or []
                            summary_str = ", ".join([f"{a['device']} -> {a['action']}" for a in summary if "device" in a])
                            push_log("Needle 3", summary_str or "Befehl ausgeführt.", level="INFO")

        except Exception as ex:
            push_log("Fehler", f"Mikrofon-Thread Fehler: {ex}", level="ERROR")

    t = threading.Thread(target=stt_loop, daemon=True)
    t.start()


# --- API Routes ---

@app.route("/")
def index():
    return Response(HTML_TEMPLATE, mimetype="text/html")


@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify({
        "devices": get_devices(),
        "states": get_states(),
        "layout": get_layout(),
        "needle_config": get_needle_config(),
        "log_config": get_log_config()
    })


@app.route("/api/logs", methods=["GET"])
def api_logs():
    since_id = request.args.get("since", 0, type=int)
    level_filter = request.args.get("level", "").upper()
    with _log_lock:
        if since_id:
            filtered = [l for l in system_logs if l["id"] > since_id]
        else:
            filtered = list(system_logs)

    if level_filter and level_filter in LEVEL_WEIGHTS:
        min_w = LEVEL_WEIGHTS[level_filter]
        filtered = [l for l in filtered if LEVEL_WEIGHTS.get(l.get("level", "INFO"), 20) >= min_w]

    # STATES WERDEN IMMER MITGELIEFERT!
    return jsonify({
        "logs": filtered,
        "latest_id": system_logs[-1]["id"] if system_logs else 0,
        "states": get_states()
    })


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    with _log_lock:
        system_logs.clear()
    return jsonify({"success": True})


@app.route("/api/log_settings", methods=["GET", "POST"])
def api_log_settings():
    if request.method == "POST":
        data = request.json or {}
        cfg = get_log_config()
        cfg.update(data)
        save_json(LOG_CONFIG_FILE, cfg)
        apply_vosk_log_level()
        push_log("System", f"Log-Einstellungen: Level={cfg.get('log_level')}, RawSTT={cfg.get('log_stt_raw')}", level="INFO")
        return jsonify({"success": True, "config": cfg})
    return jsonify(get_log_config())


@app.route("/api/device/execute", methods=["POST"])
def api_execute():
    data = request.json or {}
    dev_id = data.get("device_id")
    action = data.get("action")
    params = data.get("params", {})

    if not action or action == "toggle":
        curr = get_states().get(dev_id, "off")
        action = "off" if curr == "on" else "on"

    with _hardware_lock:
        res = execute_device_command(dev_id, action, params)

    push_log("Web-UI", f"{dev_id} -> {action}", level="INFO")
    return jsonify({"result": res, "states": get_states()})


@app.route("/api/needle/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Leerer Text"}), 400

    cleaned = clean_and_correct(text)
    push_log("Chat-Input", text, level="INFO")

    with _hardware_lock:
        outcome = run_needle_command(cleaned)

    summary = outcome.get("actions_executed") or []
    summary_str = ", ".join([f"{a['device']} -> {a['action']}" for a in summary if "device" in a])
    push_log("Needle 3", summary_str or "Befehl verarbeitet.", level="INFO")

    return jsonify(outcome)


@app.route("/api/stt/config", methods=["GET"])
def api_get_stt_config():
    bl_text = ""
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            bl_text = f.read()

    rep_text = ""
    if os.path.exists(REPLACEMENTS_FILE):
        with open(REPLACEMENTS_FILE, "r", encoding="utf-8") as f:
            rep_text = f.read()

    return jsonify({"blacklist": bl_text, "replacements": rep_text})


@app.route("/api/stt/config", methods=["POST"])
def api_save_stt_config():
    data = request.json or {}
    bl_content = data.get("blacklist", "")
    rep_content = data.get("replacements", "")

    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        f.write(bl_content.strip() + "\n" if bl_content.strip() else "")

    with open(REPLACEMENTS_FILE, "w", encoding="utf-8") as f:
        f.write(rep_content.strip() + "\n" if rep_content.strip() else "")

    push_log("System", "STT-Filter gespeichert.", level="INFO")
    return jsonify({"success": True})


@app.route("/api/scripts", methods=["GET"])
def api_list_scripts():
    files = []
    if os.path.exists(SCRIPTS_DIR):
        for fname in sorted(os.listdir(SCRIPTS_DIR)):
            if fname.endswith(".py"):
                fpath = os.path.join(SCRIPTS_DIR, fname)
                files.append({
                    "name": fname,
                    "size": os.path.getsize(fpath),
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(fpath)))
                })
    return jsonify({"scripts": files})


@app.route("/api/scripts/get", methods=["GET"])
def api_get_script():
    name = request.args.get("name", "")
    safe_name = os.path.basename(name)
    fpath = os.path.join(SCRIPTS_DIR, safe_name)
    if not os.path.exists(fpath):
        return jsonify({"error": "Datei nicht gefunden"}), 404

    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return jsonify({"name": safe_name, "content": content})


@app.route("/api/scripts/save", methods=["POST"])
def api_save_script():
    data = request.json or {}
    name = data.get("name", "").strip()
    content = data.get("content", "")

    if not name.endswith(".py"):
        name += ".py"
    safe_name = os.path.basename(name)
    fpath = os.path.join(SCRIPTS_DIR, safe_name)

    with open(fpath, "w", encoding="utf-8") as f:
        f.write(content)

    try:
        os.chmod(fpath, 0o755)
    except Exception:
        pass

    push_log("System", f"Tool-Skript '{safe_name}' gespeichert.", level="INFO")
    return jsonify({"success": True, "name": safe_name})


@app.route("/api/scripts/delete", methods=["POST"])
def api_delete_script():
    data = request.json or {}
    name = data.get("name", "").strip()
    safe_name = os.path.basename(name)
    fpath = os.path.join(SCRIPTS_DIR, safe_name)

    if os.path.exists(fpath):
        os.remove(fpath)
        push_log("System", f"Tool-Skript '{safe_name}' geloescht.", level="INFO")
        return jsonify({"success": True})
    return jsonify({"error": "Datei existiert nicht"}), 404


@app.route("/api/terminal/run", methods=["POST"])
def api_terminal_run():
    data = request.json or {}
    cmd = data.get("command", "").strip()
    if not cmd:
        return jsonify({"error": "Kein Befehl angegeben"}), 400

    push_log("Terminal", f"$ {cmd}", level="INFO")
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=45,
            cwd=BASE_DIR,
            env=get_venv_env()
        )
        return jsonify({
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode
        })
    except subprocess.TimeoutExpired:
        return jsonify({"stderr": "Fehler: Befehl hat das Timeout (45s) ueberschritten.", "returncode": -1})
    except Exception as ex:
        return jsonify({"stderr": str(ex), "returncode": -1})


@app.route("/api/layout/save", methods=["POST"])
def api_save_layout():
    data = request.json or {}
    save_json(LAYOUT_FILE, data)
    push_log("System", "Grundriss-Layout gespeichert.", level="INFO")
    return jsonify({"success": True})


@app.route("/api/devices/save", methods=["POST"])
def api_save_devices():
    req_data = request.json or {}
    devices_data = req_data.get("devices", [])
    needle_cfg = req_data.get("needle_config", {})

    save_json(DEVICES_FILE, devices_data)
    save_json(NEEDLE_CONFIG_FILE, needle_cfg)

    current_states = get_states()
    valid_ids = {d["id"] for d in devices_data}
    cleaned_states = {k: v for k, v in current_states.items() if k in valid_ids}

    for d in devices_data:
        if d["id"] not in cleaned_states:
            cleaned_states[d["id"]] = "off"

    save_json(STATE_FILE, cleaned_states)

    layout = get_layout()
    layout["placements"] = [p for p in layout.get("placements", []) if p.get("device_id") in valid_ids]
    save_json(LAYOUT_FILE, layout)

    push_log("System", "Geraete-Konfiguration gespeichert.", level="INFO")
    return jsonify({"success": True, "states": cleaned_states, "layout": layout, "needle_config": needle_cfg})


# --- Frontend HTML / JS ---

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="de" class="h-full bg-slate-950 text-slate-100">
<head>
  <meta charset="UTF-8">
  <title>Smart Home OS - Canvas & Needle</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    :root {
      --ui-scale: 1;
      --dev-tile-size: 64px;
      --dev-font-size: 9px;
    }
    html {
      font-size: calc(14px * var(--ui-scale));
    }
    .glow-on {
      box-shadow: 0 0 25px rgba(250, 204, 21, 0.65);
      border-color: #facc15;
    }
    .grid-bg {
      background-size: 20px 20px;
      background-image: 
        linear-gradient(to right, rgba(255, 255, 255, 0.05) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(255, 255, 255, 0.05) 1px, transparent 1px);
    }
    .touch-none {
      touch-action: none !important;
    }
    .resize-handle {
      width: 28px;
      height: 28px;
      position: absolute;
      right: 0px;
      bottom: 0px;
      cursor: se-resize;
      touch-action: none;
    }
    .device-tile {
      width: var(--dev-tile-size);
      height: var(--dev-tile-size);
      font-size: var(--dev-font-size);
    }
  </style>
</head>
<body class="h-full flex flex-col font-sans select-none overflow-hidden touch-none">

  <!-- Header -->
  <header class="h-14 border-b border-slate-800 bg-slate-900/90 px-3 flex items-center justify-between z-30 shrink-0 gap-2 overflow-x-auto">
    <div class="flex items-center gap-2 shrink-0">
      <div class="w-8 h-8 rounded-lg bg-amber-500/20 text-amber-400 flex items-center justify-center font-bold">
        <i class="fa-solid fa-house-signal"></i>
      </div>
      <h1 class="font-bold tracking-wide text-sm md:text-base text-white hidden sm:inline">Home Canvas</h1>
    </div>

    <div class="flex items-center gap-1.5 shrink-0">
      <div class="flex bg-slate-800 p-1 rounded-xl border border-slate-700 text-xs font-semibold">
        <button id="btnModeLive" onclick="setMode('live')" class="px-3 py-1.5 rounded-lg bg-amber-500 text-slate-950 font-bold transition">Steuerung</button>
        <button id="btnModeEdit" onclick="setMode('edit')" class="px-3 py-1.5 rounded-lg text-slate-400 hover:text-white transition">Grundriss</button>
      </div>

      <button onclick="openConsoleModal()" class="px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold flex items-center gap-1.5 transition">
        <i class="fa-solid fa-terminal text-emerald-400"></i>
        <span class="hidden md:inline">Konsole & Logs</span>
      </button>

      <button onclick="openTabletSettingsModal()" class="px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold flex items-center gap-1.5 transition">
        <i class="fa-solid fa-tablet-screen-button text-amber-400"></i>
        <span class="hidden md:inline">Tablet & Ansicht</span>
      </button>

      <button onclick="openDeviceEditor()" class="px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold flex items-center gap-1.5 transition">
        <i class="fa-solid fa-sliders text-blue-400"></i>
        <span class="hidden lg:inline">Geraete</span>
      </button>

      <button onclick="openScriptsEditor()" class="px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold flex items-center gap-1.5 transition">
        <i class="fa-brands fa-python text-blue-400"></i>
        <span class="hidden xl:inline">Tool-Skripte</span>
      </button>

      <button onclick="openSttEditor()" class="px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold flex items-center gap-1.5 transition">
        <i class="fa-solid fa-spell-check text-rose-400"></i>
        <span class="hidden xl:inline">STT-Filter</span>
      </button>

      <button id="btnSaveLayout" onclick="saveLayout()" class="hidden px-3.5 py-1.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold items-center gap-1.5 shadow transition">
        <i class="fa-solid fa-floppy-disk"></i> Plan sichern
      </button>

      <button onclick="toggleSidebar()" class="px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 text-xs font-semibold flex items-center gap-1">
        <i class="fa-solid fa-comments"></i>
      </button>
    </div>
  </header>

  <!-- Main View Area -->
  <div class="flex-1 flex overflow-hidden relative">
    
    <!-- Linke Geräte-Palette -->
    <aside id="devicePalette" class="hidden w-72 border-r border-slate-800 bg-slate-900/95 flex flex-col z-20 shrink-0">
      <div class="p-3 border-b border-slate-800 flex items-center justify-between">
        <span class="text-xs font-bold uppercase tracking-wider text-slate-300">Geraete</span>
        <button onclick="addRoom()" class="px-2 py-1 bg-amber-500/10 text-amber-400 hover:bg-amber-500 hover:text-slate-950 rounded-lg text-xs font-bold transition">
          <i class="fa-solid fa-plus mr-1"></i> Raum
        </button>
      </div>
      <div id="paletteDeviceList" class="flex-1 overflow-y-auto p-3 space-y-2"></div>
    </aside>

    <!-- Canvas Viewport -->
    <main class="flex-1 relative overflow-hidden grid-bg touch-none" id="viewport">
      <div id="zoomContainer" class="absolute origin-top-left transition-transform duration-75">
        <div id="canvas" class="relative min-w-[1200px] min-h-[900px] border border-dashed border-slate-800/80 rounded-3xl bg-slate-900/30"></div>
      </div>

      <!-- Zoom-Buttons -->
      <div class="absolute bottom-4 left-4 flex items-center gap-1.5 bg-slate-900/90 border border-slate-700 rounded-2xl p-1.5 shadow-2xl z-20 backdrop-blur">
        <button onclick="zoomStep(-0.15)" class="w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 active:scale-95 text-white flex items-center justify-center font-bold text-base transition">
          <i class="fa-solid fa-minus"></i>
        </button>
        <button onclick="resetZoomPan()" id="zoomLevelDisplay" class="px-2.5 h-9 rounded-xl bg-slate-800/60 text-slate-300 font-mono text-xs font-bold flex items-center justify-center">
          100%
        </button>
        <button onclick="zoomStep(0.15)" class="w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 active:scale-95 text-white flex items-center justify-center font-bold text-base transition">
          <i class="fa-solid fa-plus"></i>
        </button>
        <button onclick="fitCanvasToScreen()" title="Ansicht einpassen" class="w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 active:scale-95 text-amber-400 flex items-center justify-center font-bold text-xs transition">
          <i class="fa-solid fa-expand"></i>
        </button>
      </div>
    </main>

    <!-- Rechte Leiste -->
    <aside id="rightSidebar" class="w-80 md:w-96 border-l border-slate-800 bg-slate-900/90 flex flex-col z-20 shrink-0 transition-all duration-300">
      <div class="p-3 border-b border-slate-800 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse"></span>
          <span class="text-xs font-bold tracking-wider text-slate-300 uppercase">Sprach- & Textbefehle</span>
        </div>
        <div class="flex items-center gap-1.5">
          <button onclick="openConsoleModal()" title="Vollbild-Konsole öffnen" class="text-[11px] text-emerald-400 hover:text-emerald-300 px-2 py-0.5 rounded bg-emerald-500/10 border border-emerald-500/20 font-bold">
            <i class="fa-solid fa-terminal mr-1"></i> Konsole
          </button>
          <button onclick="toggleSidebar()" class="text-slate-400 hover:text-white md:hidden ml-1">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </div>
      </div>

      <div id="chatLog" class="flex-1 overflow-y-auto p-3 space-y-2.5 text-xs">
        <div class="p-3 bg-slate-800/80 rounded-xl border border-slate-700/60 text-slate-300">
          <i class="fa-solid fa-circle-info text-amber-400 mr-1"></i> Sag z. B. <i>"Computer alle Lampen an"</i> oder tippe unten einen Befehl ein.
        </div>
      </div>

      <div class="p-3 border-t border-slate-800 bg-slate-900">
        <div class="flex gap-2">
          <input id="chatInput" type="text" placeholder="Befehl an Needle schreiben..." 
                 class="flex-1 bg-slate-950 border border-slate-700 rounded-xl px-3 py-2.5 text-xs text-white focus:outline-none focus:border-amber-400"
                 onkeydown="if(event.key==='Enter') sendTextCommand()">
          <button onclick="sendTextCommand()" class="px-3.5 bg-amber-500 hover:bg-amber-400 active:scale-95 text-slate-950 font-bold rounded-xl text-xs transition">
            <i class="fa-solid fa-paper-plane"></i>
          </button>
        </div>
      </div>
    </aside>
  </div>

  <!-- Device Context Action Menu -->
  <div id="deviceActionMenu" class="hidden fixed bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl p-2.5 z-50 text-xs flex flex-col gap-1.5 min-w-[170px]">
    <div id="actionMenuTitle" class="font-bold text-slate-300 px-2 py-1 border-b border-slate-800 flex justify-between items-center">
      <span>Aktionen</span>
      <button onclick="closeActionMenu()" class="text-slate-500 hover:text-white"><i class="fa-solid fa-xmark"></i></button>
    </div>
    <div id="actionMenuButtons" class="flex flex-col gap-1 mt-1"></div>
  </div>

  <!-- SYSTEM-KONSOLE & LIVE-LOG MODAL -->
  <div id="consoleModal" class="hidden fixed inset-0 bg-slate-950/85 backdrop-blur-sm z-50 flex items-center justify-center p-3 sm:p-5">
    <div class="bg-slate-900 border border-slate-800 w-full max-w-5xl rounded-3xl p-5 shadow-2xl flex flex-col h-[90vh]">
      <div class="flex items-center justify-between pb-3 border-b border-slate-800">
        <div class="flex items-center gap-2">
          <div class="w-8 h-8 rounded-xl bg-emerald-500/20 text-emerald-400 flex items-center justify-center font-bold">
            <i class="fa-solid fa-terminal"></i>
          </div>
          <div>
            <h2 class="font-bold text-base text-white">Live-Systemkonsole & Log-Steuerung</h2>
            <span class="text-[11px] text-slate-400">Konfiguriere genau, wie viel protokolliert werden soll.</span>
          </div>
        </div>
        <button onclick="closeConsoleModal()" class="text-slate-400 hover:text-white p-2"><i class="fa-solid fa-xmark text-xl"></i></button>
      </div>

      <div class="py-3 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3 text-xs">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="text-slate-500 font-bold mr-1">Log-Level:</span>
          <button onclick="setLogLevelBackend('OFF')" id="filterBtnOFF" class="px-2.5 py-1 rounded-lg bg-slate-800 text-slate-300">AUS (OFF)</button>
          <button onclick="setLogLevelBackend('ERROR')" id="filterBtnERROR" class="px-2.5 py-1 rounded-lg bg-slate-800 text-slate-300">ERROR</button>
          <button onclick="setLogLevelBackend('WARN')" id="filterBtnWARN" class="px-2.5 py-1 rounded-lg bg-slate-800 text-slate-300">WARN</button>
          <button onclick="setLogLevelBackend('INFO')" id="filterBtnINFO" class="px-2.5 py-1 rounded-lg bg-slate-800 text-slate-300">INFO</button>
          <button onclick="setLogLevelBackend('DEBUG')" id="filterBtnDEBUG" class="px-2.5 py-1 rounded-lg bg-slate-800 text-slate-300">DEBUG</button>
        </div>

        <div class="flex items-center gap-3">
          <input id="consoleSearchInput" oninput="renderConsoleLogs()" type="text" placeholder="Suche..." 
                 class="bg-slate-950 border border-slate-700 px-3 py-1 rounded-xl text-xs text-white focus:outline-none focus:border-emerald-400 w-36">
          <label class="flex items-center gap-1.5 text-slate-400 cursor-pointer text-xs">
            <input type="checkbox" id="autoScrollCheck" checked class="rounded border-slate-700"> Auto-Scroll
          </label>
          <button onclick="clearSystemLogs()" class="px-2.5 py-1 bg-rose-500/10 hover:bg-rose-500 text-rose-400 hover:text-white rounded-lg transition font-semibold">
            <i class="fa-solid fa-trash-can mr-1"></i> Leeren
          </button>
        </div>
      </div>

      <div id="fullConsoleOutput" class="flex-1 bg-black/90 border border-slate-800 rounded-2xl p-3 my-3 font-mono text-xs overflow-y-auto text-slate-200 select-text space-y-1 leading-relaxed"></div>

      <div class="pt-3 border-t border-slate-800 flex flex-wrap items-center justify-between gap-3 text-xs">
        <div class="flex items-center gap-4 flex-wrap">
          <label class="flex items-center gap-2 cursor-pointer text-slate-300">
            <input type="checkbox" id="chkTerminalLogging" onchange="updateLogSettings()" class="rounded border-slate-700">
            <span>Terminal-Ausgabe aktiv</span>
          </label>
          <label class="flex items-center gap-2 cursor-pointer text-slate-300">
            <input type="checkbox" id="chkSttRaw" onchange="updateLogSettings()" class="rounded border-slate-700">
            <span class="text-rose-400">Jedes Geräusch mitschreiben (Raw STT)</span>
          </label>
          <label class="flex items-center gap-2 cursor-pointer text-slate-300">
            <input type="checkbox" id="chkVoskVerbose" onchange="updateLogSettings()" class="rounded border-slate-700">
            <span>Vosk C++ Logs</span>
          </label>
        </div>
      </div>
    </div>
  </div>

  <!-- TABLET- & ANSICHTS-EINSTELLUNGEN MODAL -->
  <div id="tabletSettingsModal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-800 w-full max-w-lg rounded-3xl p-6 shadow-2xl flex flex-col space-y-5">
      <div class="flex items-center justify-between pb-3 border-b border-slate-800">
        <div class="flex items-center gap-2">
          <div class="w-8 h-8 rounded-xl bg-amber-500/20 text-amber-400 flex items-center justify-center font-bold">
            <i class="fa-solid fa-tablet-screen-button"></i>
          </div>
          <h2 class="font-bold text-base text-white">Tablet & Ansichts-Optionen</h2>
        </div>
        <button onclick="closeTabletSettingsModal()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>

      <div class="space-y-4 text-xs">
        <div>
          <div class="flex justify-between items-center mb-1.5">
            <label class="font-bold text-slate-200">Gesamte Oberfläche skalieren</label>
            <span id="labelUiScale" class="text-amber-400 font-mono font-bold">100%</span>
          </div>
          <input type="range" id="inputUiScale" min="0.75" max="1.35" step="0.05" value="1.0"
                 oninput="onUiScaleChange(this.value)" class="w-full accent-amber-500">
        </div>

        <div>
          <div class="flex justify-between items-center mb-1.5">
            <label class="font-bold text-slate-200">Kachel-Größe (Touch-Fläche)</label>
            <span id="labelDevSize" class="text-amber-400 font-mono font-bold">64px</span>
          </div>
          <input type="range" id="inputDevSize" min="54" max="96" step="2" value="64"
                 oninput="onDevSizeChange(this.value)" class="w-full accent-amber-500">
        </div>

        <div class="p-3 bg-slate-950 rounded-2xl border border-slate-800 flex items-center justify-between">
          <div>
            <div class="font-bold text-slate-200">Vibrations-Feedback</div>
            <div class="text-[10px] text-slate-500">Kurze Vibration beim Antippen von Kacheln.</div>
          </div>
          <input type="checkbox" id="chkHaptic" onchange="saveTabletConfig()" class="w-5 h-5 accent-amber-500 rounded">
        </div>
      </div>

      <div class="pt-3 border-t border-slate-800 flex justify-end">
        <button onclick="closeTabletSettingsModal()" class="px-5 py-2.5 bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold rounded-xl text-xs transition">
          Fertig & Schließen
        </button>
      </div>
    </div>
  </div>

  <!-- Device Modal -->
  <div id="deviceModal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-3 sm:p-5">
    <div class="bg-slate-900 border border-slate-800 w-full max-w-4xl rounded-3xl p-5 shadow-2xl flex flex-col max-h-[92vh]">
      <div class="flex items-center justify-between pb-3 border-b border-slate-800">
        <div>
          <h2 class="font-bold text-base text-white flex items-center gap-2">
            <i class="fa-solid fa-sliders text-amber-400"></i> Geraete- & Tool-Konfigurator
          </h2>
          <span class="text-[11px] text-slate-400">Passe Geraete, Rufnamen und Aktionen an.</span>
        </div>
        <button onclick="closeDeviceEditor()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>
      <div class="flex-1 overflow-y-auto py-3 space-y-4 text-xs pr-1">
        <div class="p-3.5 bg-slate-950 border border-slate-800 rounded-2xl space-y-2">
          <label class="text-xs font-bold text-amber-400">Globale Needle-Tool-Beschreibung</label>
          <textarea id="globalToolDescInput" rows="2" class="w-full bg-slate-900 border border-slate-700 px-3 py-2 rounded-xl text-xs text-amber-200 font-mono"></textarea>
        </div>
        <div id="deviceList" class="space-y-4"></div>
      </div>
      <div class="pt-3 border-t border-slate-800 flex justify-between items-center">
        <button onclick="addNewDevice()" class="px-3.5 py-2 bg-slate-800 hover:bg-slate-700 text-white rounded-xl text-xs font-semibold">+ Neues Geraet</button>
        <button onclick="saveDevices()" class="px-5 py-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded-xl text-xs font-bold shadow">Speichern</button>
      </div>
    </div>
  </div>

  <!-- Scripts Modal -->
  <div id="scriptsModal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-3 sm:p-5">
    <div class="bg-slate-900 border border-slate-800 w-full max-w-5xl rounded-3xl p-5 shadow-2xl flex flex-col h-[90vh]">
      <div class="flex items-center justify-between pb-3 border-b border-slate-800">
        <h2 class="font-bold text-base text-white"><i class="fa-brands fa-python text-blue-400 mr-2"></i>Tool-Skripte Editor (/tool_scripte/)</h2>
        <button onclick="closeScriptsEditor()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>
      <div class="flex-1 flex flex-col md:flex-row overflow-hidden gap-3 py-3">
        <div class="w-full md:w-64 border border-slate-800 rounded-2xl bg-slate-950/60 p-2.5 flex flex-col h-40 md:h-auto">
          <div class="flex items-center justify-between mb-2">
            <span class="text-xs font-bold uppercase text-slate-400">Dateien</span>
            <button onclick="newScriptTemplate()" class="px-2 py-1 bg-blue-600 hover:bg-blue-500 text-white rounded text-[11px] font-bold">+ Neu</button>
          </div>
          <div id="scriptFilesList" class="flex-1 overflow-y-auto space-y-1"></div>
        </div>
        <div class="flex-1 flex flex-col border border-slate-800 rounded-2xl bg-slate-950/80 p-3">
          <div class="flex items-center justify-between gap-3 mb-2">
            <input id="scriptFileName" type="text" class="bg-slate-900 border border-slate-700 px-2.5 py-1 rounded-lg text-xs font-mono text-amber-300 w-48">
            <div class="flex items-center gap-2">
              <button onclick="deleteCurrentScript()" class="px-3 py-1 bg-rose-500/10 hover:bg-rose-500 text-rose-400 hover:text-white rounded-lg text-xs font-semibold">Loeschen</button>
              <button onclick="saveCurrentScript()" class="px-4 py-1 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg text-xs font-bold">Speichern</button>
            </div>
          </div>
          <textarea id="scriptCodeEditor" spellcheck="false" class="flex-1 bg-slate-900/90 border border-slate-800 rounded-xl p-3 font-mono text-xs text-slate-100 resize-none leading-relaxed"></textarea>
        </div>
      </div>
    </div>
  </div>

  <!-- STT Filter Modal -->
  <div id="sttModal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-800 w-full max-w-3xl rounded-3xl p-5 shadow-2xl flex flex-col max-h-[90vh]">
      <div class="flex items-center justify-between pb-3 border-b border-slate-800">
        <h2 class="font-bold text-base text-white"><i class="fa-solid fa-spell-check text-rose-400 mr-2"></i>STT-Filter</h2>
        <button onclick="closeSttEditor()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>
      <div class="flex-1 overflow-y-auto py-4 space-y-4 text-xs pr-1">
        <div>
          <label class="font-bold text-amber-400 text-xs block mb-1">Wort-Ersetzungen (replacements.txt)</label>
          <textarea id="sttReplacementsInput" rows="7" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-3 font-mono text-xs text-amber-200"></textarea>
        </div>
        <div>
          <label class="font-bold text-rose-400 text-xs block mb-1">Ignorierte Woerter (blacklist.txt)</label>
          <textarea id="sttBlacklistInput" rows="4" class="w-full bg-slate-950 border border-slate-800 rounded-xl p-3 font-mono text-xs text-rose-200"></textarea>
        </div>
      </div>
      <div class="pt-3 border-t border-slate-800 flex justify-end gap-2">
        <button onclick="closeSttEditor()" class="px-4 py-2 bg-slate-800 text-white rounded-xl text-xs">Abbrechen</button>
        <button onclick="saveSttConfig()" class="px-5 py-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded-xl text-xs font-bold">Speichern</button>
      </div>
    </div>
  </div>

  <script>
    let appData = { devices: [], states: {}, layout: { rooms: [], placements: [] }, needle_config: {}, log_config: {} };
    let currentMode = 'live';
    let renderedLogIds = new Set();
    let currentOpenScript = null;
    let allLogs = [];

    // Pan & Zoom
    let panZoom = { zoom: 1.0, panX: 40, panY: 40, isPanning: false, startPanX: 0, startPanY: 0, initialDistance: 0, initialZoom: 1.0 };

    // Tablet Einstellungen
    let tabletConfig = { uiScale: 1.0, devSize: 64, haptic: true };

    const PRESET_ICONS = [
      { id: "fa-lightbulb", label: "Licht" },
      { id: "fa-fan", label: "Luefter" },
      { id: "fa-plug", label: "Steckdose" },
      { id: "fa-fire", label: "Heizung" },
      { id: "fa-tv", label: "Fernseher" },
      { id: "fa-blinds", label: "Rollo" },
      { id: "fa-music", label: "Audio" },
      { id: "fa-power-off", label: "Schalter" },
      { id: "fa-robot", label: "Roboter" }
    ];

    function triggerHaptic() {
      if (tabletConfig.haptic && navigator.vibrate) {
        try { navigator.vibrate(30); } catch(e) {}
      }
    }

    function loadTabletConfig() {
      try {
        const saved = localStorage.getItem('sh_tablet_config');
        if (saved) tabletConfig = Object.assign(tabletConfig, JSON.parse(saved));
      } catch(e) {}
      applyTabletConfig();
    }

    function saveTabletConfig() {
      tabletConfig.haptic = document.getElementById('chkHaptic').checked;
      try { localStorage.setItem('sh_tablet_config', JSON.stringify(tabletConfig)); } catch(e) {}
      applyTabletConfig();
    }

    function applyTabletConfig() {
      document.documentElement.style.setProperty('--ui-scale', tabletConfig.uiScale);
      document.documentElement.style.setProperty('--dev-tile-size', tabletConfig.devSize + 'px');
      document.documentElement.style.setProperty('--dev-font-size', Math.round(tabletConfig.devSize * 0.14) + 'px');

      const scaleInput = document.getElementById('inputUiScale');
      if (scaleInput) scaleInput.value = tabletConfig.uiScale;
      const scaleLabel = document.getElementById('labelUiScale');
      if (scaleLabel) scaleLabel.innerText = Math.round(tabletConfig.uiScale * 100) + '%';

      const devInput = document.getElementById('inputDevSize');
      if (devInput) devInput.value = tabletConfig.devSize;
      const devLabel = document.getElementById('labelDevSize');
      if (devLabel) devLabel.innerText = tabletConfig.devSize + 'px';

      const chkHaptic = document.getElementById('chkHaptic');
      if (chkHaptic) chkHaptic.checked = tabletConfig.haptic;
    }

    function onUiScaleChange(val) {
      tabletConfig.uiScale = parseFloat(val);
      document.getElementById('labelUiScale').innerText = Math.round(tabletConfig.uiScale * 100) + '%';
      saveTabletConfig();
    }

    function onDevSizeChange(val) {
      tabletConfig.devSize = parseInt(val);
      document.getElementById('labelDevSize').innerText = tabletConfig.devSize + 'px';
      saveTabletConfig();
      renderCanvas();
    }

    function openTabletSettingsModal() { document.getElementById('tabletSettingsModal').classList.remove('hidden'); }
    function closeTabletSettingsModal() { document.getElementById('tabletSettingsModal').classList.add('hidden'); }
    function toggleSidebar() { document.getElementById('rightSidebar').classList.toggle('hidden'); }

    // --- Pan & Zoom ---
    function applyTransform() {
      const container = document.getElementById('zoomContainer');
      if (!container) return;
      container.style.transform = `translate(${panZoom.panX}px, ${panZoom.panY}px) scale(${panZoom.zoom})`;
      const disp = document.getElementById('zoomLevelDisplay');
      if (disp) disp.innerText = Math.round(panZoom.zoom * 100) + '%';
    }

    function zoomStep(delta) {
      panZoom.zoom = Math.round(Math.min(2.5, Math.max(0.4, panZoom.zoom + delta)) * 100) / 100;
      applyTransform();
    }

    function resetZoomPan() {
      panZoom.zoom = 1.0; panZoom.panX = 40; panZoom.panY = 40; applyTransform();
    }

    function fitCanvasToScreen() {
      const viewport = document.getElementById('viewport');
      const rect = viewport.getBoundingClientRect();
      const scaleX = (rect.width - 40) / 1200;
      const scaleY = (rect.height - 40) / 900;
      panZoom.zoom = Math.min(1.2, Math.max(0.4, Math.min(scaleX, scaleY)));
      panZoom.panX = 20; panZoom.panY = 20;
      applyTransform();
    }

    function setupViewportGestures() {
      const vp = document.getElementById('viewport');
      vp.addEventListener('touchstart', (e) => {
        if (e.touches.length === 2) {
          e.preventDefault();
          const p1 = e.touches[0], p2 = e.touches[1];
          panZoom.initialDistance = Math.hypot(p1.clientX - p2.clientX, p1.clientY - p2.clientY);
          panZoom.initialZoom = panZoom.zoom;
          panZoom.startPanX = (p1.clientX + p2.clientX) / 2 - panZoom.panX;
          panZoom.startPanY = (p1.clientY + p2.clientY) / 2 - panZoom.panY;
        } else if (e.touches.length === 1 && !e.target.closest('.device-tile') && !e.target.closest('.room-card')) {
          panZoom.isPanning = true;
          panZoom.startPanX = e.touches[0].clientX - panZoom.panX;
          panZoom.startPanY = e.touches[0].clientY - panZoom.panY;
        }
      }, { passive: false });

      vp.addEventListener('touchmove', (e) => {
        if (e.touches.length === 2) {
          e.preventDefault();
          const p1 = e.touches[0], p2 = e.touches[1];
          const dist = Math.hypot(p1.clientX - p2.clientX, p1.clientY - p2.clientY);
          if (panZoom.initialDistance > 0) {
            panZoom.zoom = Math.min(2.5, Math.max(0.4, panZoom.initialZoom * (dist / panZoom.initialDistance)));
          }
          panZoom.panX = (p1.clientX + p2.clientX) / 2 - panZoom.startPanX;
          panZoom.panY = (p1.clientY + p2.clientY) / 2 - panZoom.startPanY;
          applyTransform();
        } else if (e.touches.length === 1 && panZoom.isPanning) {
          e.preventDefault();
          panZoom.panX = e.touches[0].clientX - panZoom.startPanX;
          panZoom.panY = e.touches[0].clientY - panZoom.startPanY;
          applyTransform();
        }
      }, { passive: false });

      vp.addEventListener('touchend', (e) => {
        if (e.touches.length < 2) panZoom.initialDistance = 0;
        if (e.touches.length === 0) panZoom.isPanning = false;
      });

      vp.addEventListener('wheel', (e) => {
        e.preventDefault();
        zoomStep(e.deltaY < 0 ? 0.08 : -0.08);
      }, { passive: false });
    }

    // --- State & Logs Laden ---

    async function loadState() {
      try {
        const res = await fetch('/api/state');
        appData = await res.json();
        if (!appData.layout) appData.layout = { rooms: [], placements: [] };
        if (!appData.devices) appData.devices = [];
        if (!appData.states) appData.states = {};
        if (appData.log_config) applyLogConfigToInputs(appData.log_config);
        renderCanvas();
        renderPalette();
        fitCanvasToScreen();
      } catch (err) {}
    }

    // Haupt-Polling: Läuft zuverlässig jede Sekunde
    async function pollLogsAndState() {
      try {
        const res = await fetch('/api/logs');
        const data = await res.json();

        // 1. LIVE-UPDATE DER GERÄTE-STATUS (KOMPLETT UNABHÄNGIG VON LOGS)
        if (data.states) {
          const newStatesStr = JSON.stringify(data.states);
          const oldStatesStr = JSON.stringify(appData.states);
          if (newStatesStr !== oldStatesStr) {
            appData.states = data.states;
            renderCanvas();
          }
        }

        // 2. Chat / Sprachbefehle in Sidebar
        allLogs = data.logs || [];
        const box = document.getElementById('chatLog');
        if (box) {
          let hasNewChat = false;
          allLogs.forEach(l => {
            if (!renderedLogIds.has(l.id)) {
              renderedLogIds.add(l.id);

              const isChatRelevant = (
                l.author === 'Chat-Input' || 
                l.author === 'Needle 3' || 
                (l.author === 'Pi Mikrofon' && (l.text.startsWith('Befehl:') || l.text.startsWith('Wakeword')))
              );

              if (isChatRelevant) {
                hasNewChat = true;
                const item = document.createElement('div');
                item.className = "p-2.5 rounded-xl border bg-slate-800/90 border-slate-700 text-slate-200";
                const authorColor = l.author === 'Pi Mikrofon' ? 'text-emerald-400' : (l.author === 'Needle 3' ? 'text-amber-400' : 'text-blue-400');
                item.innerHTML = `
                  <div class="flex items-center justify-between font-bold mb-1">
                    <span class="${authorColor}">${l.author}</span>
                    <span class="text-[10px] text-slate-500">${l.time}</span>
                  </div>
                  <div>${escapeHtml(l.text)}</div>
                `;
                box.appendChild(item);
              }
            }
          });
          if (hasNewChat) {
            box.scrollTop = box.scrollHeight;
          }
        }

        const modal = document.getElementById('consoleModal');
        if (modal && !modal.classList.contains('hidden')) {
          renderConsoleLogs();
        }
      } catch (err) {}
    }

    // 1 Sekunde für direkte Live-Aktualisierung
    setInterval(pollLogsAndState, 1000);

    // --- Konsole & Log-Viewer ---

    function openConsoleModal() {
      document.getElementById('consoleModal').classList.remove('hidden');
      loadLogSettings();
      renderConsoleLogs();
    }

    function closeConsoleModal() {
      document.getElementById('consoleModal').classList.add('hidden');
    }

    async function setLogLevelBackend(level) {
      await fetch('/api/log_settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ log_level: level })
      });
      loadLogSettings();
    }

    function updateLogLevelButtons(activeLevel) {
      ['OFF', 'ERROR', 'WARN', 'INFO', 'DEBUG'].forEach(lvl => {
        const btn = document.getElementById('filterBtn' + lvl);
        if (btn) {
          if (lvl === activeLevel) {
            btn.className = "px-2.5 py-1 rounded-lg bg-amber-500 text-slate-950 font-bold shadow";
          } else {
            btn.className = "px-2.5 py-1 rounded-lg bg-slate-800 text-slate-300 hover:text-white";
          }
        }
      });
    }

    function renderConsoleLogs() {
      const container = document.getElementById('fullConsoleOutput');
      if (!container) return;
      const search = (document.getElementById('consoleSearchInput').value || '').toLowerCase();

      const filtered = allLogs.filter(l => {
        if (search && !l.text.toLowerCase().includes(search) && !l.author.toLowerCase().includes(search)) return false;
        return true;
      });

      container.innerHTML = filtered.map(l => {
        let lvlColor = "text-slate-400";
        if (l.level === "DEBUG") lvlColor = "text-slate-500";
        if (l.level === "INFO") lvlColor = "text-cyan-400";
        if (l.level === "WARN") lvlColor = "text-amber-400";
        if (l.level === "ERROR") lvlColor = "text-rose-400 font-bold";

        return `
          <div class="hover:bg-slate-900/60 p-0.5 rounded transition">
            <span class="text-slate-600">[${l.time}]</span>
            <span class="${lvlColor} font-bold mr-1">[${l.level}]</span>
            <span class="text-amber-300 font-semibold">&lt;${l.author}&gt;</span>
            <span class="text-slate-100">${escapeHtml(l.text)}</span>
            ${l.extra ? `<div class="ml-6 text-[11px] text-slate-500 italic">↳ ${escapeHtml(l.extra)}</div>` : ''}
          </div>
        `;
      }).join('');

      if (document.getElementById('autoScrollCheck').checked) {
        container.scrollTop = container.scrollHeight;
      }
    }

    async function clearSystemLogs() {
      await fetch('/api/logs/clear', { method: 'POST' });
      allLogs = [];
      renderedLogIds.clear();
      renderConsoleLogs();
      const box = document.getElementById('chatLog');
      if (box) box.innerHTML = '';
    }

    async function loadLogSettings() {
      try {
        const res = await fetch('/api/log_settings');
        const cfg = await res.json();
        applyLogConfigToInputs(cfg);
      } catch(e) {}
    }

    function applyLogConfigToInputs(cfg) {
      updateLogLevelButtons(cfg.log_level || "INFO");
      const term = document.getElementById('chkTerminalLogging');
      if (term) term.checked = !!cfg.terminal_logging;
      const stt = document.getElementById('chkSttRaw');
      if (stt) stt.checked = !!cfg.log_stt_raw;
      const vosk = document.getElementById('chkVoskVerbose');
      if (vosk) vosk.checked = !!cfg.vosk_verbose;
    }

    async function updateLogSettings() {
      const body = {
        terminal_logging: document.getElementById('chkTerminalLogging').checked,
        log_stt_raw: document.getElementById('chkSttRaw').checked,
        vosk_verbose: document.getElementById('chkVoskVerbose').checked
      };
      await fetch('/api/log_settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // --- Modus & Palette ---

    function setMode(mode) {
      currentMode = mode;
      document.getElementById('btnModeLive').className = mode === 'live' 
        ? "px-3 py-1.5 rounded-lg bg-amber-500 text-slate-950 font-bold transition"
        : "px-3 py-1.5 rounded-lg text-slate-400 hover:text-white transition";

      document.getElementById('btnModeEdit').className = mode === 'edit'
        ? "px-3 py-1.5 rounded-lg bg-amber-500 text-slate-950 font-bold transition"
        : "px-3 py-1.5 rounded-lg text-slate-400 hover:text-white transition";

      document.getElementById('devicePalette').classList.toggle('hidden', mode !== 'edit');
      document.getElementById('btnSaveLayout').classList.toggle('hidden', mode !== 'edit');
      closeActionMenu();
      renderCanvas();
      renderPalette();
    }

    function renderPalette() {
      const container = document.getElementById('paletteDeviceList');
      if (!container) return;
      container.innerHTML = '';
      const placedIds = new Set((appData.layout.placements || []).map(p => p.device_id));

      (appData.devices || []).forEach(dev => {
        const isPlaced = placedIds.has(dev.id);
        const card = document.createElement('div');
        card.className = `p-2.5 rounded-xl border flex items-center justify-between transition ${
          isPlaced ? 'bg-slate-950/40 border-slate-800 text-slate-400' : 'bg-slate-800 border-slate-700 text-slate-100 hover:border-amber-400'
        }`;

        const iconClass = dev.icon || 'fa-plug';
        card.innerHTML = `
          <div class="flex items-center gap-2.5 overflow-hidden">
            <div class="w-8 h-8 rounded-lg bg-slate-900 flex items-center justify-center text-amber-400">
              <i class="fa-solid ${iconClass} text-xs"></i>
            </div>
            <div class="truncate">
              <div class="text-xs font-bold truncate">${dev.name || dev.id}</div>
              <div class="text-[10px] text-slate-500 font-mono">${dev.id}</div>
            </div>
          </div>
          <div>
            ${isPlaced 
              ? `<span class="text-[10px] text-emerald-400 font-semibold px-2 py-0.5 rounded bg-emerald-500/10"><i class="fa-solid fa-check"></i> Gesetz</span>` 
              : `<button onclick="placeDeviceOnCanvas('${dev.id}', 120, 120)" class="px-2.5 py-1 bg-amber-500 hover:bg-amber-400 active:scale-95 text-slate-950 rounded-lg text-[10px] font-bold transition">+ Setzen</button>`}
          </div>
        `;
        container.appendChild(card);
      });
    }

    function placeDeviceOnCanvas(devId, x, y) {
      appData.layout.placements = appData.layout.placements || [];
      const existing = appData.layout.placements.find(p => p.device_id === devId);
      if (existing) { existing.x = x; existing.y = y; } 
      else { appData.layout.placements.push({ device_id: devId, x: x, y: y }); }
      renderCanvas();
      renderPalette();
    }

    function unplaceDevice(devId) {
      appData.layout.placements = (appData.layout.placements || []).filter(p => p.device_id !== devId);
      renderCanvas();
      renderPalette();
    }

    // --- Drag & Touch ---

    function makeElementDraggable(el, onMove) {
      let startClientX = 0, startClientY = 0, initialLeft = 0, initialTop = 0, isDragging = false;

      function onStart(e) {
        if (e.target.closest('button') || e.target.closest('.resize-handle')) return;
        isDragging = true;
        const pt = e.touches ? e.touches[0] : e;
        startClientX = pt.clientX; startClientY = pt.clientY;
        initialLeft = parseFloat(el.style.left) || 0;
        initialTop = parseFloat(el.style.top) || 0;

        window.addEventListener('mousemove', onDrag, { passive: false });
        window.addEventListener('mouseup', onEnd);
        window.addEventListener('touchmove', onDrag, { passive: false });
        window.addEventListener('touchend', onEnd);
      }

      function onDrag(e) {
        if (!isDragging) return;
        e.preventDefault();
        const pt = e.touches ? e.touches[0] : e;
        const deltaX = (pt.clientX - startClientX) / panZoom.zoom;
        const deltaY = (pt.clientY - startClientY) / panZoom.zoom;
        let newX = Math.max(10, Math.round((initialLeft + deltaX) / 10) * 10);
        let newY = Math.max(10, Math.round((initialTop + deltaY) / 10) * 10);
        el.style.left = newX + 'px';
        el.style.top = newY + 'px';
        onMove(newX, newY);
      }

      function onEnd() {
        isDragging = false;
        window.removeEventListener('mousemove', onDrag);
        window.removeEventListener('mouseup', onEnd);
        window.removeEventListener('touchmove', onDrag);
        window.removeEventListener('touchend', onEnd);
      }

      el.addEventListener('mousedown', onStart);
      el.addEventListener('touchstart', onStart, { passive: false });
    }

    // --- Canvas Rendering ---

    function renderCanvas() {
      const canvas = document.getElementById('canvas');
      if (!canvas) return;
      canvas.innerHTML = '';

      // Räume
      (appData.layout.rooms || []).forEach((room, idx) => {
        const rEl = document.createElement('div');
        rEl.className = `room-card absolute rounded-2xl border border-slate-700/80 bg-slate-800/40 p-3 transition-colors ${
          currentMode === 'edit' ? 'cursor-move ring-1 ring-amber-500/40' : ''
        }`;
        rEl.style.left = room.x + 'px';
        rEl.style.top = room.y + 'px';
        rEl.style.width = room.w + 'px';
        rEl.style.height = room.h + 'px';

        rEl.innerHTML = `
          <div class="flex items-center justify-between text-xs font-bold text-slate-400 select-none">
            <span><i class="fa-solid fa-vector-square mr-1 text-slate-500"></i> ${room.name}</span>
            <span class="text-[10px] text-slate-600 font-mono">${room.w}x${room.h}</span>
            ${currentMode === 'edit' ? `<button onclick="deleteRoom(${idx})" class="text-rose-400 hover:text-rose-300 ml-2 p-1"><i class="fa-solid fa-trash"></i></button>` : ''}
          </div>
        `;

        if (currentMode === 'edit') {
          makeElementDraggable(rEl, (x, y) => { room.x = x; room.y = y; });

          const resizeHandle = document.createElement('div');
          resizeHandle.className = 'resize-handle text-slate-500 hover:text-amber-400 flex items-center justify-center';
          resizeHandle.innerHTML = '<i class="fa-solid fa-grip-lines-vertical rotate-45 text-xs"></i>';

          let rStartX = 0, rStartY = 0, rStartW = 0, rStartH = 0;
          function startResize(e) {
            e.stopPropagation();
            const pt = e.touches ? e.touches[0] : e;
            rStartX = pt.clientX; rStartY = pt.clientY;
            rStartW = room.w; rStartH = room.h;

            function doResize(ev) {
              ev.preventDefault();
              const p = ev.touches ? ev.touches[0] : ev;
              const dX = (p.clientX - rStartX) / panZoom.zoom;
              const dY = (p.clientY - rStartY) / panZoom.zoom;
              const newW = Math.max(120, Math.round((rStartW + dX) / 10) * 10);
              const newH = Math.max(100, Math.round((rStartH + dY) / 10) * 10);
              room.w = newW; room.h = newH;
              rEl.style.width = newW + 'px'; rEl.style.height = newH + 'px';
              const label = rEl.querySelector('.font-mono');
              if (label) label.innerText = `${newW}x${newH}`;
            }

            function stopResize() {
              window.removeEventListener('mousemove', doResize);
              window.removeEventListener('mouseup', stopResize);
              window.removeEventListener('touchmove', doResize);
              window.removeEventListener('touchend', stopResize);
            }

            window.addEventListener('mousemove', doResize);
            window.addEventListener('mouseup', stopResize);
            window.addEventListener('touchmove', doResize, { passive: false });
            window.addEventListener('touchend', stopResize);
          }

          resizeHandle.addEventListener('mousedown', startResize);
          resizeHandle.addEventListener('touchstart', startResize, { passive: false });
          rEl.appendChild(resizeHandle);
        }

        canvas.appendChild(rEl);
      });

      // Geräte
      (appData.layout.placements || []).forEach((p) => {
        const dev = (appData.devices || []).find(d => d.id === p.device_id) || { id: p.device_id, name: p.device_id, icon: 'fa-plug', type: 'custom', actions: ['on', 'off'] };
        const state = (appData.states && appData.states[p.device_id]) || 'off';
        const isOn = state === 'on';

        const dEl = document.createElement('div');
        dEl.className = `device-tile absolute z-10 rounded-2xl flex flex-col items-center justify-center p-1 transition-all duration-300 border select-none ${
          isOn ? 'bg-amber-500/20 border-amber-400 glow-on text-amber-300' : 'bg-slate-800/95 border-slate-700 text-slate-400'
        } ${currentMode === 'edit' ? 'cursor-grab ring-2 ring-blue-500/60' : 'cursor-pointer hover:scale-105 active:scale-95'}`;

        dEl.style.left = p.x + 'px';
        dEl.style.top = p.y + 'px';

        const iconClass = dev.icon || 'fa-plug';
        dEl.innerHTML = `
          ${currentMode === 'edit' ? `<button onclick="unplaceDevice('${dev.id}')" title="Entfernen" class="absolute -top-2 -right-2 w-6 h-6 rounded-full bg-rose-500 text-white flex items-center justify-center text-xs shadow"><i class="fa-solid fa-xmark"></i></button>` : ''}
          <i class="fa-solid ${iconClass} ${dev.type === 'fan' && isOn ? 'fa-spin' : ''} text-lg mb-0.5 pointer-events-none"></i>
          <span class="font-bold text-center leading-tight truncate w-full px-1 pointer-events-none">${dev.name || dev.id}</span>
          <span class="text-[8px] uppercase tracking-wider font-extrabold pointer-events-none ${isOn ? 'text-amber-400' : 'text-slate-500'}">${state}</span>
        `;

        if (currentMode === 'live') {
          let touchTimer = null;
          let hasMoved = false;

          dEl.addEventListener('touchstart', (e) => {
            hasMoved = false;
            touchTimer = setTimeout(() => {
              triggerHaptic();
              openActionMenu(e.touches[0].clientX, e.touches[0].clientY, dev);
              touchTimer = null;
            }, 550);
          }, { passive: true });

          dEl.addEventListener('touchmove', () => { hasMoved = true; clearTimeout(touchTimer); }, { passive: true });
          dEl.addEventListener('touchend', () => {
            clearTimeout(touchTimer);
            if (!hasMoved && touchTimer !== null) {
              triggerHaptic();
              const nextAction = (state === 'on') ? 'off' : 'on';
              triggerDeviceAction(p.device_id, nextAction);
            }
          });

          dEl.onclick = () => {
            triggerHaptic();
            const nextAction = (state === 'on') ? 'off' : 'on';
            triggerDeviceAction(p.device_id, nextAction);
          };

          dEl.oncontextmenu = (e) => {
            e.preventDefault();
            triggerHaptic();
            openActionMenu(e.clientX, e.clientY, dev);
          };
        } else {
          makeElementDraggable(dEl, (x, y) => { p.x = x; p.y = y; });
        }

        canvas.appendChild(dEl);
      });
    }

    // --- Kontextmenü ---

    function openActionMenu(x, y, dev) {
      const menu = document.getElementById('deviceActionMenu');
      const title = document.getElementById('actionMenuTitle');
      const container = document.getElementById('actionMenuButtons');

      title.innerHTML = `<span>${dev.name || dev.id}</span><button onclick="closeActionMenu()" class="text-slate-500 hover:text-white"><i class="fa-solid fa-xmark"></i></button>`;
      container.innerHTML = '';

      const actions = (dev.actions && dev.actions.length > 0) ? dev.actions : ['on', 'off', 'status'];
      actions.forEach(act => {
        const btn = document.createElement('button');
        btn.className = "px-3 py-2 rounded-xl bg-slate-800 hover:bg-amber-500 hover:text-slate-950 text-left font-semibold capitalize transition flex items-center justify-between text-xs";
        btn.innerHTML = `<span>${act}</span> <i class="fa-solid fa-play text-[9px] opacity-60"></i>`;
        btn.onclick = () => {
          triggerHaptic();
          triggerDeviceAction(dev.id, act);
          closeActionMenu();
        };
        container.appendChild(btn);
      });

      menu.style.left = Math.min(x, window.innerWidth - 180) + 'px';
      menu.style.top = Math.min(y, window.innerHeight - 200) + 'px';
      menu.classList.remove('hidden');
    }

    function closeActionMenu() { document.getElementById('deviceActionMenu').classList.add('hidden'); }

    window.addEventListener('click', (e) => {
      if (!e.target.closest('#deviceActionMenu')) closeActionMenu();
    });

    function addRoom() {
      const name = prompt("Name des neuen Raumes (z.B. Wohnzimmer, Flur):", "Neuer Raum");
      if (!name) return;
      appData.layout.rooms = appData.layout.rooms || [];
      appData.layout.rooms.push({ id: "room_" + Date.now(), name: name, x: 80, y: 80, w: 280, h: 240 });
      renderCanvas();
    }

    function deleteRoom(idx) {
      if (confirm("Diesen Raum wirklich entfernen?")) {
        appData.layout.rooms.splice(idx, 1);
        renderCanvas();
      }
    }

    async function saveLayout() {
      await fetch('/api/layout/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(appData.layout)
      });
      setMode('live');
    }

    // Schnelles Feedback: Optimistisches Umschalten vor dem Netzwerk-Call
    async function triggerDeviceAction(deviceId, action) {
      if (action === 'on' || action === 'off') {
        appData.states = appData.states || {};
        appData.states[deviceId] = action;
        renderCanvas();
      }

      const res = await fetch('/api/device/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: deviceId, action: action })
      });
      const data = await res.json();
      if (data.states) {
        appData.states = data.states;
        renderCanvas();
      }
    }

    async function sendTextCommand() {
      const input = document.getElementById('chatInput');
      const val = input.value.trim();
      if (!val) return;
      input.value = '';

      const res = await fetch('/api/needle/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: val })
      });
      const data = await res.json();
      if (data.states) {
        appData.states = data.states;
        renderCanvas();
      }
    }

    // --- STT-Filter ---

    async function openSttEditor() {
      const res = await fetch('/api/stt/config');
      const data = await res.json();
      document.getElementById('sttBlacklistInput').value = data.blacklist || '';
      document.getElementById('sttReplacementsInput').value = data.replacements || '';
      document.getElementById('sttModal').classList.remove('hidden');
    }

    function closeSttEditor() { document.getElementById('sttModal').classList.add('hidden'); }

    async function saveSttConfig() {
      const bl = document.getElementById('sttBlacklistInput').value;
      const rep = document.getElementById('sttReplacementsInput').value;
      await fetch('/api/stt/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ blacklist: bl, replacements: rep })
      });
      closeSttEditor();
    }

    // --- Tool-Skripte ---

    async function openScriptsEditor() {
      await loadScriptsList();
      document.getElementById('scriptsModal').classList.remove('hidden');
    }

    function closeScriptsEditor() { document.getElementById('scriptsModal').classList.add('hidden'); }

    async function loadScriptsList() {
      const res = await fetch('/api/scripts');
      const data = await res.json();
      const container = document.getElementById('scriptFilesList');
      container.innerHTML = '';
      const scripts = data.scripts || [];
      if (scripts.length === 0) {
        container.innerHTML = '<div class="text-[11px] text-slate-500 italic p-2">Keine Skripte.</div>';
        newScriptTemplate();
        return;
      }
      scripts.forEach(s => {
        const item = document.createElement('div');
        const isActive = currentOpenScript === s.name;
        item.className = `p-2 rounded-xl cursor-pointer flex items-center justify-between text-xs font-mono transition ${
          isActive ? 'bg-blue-600 text-white font-bold' : 'text-slate-300 hover:bg-slate-900 border border-transparent hover:border-slate-800'
        }`;
        item.innerHTML = `<span><i class="fa-brands fa-python mr-1.5 opacity-70"></i>${s.name}</span>`;
        item.onclick = () => openScriptFile(s.name);
        container.appendChild(item);
      });
      if (!currentOpenScript && scripts.length > 0) openScriptFile(scripts[0].name);
    }

    async function openScriptFile(filename) {
      currentOpenScript = filename;
      const res = await fetch(`/api/scripts/get?name=${encodeURIComponent(filename)}`);
      const data = await res.json();
      document.getElementById('scriptFileName').value = data.name || filename;
      document.getElementById('scriptCodeEditor').value = data.content || '';
      loadScriptsList();
    }

    function newScriptTemplate() {
      currentOpenScript = "neues_tool.py";
      document.getElementById('scriptFileName').value = "neues_tool.py";
      document.getElementById('scriptCodeEditor').value = '#!/usr/bin/env python3\\nimport argparse, json\\n\\ndef main():\\n    pass\\n\\nif __name__ == "__main__":\\n    main()';
    }

    async function saveCurrentScript() {
      const name = document.getElementById('scriptFileName').value.trim();
      const content = document.getElementById('scriptCodeEditor').value;
      if (!name) return;
      const res = await fetch('/api/scripts/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, content: content })
      });
      const data = await res.json();
      currentOpenScript = data.name;
      await loadScriptsList();
    }

    async function deleteCurrentScript() {
      const name = document.getElementById('scriptFileName').value.trim();
      if (!name) return;
      if (confirm(`Skript '${name}' wirklich loeschen?`)) {
        await fetch('/api/scripts/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name })
        });
        currentOpenScript = null;
        await loadScriptsList();
      }
    }

    // --- Geräte-Konfigurator ---

    function openDeviceEditor() {
      const list = document.getElementById('deviceList');
      list.innerHTML = '';
      const globalInput = document.getElementById('globalToolDescInput');
      if (globalInput) {
        globalInput.value = (appData.needle_config && appData.needle_config.tool_description) ? appData.needle_config.tool_description : "";
      }

      appData.devices.forEach((dev, idx) => {
        dev.actions = dev.actions || ['on', 'off', 'status'];
        const item = document.createElement('div');
        item.className = "p-4 bg-slate-950 rounded-2xl border border-slate-800 space-y-3 relative";
        item.innerHTML = `
          <div class="flex gap-2 items-center flex-wrap">
            <div class="flex-1 min-w-[160px]">
              <label class="text-[10px] text-slate-400 font-bold block mb-0.5">Anzeigename</label>
              <input type="text" value="${dev.name || dev.id}" onchange="appData.devices[${idx}].name = this.value" class="w-full bg-slate-900 border border-slate-700 px-3 py-1.5 rounded-xl text-white font-bold text-xs">
            </div>
            <div class="w-28">
              <label class="text-[10px] text-slate-400 font-bold block mb-0.5">Typ</label>
              <input type="text" value="${dev.type || 'custom'}" onchange="appData.devices[${idx}].type = this.value" class="w-full bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded-xl text-white text-xs font-semibold">
            </div>
            <div class="w-28">
              <label class="text-[10px] text-slate-400 font-bold block mb-0.5">Icon</label>
              <select onchange="appData.devices[${idx}].icon = this.value; renderCanvas(); openDeviceEditor();" class="w-full bg-slate-900 border border-slate-700 px-2 py-1.5 rounded-xl text-white text-xs">
                ${PRESET_ICONS.map(i => `<option value="${i.id}" ${dev.icon === i.id ? 'selected':''}>${i.label}</option>`).join('')}
              </select>
            </div>
            <div class="pt-3">
              <button onclick="deleteDeviceConfirm(${idx})" class="w-8 h-8 rounded-xl bg-rose-500/10 hover:bg-rose-500 text-rose-400 hover:text-white flex items-center justify-center transition"><i class="fa-solid fa-trash text-xs"></i></button>
            </div>
          </div>
          <div>
            <label class="text-[10px] text-amber-400 font-bold block mb-0.5">Needle-Beschreibung</label>
            <input type="text" value="${dev.description || ''}" onchange="appData.devices[${idx}].description = this.value" class="w-full bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded-xl text-xs text-slate-200">
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label class="text-[10px] text-amber-400 font-bold block mb-0.5">System-ID</label>
              <input type="text" value="${dev.id}" onchange="appData.devices[${idx}].id = this.value" class="w-full bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded-xl font-mono text-xs text-slate-300">
            </div>
            <div>
              <label class="text-[10px] text-amber-400 font-bold block mb-0.5">Rufnamen / Aliases</label>
              <input type="text" value="${(dev.aliases || []).join(', ')}" onchange="appData.devices[${idx}].aliases = this.value.split(',').map(s=>s.trim()).filter(Boolean)" class="w-full bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded-xl text-xs text-slate-300">
            </div>
          </div>
          <div>
            <label class="text-[10px] text-emerald-400 font-bold block mb-0.5">Aktionen</label>
            <input type="text" value="${(dev.actions || []).join(', ')}" onchange="appData.devices[${idx}].actions = this.value.split(',').map(s=>s.trim()).filter(Boolean)" class="w-full bg-slate-950 border border-slate-700 px-2.5 py-1.5 rounded-xl font-mono text-xs text-emerald-300">
          </div>
          <div>
            <label class="text-[10px] text-slate-400 font-bold block mb-0.5">Befehl ({action} wird ersetzt)</label>
            <input type="text" value="${dev.command || ''}" onchange="appData.devices[${idx}].command = this.value" class="w-full bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded-xl font-mono text-xs text-amber-300">
          </div>
        `;
        list.appendChild(item);
      });
      document.getElementById('deviceModal').classList.remove('hidden');
    }

    function deleteDeviceConfirm(idx) {
      const dev = appData.devices[idx];
      if (confirm(`"${dev.name || dev.id}" wirklich loeschen?`)) {
        appData.devices.splice(idx, 1);
        appData.layout.placements = (appData.layout.placements || []).filter(p => p.device_id !== dev.id);
        delete appData.states[dev.id];
        openDeviceEditor();
        renderCanvas();
        renderPalette();
      }
    }

    function addNewDevice() {
      const id = "dev_" + Date.now();
      appData.devices.push({
        id: id,
        name: "Neues Geraet",
        type: "light",
        icon: "fa-lightbulb",
        description: `Schaltet ${id}.`,
        aliases: [id],
        actions: ["on", "off", "status"],
        command: `python3 tool_scripte/steckdose.py --device ${id} --action {action}`
      });
      openDeviceEditor();
      renderPalette();
    }

    async function saveDevices() {
      const globalInput = document.getElementById('globalToolDescInput');
      appData.needle_config = appData.needle_config || {};
      appData.needle_config.tool_description = globalInput ? globalInput.value.trim() : "";

      const res = await fetch('/api/devices/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          devices: appData.devices,
          needle_config: appData.needle_config
        })
      });
      const data = await res.json();
      if (data.states) appData.states = data.states;
      if (data.layout) appData.layout = data.layout;
      closeDeviceEditor();
      renderCanvas();
      renderPalette();
    }

    function closeDeviceEditor() { document.getElementById('deviceModal').classList.add('hidden'); }

    window.onload = () => {
      loadTabletConfig();
      setupViewportGestures();
      loadState();
    };
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    start_continuous_stt()
    cfg = get_log_config()
    print("==================================================")
    print("  Smart Home Server aktiv: http://0.0.0.0:5000")
    print("  Log-Level:              ", cfg.get("log_level", "INFO"))
    print("  STT Raw Logging:        ", "AKTIV" if cfg.get("log_stt_raw") else "DEAKTIVIERT")
    print("  Pi-Mikrofon lauscht auf:", WAKEWORDS)
    print("==================================================")
    app.run(host="0.0.0.0", port=5000, debug=False)