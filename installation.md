# 0. Unter Debian/Ubuntu ggf. zuerst das System-Paket installieren (falls noch nicht vorhanden):
# sudo apt update && sudo apt install -y python3-venv libportaudio2
# erstelle ein ordner stt/ und enpacke das projekt dort.
cd stt/
# 1. Zip-Datei herunterladen (ca. 45 MB)
curl -LO https://alphacephei.com/vosk/models/vosk-model-small-de-0.15.zip

# 2. Entpacken
unzip vosk-model-small-de-0.15.zip

# 3. Den Ordner in "model" umbenennen (passend zu Model("model") im Skript)
mv vosk-model-small-de-0.15 model

# 4. Archiv aufräumen
rm vosk-model-small-de-0.15.zip


# 1. Virtuelle Umgebung im Ordner ".venv" erstellen
python3 -m venv venv

# 2. Virtuelle Umgebung aktivieren
source venv/bin/activate

# 3. Pip aktualisieren
pip install --upgrade pip

# 4. Bibliotheken installieren
pip install -r requirements.txt
