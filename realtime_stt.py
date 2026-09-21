import json
import os
import queue
import re
import sys
import sounddevice as sd
from vosk import KaldiRecognizer, Model

q = queue.Queue()


def load_blacklist(filepath="blacklist.txt"):
    """Lädt einzelne Wörter, die ignoriert oder am Anfang entfernt werden sollen."""
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


def load_replacements(filepath="replacements.txt"):
    """Lädt Ersetzungen. Format: falscher text -> richtiger text"""
    replacements = []
    if not os.path.exists(filepath):
        return replacements

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "->" in line:
                source, target = line.split("->", 1)
                replacements.append((source.strip().lower(), target.strip()))

    # Längere Phrasen zuerst ersetzen, um Teilüberschreibungen zu verhindern
    replacements.sort(key=lambda x: len(x[0]), reverse=True)
    return replacements


def clean_and_correct(text, blacklist, replacements):
    text = text.strip().lower()
    if not text:
        return ""

    # Regel 1: Wenn der gesamte Text nur aus einem Blacklist-Wort besteht -> verwerfen
    if text in blacklist:
        return ""

    # Regel 2: Phrasen-Ersetzungen anwenden (z.B. 'licht auf' -> 'licht aus')
    for src, target in replacements:
        pattern = r"\b" + re.escape(src) + r"\b"
        text = re.sub(pattern, target, text)

    # Regel 3: Fälschlich erkannte Geisterwörter am Satzanfang entfernen
    words = text.split()
    while words and words[0].lower() in blacklist:
        words.pop(0)

    return " ".join(words).strip()


def audio_callback(indata, frames, time, status):
    if status:
        print(f"Audio-Status: {status}", file=sys.stderr)
    q.put(bytes(indata))


def main():
    blacklist = load_blacklist("blacklist.txt")
    replacements = load_replacements("replacements.txt")

    try:
        device_info = sd.query_devices(kind="input")
        samplerate = int(device_info["default_samplerate"])
        print(f"Eingabegerät: {device_info['name']}")
        print(f"Ermittelte Samplerate: {samplerate} Hz")
    except Exception as e:
        print(f"Fehler beim Abfragen des Mikrofons: {e}")
        samplerate = 44100
        print(f"Verwende Fallback-Samplerate: {samplerate} Hz")

    print("Lade Vosk-Modell...")
    model = Model("model")
    rec = KaldiRecognizer(model, samplerate)

    blocksize = int(samplerate * 0.2)

    try:
        with sd.RawInputStream(
            samplerate=samplerate,
            blocksize=blocksize,
            dtype="int16",
            channels=1,
            callback=audio_callback,
        ):
            print(
                "\nMikrofon aktiv. Sprechen Sie jetzt auf Deutsch... (Beenden mit Strg+C)\n"
            )

            while True:
                data = q.get()
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    raw_text = result.get("text", "").strip()

                    cleaned_text = clean_and_correct(
                        raw_text, blacklist, replacements
                    )
                    if cleaned_text:
                        print(f"\n[Endgültig]: {cleaned_text}")
                else:
                    partial = json.loads(rec.PartialResult())
                    raw_partial = partial.get("partial", "").strip()

                    cleaned_partial = clean_and_correct(
                        raw_partial, blacklist, replacements
                    )
                    if cleaned_partial:
                        print(
                            f"\r[Live]: {cleaned_partial}", end="", flush=True
                        )

    except KeyboardInterrupt:
        print("\n\nTranskription beendet.")
    except Exception as e:
        print(f"\nFehler beim Ausführen des Streams: {e}")


if __name__ == "__main__":
    main()