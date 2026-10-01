#!/usr/bin/env python3
import io
import json
import os
import re
import shlex
import subprocess
import threading
import wave
from typing import Literal

from flask import Flask, Response, jsonify, request
import needle
from vosk import KaldiRecognizer, Model

app = Flask(__name__)

DEVICES_FILE = "devices.json"
STATE_FILE = "device_states.json"
LAYOUT_FILE = "layout.json"
BLACKLIST_FILE = "blacklist.txt"
REPLACEMENTS_FILE = "replacements.txt"
VOSK_MODEL_PATH = "model"

_hardware_lock = threading.Lock()

print("Lade Vosk-Modell fuer Sprachaufnahme...")
if os.path.exists(VOSK_MODEL_PATH):
    vosk_model = Model(VOSK_MODEL_PATH)
else:
    print(f"Hinweis: '{VOSK_MODEL_PATH}' nicht gefunden. Lokale Spracheingabe im Browser deaktiviert.")
    vosk_model = None


# --- Persistenz (Keine Dummy-Räume, nur existierende Dateien) ---

def load_json(filepath, default_val):
    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(default_val, f, indent=2, ensure_ascii=False)
        return default_val
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if data is not None else default_val
    except Exception as e:
        print(f"Fehler beim Laden von {filepath}: {e}")
        return default_val


def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_devices():
    devices = load_json(DEVICES_FILE, [])
    # Sicherstellen, dass jedes Gerät mindestens die Standard-Aktionen hat
    for d in devices:
        if "actions" not in d or not d["actions"]:
            d["actions"] = ["on", "off", "status"]
        if "command" not in d or not d["command"]:
            d["command"] = f"python3 tool_scripte/steckdose.py --device {d.get('id', 'dev')} --action {{action}}"
    return devices


def get_states():
    return load_json(STATE_FILE, {})


def get_layout():
    return load_json(LAYOUT_FILE, {"rooms": [], "placements": []})


def update_state(device_id, action):
    states = get_states()
    states[device_id] = action
    save_json(STATE_FILE, states)


def load_replacements():
    replacements = []
    if not os.path.exists(REPLACEMENTS_FILE):
        return replacements
    with open(REPLACEMENTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if "->" in line:
                s, t = line.strip().split("->", 1)
                replacements.append((s.strip().lower(), t.strip()))
    return replacements


def load_blacklist():
    if not os.path.exists(BLACKLIST_FILE):
        return set()
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


def clean_text(text):
    text = text.strip().lower()
    for src, target in load_replacements():
        text = re.sub(r"\b" + re.escape(src) + r"\b", target, text)
    bl = load_blacklist()
    words = [w for w in text.split() if w not in bl]
    return " ".join(words).strip()


# --- Hardware-Ausführung ---

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

    print(f"\n[Hardware-Ausfuehrung]: {formatted_cmd}")
    try:
        proc = subprocess.run(
            shlex.split(formatted_cmd),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        stdout = proc.stdout.strip()
        try:
            data = json.loads(stdout)
        except Exception:
            data = stdout or proc.stderr.strip()

        # Automatische Aktualisierung von device_states.json
        if proc.returncode == 0:
            update_state(device_id, action)

        return {"success": proc.returncode == 0, "device": device_id, "action": action, "output": data}
    except Exception as ex:
        return {"success": False, "device": device_id, "error": str(ex)}


# --- Geräte-Auflösung & Needle 3 Tool ---

def resolve_devices(device_query: str):
    devices = get_devices()
    q = device_query.lower().strip()
    matched = []

    # 1. Direkter Namens- oder Rufnamen-Treffer
    for d in devices:
        d_id = d.get("id", "").lower()
        d_name = d.get("name", "").lower()
        if d_id == q or d_name == q:
            matched.append(d)
            continue
        aliases = [a.lower().strip() for a in d.get("aliases", [])]
        if q in aliases or any(a in q for a in aliases if len(a) > 2):
            matched.append(d)

    # 2. Allgemeine Gruppenbegriffe (z. B. "alle lichter", "alle")
    if not matched:
        if any(w in q for w in ["light", "licht", "lampe", "lights"]):
            matched = [d for d in devices if d.get("type") == "light"]
        elif any(w in q for w in ["fan", "ventilator", "luefter"]):
            matched = [d for d in devices if d.get("type") == "fan"]
        elif "all" in q or "alle" in q:
            matched = devices

    # Duplikate filtern
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

    # Dynamische Aktionen aller konfigurierten Geräte einsammeln (z.B. on, off, links, rechts)
    all_actions = get_all_configured_actions(devices)
    ActionType = Literal[tuple(all_actions)]

    action_words = set(all_actions) | {"an", "aus", "auf", "zu"}
    action_regex = r"\b(" + "|".join(re.escape(w) for w in action_words) + r")\b"

    @needle.tool(
        triggers=[
            r"\b(turn|switch|power|flip|mach|schalte|fahre|drehe|stelle|wie|status|set)\b",
            action_regex,
        ]
    )
    def control_device(device: str, action: ActionType):
        """Switch, query or control any smart-home device using the requested action."""
        resolved = resolve_devices(device)
        if not resolved:
            res = {"error": f"Kein passendes Geraet fuer '{device}' gefunden."}
            executed_results.append(res)
            return res

        current_states = get_states()
        for target in resolved:
            dev_id = target["id"]
            curr = current_states.get(dev_id, "unknown")

            eff_action = action
            if action == "toggle":
                eff_action = "off" if curr == "on" else "on"

            # Status-Gatekeeper: Nur bei on/off filtern
            if eff_action in ["on", "off"] and curr == eff_action:
                res = {"device": dev_id, "action": eff_action, "skipped": True, "message": f"bereits {curr}"}
                executed_results.append(res)
                continue

            res = execute_device_command(dev_id, eff_action)
            executed_results.append(res)

        return executed_results

    # Umgangssprache filtern
    t = raw_text.lower()
    t = re.sub(r"\bauf\b", "on", t)
    t = re.sub(r"\ban\b", "on", t)
    t = re.sub(r"\baus\b", "off", t)
    t = re.sub(r"\bliving room\b", "livingroom", t)
    t = re.sub(r"\bbett zimmer\b", "bedroom", t)

    print(f"\n[Anfrage an Needle]: '{t}'")
    agent = needle.Needle(tools=[control_device])
    agent_output = agent.run(t)
    print(f"[Needle Ausgabe]: {agent_output}")

    return {
        "input_command": raw_text,
        "normalized": t,
        "needle_output": agent_output,
        "actions_executed": executed_results,
        "states": get_states(),
    }


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
    })


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

    return jsonify({"result": res, "states": get_states()})


