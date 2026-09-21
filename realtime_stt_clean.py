import json
import queue
import sys
import sounddevice as sd
from vosk import Model, KaldiRecognizer

# Warteschlange für eingehende Audio-Chunks
q = queue.Queue()

def audio_callback(indata, frames, time, status):
    if status:
        print(f"Audio-Status: {status}", file=sys.stderr)
    q.put(bytes(indata))

def main():
    # 1. Standard-Eingabegerät und native Samplerate ermitteln
    try:
        device_info = sd.query_devices(kind='input')
        samplerate = int(device_info['default_samplerate'])
        print(f"Eingabegerät: {device_info['name']}")
        print(f"Ermittelte Samplerate: {samplerate} Hz")
    except Exception as e:
        print(f"Fehler beim Abfragen des Mikrofons: {e}")
        # Fallback auf 44100 Hz, falls die automatische Erkennung fehlschlägt
        samplerate = 44100
        print(f"Verwende Fallback-Samplerate: {samplerate} Hz")

    # 2. Modell und KaldiRecognizer laden
    print("Lade Vosk-Modell...")
    model = Model("model")
    rec = KaldiRecognizer(model, samplerate)

    # 3. Audio-Stream starten (Puffergröße: ca. 200 ms Audio)
    blocksize = int(samplerate * 0.2)

    try:
        with sd.RawInputStream(
            samplerate=samplerate,
            blocksize=blocksize,
            dtype='int16',
            channels=1,
            callback=audio_callback
        ):
            print("\nMikrofon aktiv. Sprechen Sie jetzt auf Deutsch... (Beenden mit Strg+C)\n")

            while True:
                data = q.get()
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    text = result.get("text", "").strip()
                    if text:
                        print(f"\n[Endgültig]: {text}")
                else:
                    partial = json.loads(rec.PartialResult())
                    partial_text = partial.get("partial", "").strip()
                    if partial_text:
                        print(f"\r[Live]: {partial_text}", end="", flush=True)

    except KeyboardInterrupt:
        print("\n\nTranskription beendet.")
    except Exception as e:
        print(f"\nFehler beim Ausführen des Streams: {e}")

if __name__ == "__main__":
    main()