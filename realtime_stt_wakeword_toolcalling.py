#!/usr/bin/env python3
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import Literal
import needle
import sounddevice as sd
from vosk import KaldiRecognizer, Model

WAKEWORDS = ["computer", "hey computer", "jarvis"]
WAKE_TIMEOUT_SECONDS = 6.0
DEVICES_FILE = "devices.json"
STATE_FILE = "device_states.json"
q = queue.Queue()

_executed_in_turn_lock = threading.Lock()
_executed_devices_in_turn = set()

DEFAULT_DEVICES = [
    {
        "id": "bedroom_light",
        "type": "light",
        "aliases": ["bedroom light", "bedroom", "schlafzimmer", "bett", "bettzimmer", "schlafzimmerlicht"]
    },
    {
        "id": "livingroom_light",
        "type": "light",
        "aliases": ["livingroom light", "living room light", "livingroom", "living room", "wohnzimmer", "wohnzimmerlicht"]
    },
    {
        "id": "fan",
        "type": "fan",
        "aliases": ["fan", "ventilator", "lüfter", "blower"]
    }
]


def load_devices_config(filepath=DEVICES_FILE):
    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DEVICES, f, indent=2, ensure_ascii=False)
        return DEFAULT_DEVICES

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if data else DEFAULT_DEVICES
    except Exception as e:
        print(f"Fehler beim Laden von {filepath}: {e}")
        return DEFAULT_DEVICES


def get_current_states(filepath=STATE_FILE):
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Fehler beim Laden von {filepath}: {e}")
        return {}


def update_device_state(device_id, action, filepath=STATE_FILE):
    if action not in ["on", "off"]:
        return
    try:
        states = get_current_states(filepath)
        states[device_id] = action
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(states, f, indent=2)
    except Exception as ex:
        print(f"Fehler beim Schreiben von {filepath}: {ex}")