@app.route("/api/needle/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Leerer Text"}), 400

    cleaned = clean_text(text)
    with _hardware_lock:
        outcome = run_needle_command(cleaned)

    return jsonify(outcome)


@app.route("/api/audio/transcribe", methods=["POST"])
def api_audio():
    if not vosk_model:
        return jsonify({"error": "Vosk-Modell ist auf dem Server nicht geladen."}), 500

    if "audio" not in request.files:
        return jsonify({"error": "Keine Audiodatei empfangen."}), 400

    file = request.files["audio"]
    audio_bytes = file.read()

    try:
        wf = wave.open(io.BytesIO(audio_bytes), "rb")
        rec = KaldiRecognizer(vosk_model, wf.getframerate())
        rec.SetWords(True)

        while True:
            chunk = wf.readframes(4000)
            if len(chunk) == 0:
                break
            rec.AcceptWaveform(chunk)

        final_res = json.loads(rec.FinalResult())
        recognized_text = final_res.get("text", "").strip()
    except Exception as ex:
        return jsonify({"error": f"Audio-Dekodierung fehlgeschlagen: {ex}"}), 400

    if not recognized_text:
        return jsonify({"transcript": "", "message": "Keine Sprache erkannt."})

    cleaned = clean_text(recognized_text)
    with _hardware_lock:
        outcome = run_needle_command(cleaned)
    outcome["transcript"] = recognized_text
    outcome["cleaned"] = cleaned

    return jsonify(outcome)


@app.route("/api/layout/save", methods=["POST"])
def api_save_layout():
    data = request.json or {}
    save_json(LAYOUT_FILE, data)
    return jsonify({"success": True})


@app.route("/api/devices/save", methods=["POST"])
def api_save_devices():
    devices_data = request.json or []
    save_json(DEVICES_FILE, devices_data)

    current_states = get_states()
    valid_ids = {d["id"] for d in devices_data}
    cleaned_states = {k: v for k, v in current_states.items() if k in valid_ids}

    for d in devices_data:
        if d["id"] not in cleaned_states:
            cleaned_states[d["id"]] = "off"

    save_json(STATE_FILE, cleaned_states)

    # Layout-Platzierungen von gelöschten Geräten bereinigen
    layout = get_layout()
    layout["placements"] = [p for p in layout.get("placements", []) if p.get("device_id") in valid_ids]
    save_json(LAYOUT_FILE, layout)

    return jsonify({"success": True, "states": cleaned_states, "layout": layout})


# --- Single Page Web Interface (HTML, Tailwind CSS, JS) ---

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="de" class="h-full bg-slate-950 text-slate-100">
<head>
  <meta charset="UTF-8">
  <title>Smart Home OS - Canvas & Needle 3</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
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
    .resize-handle {
      width: 18px;
      height: 18px;
      position: absolute;
      right: 2px;
      bottom: 2px;
      cursor: se-resize;
    }
  </style>
