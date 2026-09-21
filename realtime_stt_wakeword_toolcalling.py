#!/usr/bin/env python3
import inspect
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
q = queue.Queue()


def build_needle_tools_from_json(json_path="tools.json"):
    """Erzeugt dynamisch Needle-3-Tools mit Triggers und Literal-Grammar."""
    if not os.path.exists(json_path):
        print(f"Fehler: {json_path} nicht gefunden.")
        return []

    with open(json_path, "r", encoding="utf-8") as f:
        tools_def = json.load(f)

    needle_tools = []

    for tool_cfg in tools_def:
        tool_name = tool_cfg["name"]
        tool_desc = tool_cfg.get("description", "")
        cmd_template = tool_cfg["command"]
        triggers = tool_cfg.get("triggers", [])
        params_cfg = tool_cfg.get("parameters", {})

        def create_device_tool(cmd, desc, name, p_cfg, trigs):
            def device_func(**kwargs):
                try:
                    formatted_cmd = cmd.format(**kwargs)
                except KeyError as e:
                    return {"error": f"Fehlender Parameter: {e}"}

                print(f"\n[OS-Befehl ausführen]: {formatted_cmd}")

                try:
                    args = shlex.split(formatted_cmd)
                    proc = subprocess.run(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                    )

                    stdout = proc.stdout.strip()
                    stderr = proc.stderr.strip()

                    try:
                        data = json.loads(stdout)
                    except Exception:
                        data = stdout if stdout else stderr

                    return {"exit_code": proc.returncode, "response": data}
                except Exception as ex:
                    return {"error": str(ex)}

            device_func.__name__ = name
            device_func.__doc__ = desc

            sig_params = []
            for p_name, p_meta in p_cfg.items():
                if "enum" in p_meta:
                    annotation = Literal[tuple(p_meta["enum"])]
                else:
                    annotation = eval(p_meta.get("type", "str"))

                sig_params.append(
                    inspect.Parameter(
                        p_name,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation=annotation,
                    )
                )

            device_func.__signature__ = inspect.Signature(sig_params)

            if trigs:
                return needle.tool(triggers=trigs)(device_func)
            return needle.tool(device_func)

        dynamic_tool = create_device_tool(
            cmd_template, tool_desc, tool_name, params_cfg, triggers
        )
        needle_tools.append(dynamic_tool)

    return needle_tools


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


def run_needle_command_async(agent, command_text):
    """Verhindert input overflow während Needle und Subprocess arbeiten."""
    print(f"\n[Anfrage an Needle]: '{command_text}'")
    result = agent.run(command_text)
    print(f"\n[Ausgabe von steckdose.py]: {result.get('results')}")
    if "response" in result:
        print(f"[Antwort]: {result['response']}")


def main():
    blacklist = load_blacklist("blacklist.txt")
    replacements = load_replacements("replacements.txt")

    print("Registriere Geräte aus tools.json...")
    tools = build_needle_tools_from_json("tools.json")
    for t in tools:
        print(f" -> Tool geladen: {t.__name__}")

    agent = needle.Needle(tools=tools)

    try:
        device_info = sd.query_devices(kind="input")
        samplerate = int(device_info["default_samplerate"])
    except Exception:
        samplerate = 44100

    print("Lade Sprachmodell...")
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
            print("\nBereit. Sag z. B.: 'Computer light on'\n")

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
                            args=(agent, command_to_run),
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