def execute_hardware_command(device_id, action):
    cmd = f"python3 tool_scripte/steckdose.py --device {device_id} --action {action}"
    print(f"\n[OS-Befehl ausführen]: {cmd}")
    try:
        proc = subprocess.run(
            shlex.split(cmd),
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

        if proc.returncode == 0 and action in ["on", "off"]:
            update_device_state(device_id, action)

        return {"exit_code": proc.returncode, "response": data}
    except Exception as ex:
        return {"error": str(ex)}


def resolve_device_id(raw_choice: str, devices: list) -> str:
    choice = raw_choice.lower().strip()
    
    # 1. Direkte Übereinstimmung mit ID
    for d in devices:
        if d["id"].lower() == choice:
            return d["id"]

    # 2. Exakter oder enthaltener Alias-Match
    for d in devices:
        for alias in d.get("aliases", []):
            a = alias.lower()
            if a == choice or a in choice or choice in a:
                return d["id"]

    return ""


def build_tools(devices: list):
    """Erzeugt ein sauberes Tool mit striktem Literal-Enum."""
    dev_ids = [d["id"] for d in devices]
    DeviceEnum = Literal[tuple(dev_ids + ["all_lights"])]
    ActionEnum = Literal["on", "off", "status", "toggle"]

    @needle.tool(
        triggers=[
            r"\b(turn|switch|power|set|mach|schalte|wie|status)\b",
            r"\b(on|off|an|aus|auf|zu|status)\b",
            r"\b(fan|ventilator|light|lights|livingroom|bedroom|wohnzimmer|schlafzimmer)\b",
        ]
    )
    def control_device(device: DeviceEnum, action: ActionEnum):
        """Switch, query or toggle a specific smart home device."""
        target_ids = []
        if device == "all_lights":
            target_ids = [d["id"] for d in devices if d.get("type") == "light"]
        else:
            resolved = resolve_device_id(str(device), devices)
            if not resolved:
                return {"error": f"Unknown device: {device}"}
            target_ids = [resolved]

        current_states = get_current_states()
        results = []

        for dev_id in target_ids:
            with _executed_in_turn_lock:
                if dev_id in _executed_devices_in_turn:
                    return {"skipped": True, "device": dev_id, "reason": "already_executed_in_turn"}
                _executed_devices_in_turn.add(dev_id)

            curr_state = current_states.get(dev_id, "unknown")

            eff_action = action
            if action == "toggle":
                eff_action = "off" if curr_state == "on" else "on"

            if eff_action == "status":
                res = execute_hardware_command(dev_id, "status")
                results.append(res)
                continue

            # Status-Gate: Nur schalten, wenn Zustand abweicht
            if eff_action in ["on", "off"] and curr_state == eff_action:
                print(f"[Status-Gate]: {dev_id} ist bereits '{curr_state}' (übersprungen)")
                results.append({"device": dev_id, "status": curr_state, "skipped": True})
                continue

            res = execute_hardware_command(dev_id, eff_action)
            results.append(res)

        return results

    return [control_device]


def load_blacklist(filepath="blacklist.txt"):
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


def load_replacements(filepath="replacements.txt"):
    replacements = []
    if not os.path.exists(filepath):
        return replacements
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "->" in line:
                source, target = line.split("->", 1)
                replacements.append((source.strip().lower(), target.strip()))
    replacements.sort(key=lambda x: len(x[0]), reverse=True)
    return replacements


def clean_and_correct(text, blacklist, replacements):
    text = text.strip().lower()
    if not text or text in blacklist:
        return ""

    for src, target in replacements:
        pattern = r"\b" + re.escape(src) + r"\b"
        text = re.sub(pattern, target, text)

    words = text.split()
    while words and words[0].lower() in blacklist:
        words.pop(0)
    return " ".join(words).strip()


def audio_callback(indata, frames, time_info, status):
    if status:
        print(f"Audio-Status: {status}", file=sys.stderr)
    q.put(bytes(indata))


def normalize_command(text: str) -> str:
    """Bereinigt Umgangssprache und Raumbezeichnungen für Needle."""
    t = text.lower()
    t = re.sub(r"\bauf\b", "on", t)
    t = re.sub(r"\ban\b", "on", t)
    t = re.sub(r"\baus\b", "off", t)
    t = re.sub(r"\bliving room\b", "livingroom", t)
    t = re.sub(r"\bbett zimmer\b", "bedroom", t)
    t = re.sub(r"\bbett\b", "bedroom", t)
    t = re.sub(r"\balle lichter\b", "all_lights", t)
    return t


def run_needle_command_async(tools, command_text):
    global _executed_devices_in_turn
    with _executed_in_turn_lock:
        _executed_devices_in_turn.clear()

    normalized = normalize_command(command_text)

    # Frische Needle-Instanz pro Sprachbefehl verhindert Context-Pollution & Loops
    agent = needle.Needle(tools=tools)

    print(f"\n[Anfrage an Needle]: '{normalized}'")
    result = agent.run(normalized)
    print(f"\n[Ausgabe von steckdose.py]: {result.get('results')}")
    if "response" in result:
        print(f"[Antwort]: {result['response']}")


def main():
    blacklist = load_blacklist("blacklist.txt")
    replacements = load_replacements("replacements.txt")
    devices = load_devices_config(DEVICES_FILE)

    print(f"Lade Geräte-Konfiguration ({len(devices)} registriert):")
    for d in devices:
        print(f" -> {d['id']} (Aliase: {', '.join(d.get('aliases', []))})")

    # Tools einmalig definieren
    tools = build_tools(devices)

    try:
        device_info = sd.query_devices(kind="input")
        samplerate = int(device_info["default_samplerate"])
    except Exception:
        samplerate = 44100

    print("\nLade Vosk-Sprachmodell...")
    model = Model("model")
    rec = KaldiRecognizer(model, samplerate)
    blocksize = int(samplerate * 0.2)

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
            print("\nBereit. Sag z. B.: 'Computer livingroom light on' oder 'Computer fan off'\n")

            while True:
                if (
                    waiting_for_command
                    and (time.time() - wake_time) > WAKE_TIMEOUT_SECONDS
                ):
                    print("\n[Timeout]: Kein Befehl empfangen.")
                    waiting_for_command = False

                data = q.get()
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    raw_text = result.get("text", "").strip()
                    cleaned_text = clean_and_correct(
                        raw_text, blacklist, replacements
                    )

                    if not cleaned_text:
                        continue

                    print(f"\n[Erkannt]: {cleaned_text}")

                    command_to_run = None

                    if waiting_for_command:
                        command_to_run = cleaned_text
                        waiting_for_command = False
                    else:
                        for ww in WAKEWORDS:
                            pattern = r"\b" + re.escape(ww) + r"\b"
                            match = re.search(pattern, cleaned_text)
                            if match:
                                command_part = cleaned_text[
                                    match.end() :
                                ].strip()
                                if command_part:
                                    command_to_run = command_part
                                else:
                                    print("[Wakeword]: Höre zu...")
                                    waiting_for_command = True
                                    wake_time = time.time()
                                break

                    if command_to_run:
                        threading.Thread(
                            target=run_needle_command_async,
                            args=(tools, command_to_run),
                            daemon=True,
                        ).start()

                else:
                    partial = json.loads(rec.PartialResult())
                    raw_partial = partial.get("partial", "").strip()
                    cleaned_partial = clean_and_correct(
                        raw_partial, blacklist, replacements
                    )
                    if cleaned_partial:
                        status_prompt = (
                            "[Zuhören...]"
                            if waiting_for_command
                            else "[Standby]"
                        )
                        print(
                            f"\r{status_prompt} {cleaned_partial}",
                            end="",
                            flush=True,
                        )

    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()