</head>
<body class="h-full flex flex-col font-sans select-none overflow-hidden">

  <!-- Header -->
  <header class="h-14 border-b border-slate-800 bg-slate-900/90 px-5 flex items-center justify-between z-20">
    <div class="flex items-center gap-3">
      <div class="w-8 h-8 rounded-lg bg-amber-500/20 text-amber-400 flex items-center justify-center font-bold">
        <i class="fa-solid fa-house-signal"></i>
      </div>
      <h1 class="font-bold tracking-wide text-lg text-white">Home Canvas <span class="text-xs bg-slate-800 text-slate-400 px-2 py-0.5 rounded border border-slate-700">Needle 3</span></h1>
    </div>

    <!-- Modus-Auswahl & Speichern -->
    <div class="flex items-center gap-3">
      <div class="flex bg-slate-800 p-1 rounded-lg border border-slate-700 text-xs font-semibold">
        <button id="btnModeLive" onclick="setMode('live')" class="px-3 py-1 rounded bg-amber-500 text-slate-950 font-bold transition">Steuerung</button>
        <button id="btnModeEdit" onclick="setMode('edit')" class="px-3 py-1 rounded text-slate-400 hover:text-white transition">Grundriss-Editor</button>
      </div>

      <button onclick="openDeviceEditor()" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold flex items-center gap-1.5 transition">
        <i class="fa-solid fa-sliders text-amber-400"></i> Tool- & Geraete-Editor
      </button>

      <button id="btnSaveLayout" onclick="saveLayout()" class="hidden px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold items-center gap-1.5 shadow transition">
        <i class="fa-solid fa-floppy-disk"></i> Layout sichern
      </button>
    </div>
  </header>

  <!-- Main View Area -->
  <div class="flex-1 flex overflow-hidden">
    
    <!-- Linke Geräte-Leiste (Nur im Editor-Modus sichtbar, zum Hineinziehen) -->
    <aside id="devicePalette" class="hidden w-72 border-r border-slate-800 bg-slate-900/90 flex flex-col z-20">
      <div class="p-3 border-b border-slate-800 flex items-center justify-between">
        <span class="text-xs font-bold uppercase tracking-wider text-slate-300">Geraete auf Plan ziehen</span>
        <button onclick="addRoom()" class="px-2 py-1 bg-amber-500/10 text-amber-400 hover:bg-amber-500 hover:text-slate-950 rounded text-[11px] font-bold transition">
          <i class="fa-solid fa-plus mr-1"></i> Raum
        </button>
      </div>
      <div class="p-2 text-[11px] text-slate-400 bg-slate-950/40 border-b border-slate-800/60">
        Ziehe ein Geraet mit der Maus auf den Grundriss oder klicke auf das Plus-Symbol.
      </div>
      <div id="paletteDeviceList" class="flex-1 overflow-y-auto p-3 space-y-2">
        <!-- Geraete-Karten zum Hineinziehen -->
      </div>
    </aside>

    <!-- Floorplan Canvas -->
    <main class="flex-1 relative overflow-auto grid-bg p-8" id="viewport">
      <div id="canvas" 
           ondragover="event.preventDefault()" 
           ondrop="onCanvasDrop(event)"
           class="relative min-w-[1000px] min-h-[700px] border border-dashed border-slate-800 rounded-2xl bg-slate-900/40">
        <!-- Raeume und Geraete -->
      </div>
    </main>

    <!-- Right Sidebar: Needle Assistant & Speech -->
    <aside class="w-96 border-l border-slate-800 bg-slate-900/80 flex flex-col z-20">
      <div class="p-3 border-b border-slate-800 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse"></span>
          <span class="text-xs font-bold tracking-wider text-slate-300 uppercase">Needle 3 Assistent</span>
        </div>
        <span id="recordingBadge" class="hidden text-xs bg-rose-500 text-white px-2 py-0.5 rounded-full font-bold animate-bounce">Aufnahme...</span>
      </div>

      <!-- Chat Log -->
      <div id="chatLog" class="flex-1 overflow-y-auto p-4 space-y-3 text-xs">
        <div class="p-2.5 bg-slate-800/80 rounded-xl border border-slate-700/60 text-slate-300">
          <i class="fa-solid fa-circle-info text-amber-400 mr-1"></i> Klicke auf Geraete zum Schalten oder sprich Befehle wie <i>"livingroom light on"</i> oder <i>"ventilator rechts"</i>.
        </div>
      </div>

      <!-- Controls & Input -->
      <div class="p-3 border-t border-slate-800 bg-slate-900 space-y-2">
        <div class="flex gap-2">
          <input id="chatInput" type="text" placeholder="Befehl an Needle schreiben..." 
                 class="flex-1 bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-amber-400"
                 onkeydown="if(event.key==='Enter') sendTextCommand()">
          <button onclick="sendTextCommand()" class="px-3 bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold rounded-xl text-xs transition">
            <i class="fa-solid fa-paper-plane"></i>
          </button>
        </div>

        <!-- Voice Button -->
        <button id="btnMic" onmousedown="startRecording()" onmouseup="stopRecording()" 
                class="w-full py-3 bg-slate-800 hover:bg-slate-700 active:bg-rose-600 border border-slate-700 rounded-xl text-xs font-bold text-white flex items-center justify-center gap-2 transition cursor-pointer">
          <i class="fa-solid fa-microphone text-rose-400" id="micIcon"></i>
          <span id="micText">Gedrueckt halten zum Sprechen (Vosk)</span>
        </button>
      </div>
    </aside>
  </div>

  <!-- Device Context Action Menu (Live Mode) -->
  <div id="deviceActionMenu" class="hidden fixed bg-slate-900 border border-slate-700 rounded-xl shadow-2xl p-2 z-50 text-xs flex flex-col gap-1 min-w-[150px]">
    <div id="actionMenuTitle" class="font-bold text-slate-400 px-2 py-1 border-b border-slate-800">Aktionen</div>
    <div id="actionMenuButtons" class="flex flex-col gap-1 mt-1"></div>
  </div>

  <!-- Device & Tool Manager Modal -->
  <div id="deviceModal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-800 w-full max-w-4xl rounded-2xl p-5 shadow-2xl flex flex-col max-h-[92vh]">
      <div class="flex items-center justify-between pb-3 border-b border-slate-800">
        <div>
          <h2 class="font-bold text-base text-white flex items-center gap-2">
            <i class="fa-solid fa-sliders text-amber-400"></i> Geraete- & Tool-Konfigurator
          </h2>
          <span class="text-[11px] text-slate-400">Verwalte hier deine Geraete, eigene Parameter (z.B. links, rechts) und wie Needle sie per Sprache anspricht.</span>
        </div>
        <button onclick="closeDeviceEditor()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>

      <div id="deviceList" class="flex-1 overflow-y-auto py-4 space-y-4 text-xs pr-1">
        <!-- Dynamische Geraete-Karten -->
      </div>

      <div class="pt-3 border-t border-slate-800 flex justify-between items-center">
        <button onclick="addNewDevice()" class="px-3.5 py-2 bg-slate-800 hover:bg-slate-700 text-white rounded-lg text-xs font-semibold flex items-center gap-1.5">
          <i class="fa-solid fa-plus text-amber-400"></i> Neues Geraet anlegen
        </button>
        <button onclick="saveDevices()" class="px-5 py-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg text-xs font-bold shadow flex items-center gap-1.5">
          <i class="fa-solid fa-check"></i> Speichern & Uebernehmen
        </button>
      </div>
    </div>
  </div>

  <script>
    let appData = { devices: [], states: {}, layout: { rooms: [], placements: [] } };
    let currentMode = 'live';
    let dragOffset = { x: 0, y: 0 };

    let audioContext = null;
    let mediaStream = null;
    let audioProcessor = null;
    let audioChunks = [];

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

    async function loadState() {
      const res = await fetch('/api/state');
      appData = await res.json();
      renderCanvas();
      renderPalette();
    }

    function setMode(mode) {
      currentMode = mode;
      document.getElementById('btnModeLive').className = mode === 'live' 
        ? "px-3 py-1 rounded bg-amber-500 text-slate-950 font-bold transition"
        : "px-3 py-1 rounded text-slate-400 hover:text-white transition";

      document.getElementById('btnModeEdit').className = mode === 'edit'
        ? "px-3 py-1 rounded bg-amber-500 text-slate-950 font-bold transition"
        : "px-3 py-1 rounded text-slate-400 hover:text-white transition";

      document.getElementById('devicePalette').classList.toggle('hidden', mode !== 'edit');
      document.getElementById('btnSaveLayout').classList.toggle('hidden', mode !== 'edit');
      closeActionMenu();
      renderCanvas();
      renderPalette();
    }

    // --- Palette fuer Geraete im Editor ---

    function renderPalette() {
      const container = document.getElementById('paletteDeviceList');
      if (!container) return;
      container.innerHTML = '';

      const placedIds = new Set((appData.layout.placements || []).map(p => p.device_id));

      (appData.devices || []).forEach(dev => {
        const isPlaced = placedIds.has(dev.id);
        const card = document.createElement('div');
        card.draggable = true;
        card.className = `p-2.5 rounded-xl border flex items-center justify-between transition cursor-grab active:cursor-grabbing ${
          isPlaced ? 'bg-slate-950/40 border-slate-800 text-slate-400' : 'bg-slate-800 border-slate-700 text-slate-100 hover:border-amber-400'
        }`;

        card.ondragstart = (e) => {
          e.dataTransfer.setData('text/plain', dev.id);
        };

        const iconClass = dev.icon || 'fa-plug';
        card.innerHTML = `
          <div class="flex items-center gap-2.5 overflow-hidden">
            <div class="w-7 h-7 rounded-lg bg-slate-900 flex items-center justify-center text-amber-400">
              <i class="fa-solid ${iconClass} text-xs"></i>
            </div>
            <div class="truncate">
              <div class="text-xs font-bold truncate">${dev.name || dev.id}</div>
              <div class="text-[10px] text-slate-500 font-mono">${dev.id}</div>
            </div>
          </div>
          <div>
            ${isPlaced 
              ? `<span class="text-[10px] text-emerald-400 font-semibold px-2 py-0.5 rounded bg-emerald-500/10"><i class="fa-solid fa-check"></i> Platziert</span>` 
              : `<button onclick="placeDeviceOnCanvas('${dev.id}', 100, 100)" class="px-2 py-1 bg-amber-500 hover:bg-amber-400 text-slate-950 rounded text-[10px] font-bold transition">+ Setzen</button>`}
          </div>
        `;
        container.appendChild(card);
      });
    }

    function onCanvasDrop(event) {
      event.preventDefault();
      const devId = event.dataTransfer.getData('text/plain');
      if (!devId) return;

      const canvas = document.getElementById('canvas');
      const rect = canvas.getBoundingClientRect();
      const dropX = Math.max(20, Math.round((event.clientX - rect.left - 32) / 10) * 10);
      const dropY = Math.max(20, Math.round((event.clientY - rect.top - 32) / 10) * 10);

      placeDeviceOnCanvas(devId, dropX, dropY);
    }

    function placeDeviceOnCanvas(devId, x, y) {
      appData.layout.placements = appData.layout.placements || [];
      const existing = appData.layout.placements.find(p => p.device_id === devId);
      if (existing) {
        existing.x = x;
        existing.y = y;
      } else {
        appData.layout.placements.push({ device_id: devId, x: x, y: y });
      }
      renderCanvas();
      renderPalette();
    }

    function unplaceDevice(devId) {
      appData.layout.placements = (appData.layout.placements || []).filter(p => p.device_id !== devId);
      renderCanvas();
      renderPalette();
    }

    // --- Render Canvas (Grundriss) ---

    function renderCanvas() {
      const canvas = document.getElementById('canvas');
      canvas.innerHTML = '';

      // 1. Raeume rendern
      (appData.layout.rooms || []).forEach((room, idx) => {
        const rEl = document.createElement('div');
        rEl.className = `absolute rounded-xl border border-slate-700/80 bg-slate-800/40 p-3 transition-colors ${currentMode === 'edit' ? 'cursor-move ring-1 ring-amber-500/40' : ''}`;
        rEl.style.left = room.x + 'px';
        rEl.style.top = room.y + 'px';
        rEl.style.width = room.w + 'px';
        rEl.style.height = room.h + 'px';

        rEl.innerHTML = `
          <div class="flex items-center justify-between text-xs font-bold text-slate-400 select-none">
            <span><i class="fa-solid fa-vector-square mr-1 text-slate-500"></i> ${room.name}</span>
            <span class="text-[10px] text-slate-600 font-mono">${room.w}x${room.h}</span>
            ${currentMode === 'edit' ? `<button onclick="deleteRoom(${idx})" class="text-rose-400 hover:text-rose-300 ml-2"><i class="fa-solid fa-trash"></i></button>` : ''}
          </div>
        `;

        if (currentMode === 'edit') {
          makeDraggable(rEl, (x, y) => { room.x = x; room.y = y; });

          // Resize Handle unten rechts am Raum
          const resizeHandle = document.createElement('div');
          resizeHandle.className = 'resize-handle text-slate-500 hover:text-amber-400 flex items-center justify-center';
          resizeHandle.innerHTML = '<i class="fa-solid fa-grip-lines-vertical rotate-45 text-[10px]"></i>';

          resizeHandle.onmousedown = (e) => {
            e.stopPropagation();
            const startX = e.clientX;
            const startY = e.clientY;
            const startW = room.w;
            const startH = room.h;

            function onMouseMove(ev) {
              const newW = Math.max(100, Math.round((startW + (ev.clientX - startX)) / 10) * 10);
              const newH = Math.max(80, Math.round((startH + (ev.clientY - startY)) / 10) * 10);
              room.w = newW;
              room.h = newH;
              rEl.style.width = newW + 'px';
              rEl.style.height = newH + 'px';
              const label = rEl.querySelector('.font-mono');
              if (label) label.innerText = `${newW}x${newH}`;
            }

            function onMouseUp() {
              document.removeEventListener('mousemove', onMouseMove);
              document.removeEventListener('mouseup', onMouseUp);
            }

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
          };

          rEl.appendChild(resizeHandle);
        }

        canvas.appendChild(rEl);
      });

      // 2. Geraete rendern
      (appData.layout.placements || []).forEach((p) => {
        const dev = appData.devices.find(d => d.id === p.device_id) || { id: p.device_id, name: p.device_id, icon: 'fa-plug', type: 'custom', actions: ['on', 'off'] };
        const state = appData.states[p.device_id] || 'off';
        const isOn = state === 'on';

        const dEl = document.createElement('div');
        dEl.className = `absolute z-10 w-16 h-16 rounded-2xl flex flex-col items-center justify-center p-1.5 transition-all duration-300 border 
          ${isOn ? 'bg-amber-500/20 border-amber-400 glow-on text-amber-300' : 'bg-slate-800/90 border-slate-700 text-slate-400'}
          ${currentMode === 'edit' ? 'cursor-grab ring-2 ring-blue-500/50' : 'cursor-pointer hover:scale-105 active:scale-95'}`;

        dEl.style.left = p.x + 'px';
        dEl.style.top = p.y + 'px';

        const iconClass = dev.icon || 'fa-plug';
        dEl.innerHTML = `
          ${currentMode === 'edit' ? `<button onclick="unplaceDevice('${dev.id}')" title="Vom Plan entfernen" class="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-rose-500 text-white flex items-center justify-center text-[10px] shadow hover:bg-rose-600"><i class="fa-solid fa-xmark"></i></button>` : ''}
          <i class="fa-solid ${iconClass} ${dev.type === 'fan' && isOn ? 'fa-spin' : ''} text-lg mb-1"></i>
          <span class="text-[9px] font-bold text-center leading-tight truncate w-full px-1">${dev.name || dev.id}</span>
          <span class="text-[8px] uppercase tracking-wider font-extrabold ${isOn ? 'text-amber-400' : 'text-slate-500'}">${state}</span>
        `;

        if (currentMode === 'live') {
          // Linksklick: Toggle oder erste definierte Aktion
          dEl.onclick = () => {
            const nextAction = (state === 'on') ? 'off' : 'on';
            triggerDeviceAction(p.device_id, nextAction);
          };
          // Rechtsklick: Zeigt alle fuer dieses Geraet konfigurierten Aktionen (z.B. links, rechts, on, off)
          dEl.oncontextmenu = (e) => {
            e.preventDefault();
            openActionMenu(e.clientX, e.clientY, dev);
          };
        } else {
          makeDraggable(dEl, (x, y) => { p.x = x; p.y = y; });
        }

        canvas.appendChild(dEl);
      });
    }

    // --- Kontext-Aktionsmenue (Live-Modus) ---

    function openActionMenu(x, y, dev) {
      const menu = document.getElementById('deviceActionMenu');
      const title = document.getElementById('actionMenuTitle');
      const container = document.getElementById('actionMenuButtons');

      title.innerText = dev.name || dev.id;
      container.innerHTML = '';

      const actions = (dev.actions && dev.actions.length > 0) ? dev.actions : ['on', 'off', 'status'];

      actions.forEach(act => {
        const btn = document.createElement('button');
        btn.className = "px-3 py-1.5 rounded bg-slate-800 hover:bg-amber-500 hover:text-slate-950 text-left font-semibold capitalize transition flex items-center justify-between";
        btn.innerHTML = `<span>${act}</span> <i class="fa-solid fa-play text-[9px] opacity-60"></i>`;
        btn.onclick = () => {
          triggerDeviceAction(dev.id, act);
          closeActionMenu();
        };
        container.appendChild(btn);
      });

      menu.style.left = x + 'px';
      menu.style.top = y + 'px';
      menu.classList.remove('hidden');
    }

    function closeActionMenu() {
      document.getElementById('deviceActionMenu').classList.add('hidden');
    }

    window.addEventListener('click', (e) => {
      if (!e.target.closest('#deviceActionMenu')) closeActionMenu();
    });

    // --- Drag-Helfer fuer Canvas ---

    function makeDraggable(el, onMove) {
      el.onmousedown = (e) => {
        if (e.target.tagName === 'BUTTON' || e.target.tagName === 'I' || e.target.classList.contains('resize-handle')) return;
        const rect = el.getBoundingClientRect();
        const canvasRect = document.getElementById('canvas').getBoundingClientRect();
        dragOffset.x = e.clientX - rect.left;
        dragOffset.y = e.clientY - rect.top;

        function onMouseMove(ev) {
          let newX = Math.max(0, ev.clientX - canvasRect.left - dragOffset.x);
          let newY = Math.max(0, ev.clientY - canvasRect.top - dragOffset.y);
          newX = Math.round(newX / 10) * 10;
          newY = Math.round(newY / 10) * 10;
          el.style.left = newX + 'px';
          el.style.top = newY + 'px';
          onMove(newX, newY);
        }

        function onMouseUp() {
          document.removeEventListener('mousemove', onMouseMove);
          document.removeEventListener('mouseup', onMouseUp);
        }

        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
      };
    }

    function addRoom() {
      const name = prompt("Name des neuen Raumes (z.B. Wohnzimmer, Flur):", "Neuer Raum");
      if (!name) return;
      appData.layout.rooms = appData.layout.rooms || [];
      appData.layout.rooms.push({
        id: "room_" + Date.now(),
        name: name,
        x: 60,
        y: 60,
        w: 260,
        h: 220
      });
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
      logChat("System", "Layout & Raumgroessen gespeichert.");
      setMode('live');
    }

    async function triggerDeviceAction(deviceId, action) {
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
      if (data.result && data.result.output) {
        logChat("Home", `${deviceId} (${action}): ${JSON.stringify(data.result.output)}`);
      }
    }

    // --- Chat & Needle 3 Interaktion ---

    function logChat(author, text, extra = "") {
      const box = document.getElementById('chatLog');
      const item = document.createElement('div');
      item.className = "p-2.5 rounded-xl border bg-slate-800/90 border-slate-700 text-slate-200";
      item.innerHTML = `
        <div class="flex items-center justify-between font-bold mb-1">
          <span class="${author === 'Du' ? 'text-amber-400' : 'text-emerald-400'}">${author}</span>
          <span class="text-[10px] text-slate-500">${new Date().toLocaleTimeString()}</span>
        </div>
        <div>${text}</div>
        ${extra ? `<div class="mt-1 text-[10px] text-slate-400 bg-slate-900/80 p-1.5 rounded font-mono">${extra}</div>` : ''}
      `;
      box.appendChild(item);
      box.scrollTop = box.scrollHeight;
    }

    async function sendTextCommand() {
      const input = document.getElementById('chatInput');
      const val = input.value.trim();
      if (!val) return;
      input.value = '';

      logChat("Du", val);

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

      const summary = (data.actions_executed || []).map(a => `${a.device} -> ${a.action} (${a.skipped ? 'uebersprungen' : 'ausgefuehrt'})`).join(', ');
      logChat("Needle 3", summary || "Befehl ausgefuehrt.");
    }

    // --- Audio-Aufnahme im Browser & Vosk-WAV ---

    async function startRecording() {
      audioChunks = [];
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaStream = stream;

      audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      const source = audioContext.createMediaStreamSource(stream);
      audioProcessor = audioContext.createScriptProcessor(4096, 1, 1);

      audioProcessor.onaudioprocess = (e) => {
        const inputData = e.inputBuffer.getChannelData(0);
        audioChunks.push(new Float32Array(inputData));
      };

      source.connect(audioProcessor);
      audioProcessor.connect(audioContext.destination);

      document.getElementById('recordingBadge').classList.remove('hidden');
      document.getElementById('micText').innerText = "Hoere zu... (Loslassen zum Senden)";
    }

    async function stopRecording() {
      document.getElementById('recordingBadge').classList.add('hidden');
      document.getElementById('micText').innerText = "Gedrueckt halten zum Sprechen (Vosk)";

      if (audioProcessor) audioProcessor.disconnect();
      if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());

      const wavBlob = encodeWAV(audioChunks, 16000);
      const formData = new FormData();
      formData.append("audio", wavBlob, "voice.wav");

      logChat("System", "Sende Sprache an Vosk...");

      const res = await fetch('/api/audio/transcribe', {
        method: 'POST',
        body: formData
      });
      const data = await res.json();

      if (data.transcript) {
        logChat("Vosk", `Erkannt: "${data.transcript}"`);
        if (data.states) {
          appData.states = data.states;
          renderCanvas();
        }
        const summary = (data.actions_executed || []).map(a => `${a.device} -> ${a.action}`).join(', ');
        logChat("Needle 3", summary || "Befehl verarbeitet.");
      } else {
        logChat("Vosk", "Kein Sprachbefehl erkannt.");
      }
    }

    function encodeWAV(samplesList, sampleRate) {
      let totalLength = samplesList.reduce((acc, cur) => acc + cur.length, 0);
      let merged = new Float32Array(totalLength);
      let offset = 0;
      for (let chunk of samplesList) {
        merged.set(chunk, offset);
        offset += chunk.length;
      }

      let buffer = new ArrayBuffer(44 + merged.length * 2);
      let view = new DataView(buffer);

      function writeString(view, offset, string) {
        for (let i = 0; i < string.length; i++) {
          view.setUint8(offset + i, string.charCodeAt(i));
        }
      }

      writeString(view, 0, 'RIFF');
      view.setUint32(4, 36 + merged.length * 2, true);
      writeString(view, 8, 'WAVE');
      writeString(view, 12, 'fmt ');
      view.setUint32(16, 16, true);
      view.setUint16(20, 1, true);
      view.setUint16(22, 1, true);
      view.setUint32(24, sampleRate, true);
      view.setUint32(28, sampleRate * 2, true);
      view.setUint16(32, 2, true);
      view.setUint16(34, 16, true);
      writeString(view, 36, 'data');
      view.setUint32(40, merged.length * 2, true);

      let index = 44;
      for (let i = 0; i < merged.length; i++) {
        let s = Math.max(-1, Math.min(1, merged[i]));
        view.setInt16(index, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
        index += 2;
      }
      return new Blob([view], { type: 'audio/wav' });
    }

    // --- Selbsterklaerender Tool- & Geraete-Editor ---

    function openDeviceEditor() {
      const list = document.getElementById('deviceList');
      list.innerHTML = '';

      appData.devices.forEach((dev, idx) => {
        dev.actions = dev.actions || ['on', 'off', 'status'];

        const item = document.createElement('div');
        item.className = "p-4 bg-slate-950 rounded-2xl border border-slate-800 space-y-3 relative";

        item.innerHTML = `
          <!-- Zeile 1: Name, Typ, Icon, Loeschen -->
          <div class="flex gap-2 items-center">
            <div class="flex-1">
              <label class="text-[10px] text-slate-400 font-bold block mb-0.5">Anzeigename im Grundriss</label>
              <input type="text" value="${dev.name || dev.id}" placeholder="z.B. Wohnzimmer Stehlampe" 
                     onchange="appData.devices[${idx}].name = this.value"
                     class="w-full bg-slate-900 border border-slate-700 px-3 py-1.5 rounded-xl text-white font-bold text-xs">
            </div>
            
            <div class="w-32">
              <label class="text-[10px] text-slate-400 font-bold block mb-0.5">Geraetetyp</label>
              <input type="text" value="${dev.type || 'custom'}" placeholder="z.B. light, fan, tv, rollo"
                     onchange="appData.devices[${idx}].type = this.value"
                     class="w-full bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded-xl text-white text-xs font-semibold">
            </div>

            <div class="w-28">
              <label class="text-[10px] text-slate-400 font-bold block mb-0.5">Symbol / Icon</label>
              <select onchange="appData.devices[${idx}].icon = this.value; renderCanvas(); openDeviceEditor();" 
                      class="w-full bg-slate-900 border border-slate-700 px-2 py-1.5 rounded-xl text-white text-xs">
                ${PRESET_ICONS.map(i => `<option value="${i.id}" ${dev.icon === i.id ? 'selected':''}>${i.label}</option>`).join('')}
              </select>
            </div>

            <div class="pt-4">
              <button onclick="deleteDeviceConfirm(${idx})" 
                      class="w-8 h-8 rounded-xl bg-rose-500/10 hover:bg-rose-500 text-rose-400 hover:text-white flex items-center justify-center transition" title="Geraet komplett loeschen">
                <i class="fa-solid fa-trash text-xs"></i>
              </button>
            </div>
          </div>

          <!-- Zeile 2: Geraete-Kennung und Sprachbefehle / Rufnamen -->
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-[10px] text-amber-400 font-bold block mb-0.5">
                <i class="fa-solid fa-fingerprint mr-1"></i> Geraete-Kennung / System-ID
              </label>
              <span class="text-[9px] text-slate-500 block mb-1">Eindeutiger Bezeichner fuer Skripte (z.B. <code>livingroom_light</code>). Wird an <code>--device</code> uebergeben.</span>
              <input type="text" value="${dev.id}" onchange="appData.devices[${idx}].id = this.value" 
                     class="w-full bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded-lg font-mono text-xs text-slate-300">
            </div>

            <div>
              <label class="text-[10px] text-amber-400 font-bold block mb-0.5">
                <i class="fa-solid fa-microphone mr-1"></i> Sprachbefehle / Rufnamen (Worauf hoert das Geraet?)
              </label>
              <span class="text-[9px] text-slate-500 block mb-1">Kommagetrennt. Woerter, bei denen Needle dieses Geraet erkennt (z.B. <code>wohnzimmer, couchlicht, lampe</code>).</span>
              <input type="text" value="${(dev.aliases || []).join(', ')}" 
                     placeholder="z.B. wohnzimmer, wohnzimmer licht, couchlicht"
                     onchange="appData.devices[${idx}].aliases = this.value.split(',').map(s=>s.trim()).filter(Boolean)" 
                     class="w-full bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded-lg text-xs text-slate-300">
            </div>
          </div>

          <!-- Zeile 3: Frei anpassbare Aktionen / Parameter (on, off, rechts, links etc.) -->
          <div class="bg-slate-900/80 p-3 rounded-xl border border-slate-800 space-y-1.5">
            <div class="flex items-center justify-between">
              <label class="text-[10px] text-emerald-400 font-bold">
                <i class="fa-solid fa-gears mr-1"></i> Erlaubte Aktionen / Befehlswerte fuer {action}
              </label>
              <span class="text-[9px] text-slate-500 font-mono">Wird als Needle Literal[...] registriert</span>
            </div>
            <span class="text-[9px] text-slate-400 block">
              Trage hier kommagetrennt alle Befehle ein, die du sprechen oder klicken willst. Auch eigene wie <b>rechts, links, hoch, runter, eco</b>!
            </span>
            <input type="text" value="${(dev.actions || []).join(', ')}" 
                   placeholder="on, off, status, toggle, rechts, links, hoch, runter"
                   onchange="appData.devices[${idx}].actions = this.value.split(',').map(s=>s.trim()).filter(Boolean)" 
                   class="w-full bg-slate-950 border border-slate-700 px-2.5 py-1.5 rounded-lg font-mono text-xs text-emerald-300">
          </div>

          <!-- Zeile 4: Shell Command Template -->
          <div>
            <label class="text-[10px] text-slate-400 font-bold block mb-0.5">
              <i class="fa-solid fa-terminal mr-1"></i> Terminal-Befehl (Shell Command)
            </label>
            <span class="text-[9px] text-slate-500 block mb-1">Wird im Hintergrund ausgefuehrt. <code>{action}</code> wird durch die gesprochene Aktion (on, off, links etc.) ersetzt.</span>
            <input type="text" value="${dev.command || ''}" onchange="appData.devices[${idx}].command = this.value" 
                   class="w-full bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded-lg font-mono text-xs text-amber-300">
          </div>
        `;
        list.appendChild(item);
      });

      document.getElementById('deviceModal').classList.remove('hidden');
    }

    function deleteDeviceConfirm(idx) {
      const dev = appData.devices[idx];
      if (confirm(`Moechtest du "${dev.name || dev.id}" wirklich unwiderruflich loeschen?`)) {
        appData.devices.splice(idx, 1);
        appData.layout.placements = (appData.layout.placements || []).filter(p => p.device_id !== dev.id);
        delete appData.states[dev.id];
        openDeviceEditor();
        renderCanvas();
        renderPalette();
      }
    }

    function addNewDevice() {
      const id = "device_" + (appData.devices.length + 1);
      appData.devices.push({
        id: id,
        name: "Neues Geraet",
        type: "light",
        icon: "fa-lightbulb",
        aliases: [id],
        actions: ["on", "off", "status"],
        command: `python3 tool_scripte/steckdose.py --device ${id} --action {action}`
      });
      openDeviceEditor();
      renderPalette();
    }

    async function saveDevices() {
      const res = await fetch('/api/devices/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(appData.devices)
      });
      const data = await res.json();
      if (data.states) appData.states = data.states;
      if (data.layout) appData.layout = data.layout;

      closeDeviceEditor();
      renderCanvas();
      renderPalette();
      logChat("System", "Geraete und Befehlswerte erfolgreich gespeichert.");
    }

    function closeDeviceEditor() {
      document.getElementById('deviceModal').classList.add('hidden');
    }

    window.onload = loadState;
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)