import json
import os
import re
from nicegui import ui

JSON_PATH = "tools.json"

DEFAULT_TOOLS = [
    {
        "name": "living_room_light",
        "description": "Turn the living room light on or off, or get its status.",
        "triggers": ["\\b(light|lamp|lights)\\b"],
        "command": "python3 tool_scripte/steckdose.py --device light --action {action}",
        "parameters": {
            "action": {
                "type": "Literal",
                "enum": ["on", "off", "status"],
                "description": "Switch action: 'on', 'off', or 'status'."
            }
        }
    },
    {
        "name": "fan",
        "description": "Turn the room fan on or off, or get its status.",
        "triggers": ["\\b(fan|blower|ventilator)\\b"],
        "command": "python3 tool_scripte/steckdose.py --device fan --action {action}",
        "parameters": {
            "action": {
                "type": "Literal",
                "enum": ["on", "off", "status"],
                "description": "Switch action: 'on', 'off', or 'status'."
            }
        }
    }
]

# --- Hilfsfunktionen für intuitive Trigger-Eingabe ---

def parse_regex_to_words(regex_pattern: str) -> str:
    """Wandelt \\b(word1|word2)\\b zurück in eine lesbare Liste 'word1, word2' um."""
    if not regex_pattern:
        return ""
    # Entfernt Wortgrenzen und Klammern
    clean = re.sub(r"^\\b\(?|\)?\\b$", "", regex_pattern.strip())
    words = [w.strip() for w in clean.split("|") if w.strip()]
    return ", ".join(words)

def build_regex_from_words(words_input: str) -> list[str]:
    """Baut aus 'licht, lampe' -> ['\\b(licht|lampe)\\b']."""
    raw_words = [w.strip() for w in words_input.split(",") if w.strip()]
    if not raw_words:
        return []
    escaped = [re.escape(w) for w in raw_words]
    pattern = f"\\b({'|'.join(escaped)})\\b"
    return [pattern]

# --- Datenhaltung & Persistenz ---

def load_data():
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return DEFAULT_TOOLS
    return DEFAULT_TOOLS

state = {
    "tools": load_data(),
    "current_index": 0
}
if not state["tools"]:
    state["current_index"] = None

def save_data():
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(state["tools"], f, indent=2, ensure_ascii=False)
    ui.notify("tools.json erfolgreich gespeichert!", type="positive")

# --- UI Setup ---
ui.colors(primary="#2E7D32", secondary="#81C784", accent="#FFB300")

with ui.header().classes("items-center justify-between bg-slate-800 text-white px-6 py-3"):
    ui.label("🌵 Cactus Needle 3 — Tool Configurator").classes("text-xl font-bold tracking-wide")
    ui.button("💾 Speichern", on_click=save_data).props("elevated color=positive icon=save")

with ui.row().classes("w-full p-4 gap-6 no-wrap items-start"):
    
    # --- Linke Spalte: Tool-Liste ---
    with ui.card().classes("w-1/3 p-4 shadow-sm"):
        ui.label("Verfügbare Tools").classes("text-lg font-semibold mb-2")
        tool_list_container = ui.column().classes("w-full gap-2")

        def select_tool(idx):
            state["current_index"] = idx
            refresh_editor()
            refresh_list()

        def add_tool():
            new_tool = {
                "name": f"new_device_{len(state['tools'])+1}",
                "description": "Device control description.",
                "triggers": ["\\b(device)\\b"],
                "command": "python3 script.py --action {action}",
                "parameters": {
                    "action": {
                        "type": "Literal",
                        "enum": ["on", "off"],
                        "description": "Device state."
                    }
                }
            }
            state["tools"].append(new_tool)
            state["current_index"] = len(state["tools"]) - 1
            refresh_list()
            refresh_editor()

        def duplicate_tool(idx):
            dup = json.loads(json.dumps(state["tools"][idx]))
            dup["name"] += "_copy"
            state["tools"].append(dup)
            state["current_index"] = len(state["tools"]) - 1
            refresh_list()
            refresh_editor()

        def delete_tool(idx):
            if 0 <= idx < len(state["tools"]):
                state["tools"].pop(idx)
                state["current_index"] = max(0, len(state["tools"]) - 1) if state["tools"] else None
                refresh_list()
                refresh_editor()

        def refresh_list():
            tool_list_container.clear()
            with tool_list_container:
                for i, t in enumerate(state["tools"]):
                    is_active = (i == state["current_index"])
                    bg_color = "bg-slate-100 border-l-4 border-green-600" if is_active else "bg-white hover:bg-slate-50 border"
                    with ui.row().classes(f"w-full p-2 rounded items-center justify-between {bg_color} cursor-pointer"):
                        with ui.row().classes("items-center gap-2 flex-grow").on("click", lambda _, idx=i: select_tool(idx)):
                            ui.icon("construction", size="xs").classes("text-slate-500")
                            ui.label(t.get("name", "unnamed")).classes("font-mono text-sm font-medium")
                        with ui.row().classes("gap-1 items-center"):
                            ui.button(icon="content_copy", on_click=lambda _, idx=i: duplicate_tool(idx)).props("flat dense round size=sm").tooltip("Duplizieren")
                            ui.button(icon="delete", color="negative", on_click=lambda _, idx=i: delete_tool(idx)).props("flat dense round size=sm").tooltip("Löschen")
                
                ui.button("+ Neues Tool hinzufügen", on_click=add_tool).classes("w-full mt-3").props("outline color=primary icon=add")

        refresh_list()

    # --- Rechte Spalte: Detail-Editor ---
    with ui.card().classes("w-2/3 p-5 shadow-sm"):
        editor_container = ui.column().classes("w-full gap-4")

        def refresh_editor():
            editor_container.clear()
            idx = state["current_index"]
            if idx is None or not (0 <= idx < len(state["tools"])):
                with editor_container:
                    ui.label("Kein Tool ausgewählt oder Liste ist leer.").classes("text-slate-400 italic")
                return

            tool = state["tools"][idx]

            with editor_container:
                ui.label(f"Tool bearbeiten: {tool.get('name')}").classes("text-xl font-bold text-slate-700")

                # Name
                ui.input(
                    label="Tool Name (eindeutiger Bezeichner)",
                    value=tool.get("name", ""),
                    on_change=lambda e: (tool.update({"name": e.value}), refresh_list(), update_json_view())
                ).classes("w-full").props("outlined dense")

                # Beschreibung
                ui.textarea(
                    label="Beschreibung (wird vom Modell zur Absichts-Erkennung genutzt)",
                    value=tool.get("description", ""),
                    on_change=lambda e: (tool.update({"description": e.value}), update_json_view())
                ).classes("w-full").props("outlined dense autogrow")

                # --- Schlagwörter / Trigger Box ---
                with ui.card().classes("w-full bg-slate-50 border p-3 gap-1"):
                    with ui.row().classes("items-center justify-between w-full"):
                        ui.label("Erkennungswörter / Schlagwörter").classes("text-sm font-bold text-slate-700")
                        ui.badge("Auto-Regex", color="green").props("outline")

                    ui.label("Auf welche Wörter soll dieses Tool anspringen? Einfach kommagetrennt eingeben:").classes("text-xs text-slate-500 mb-1")

                    first_pattern = tool.get("triggers", [""])[0] if tool.get("triggers") else ""
                    readable_words = parse_regex_to_words(first_pattern)
                    regex_preview = ui.label().classes("text-xs font-mono text-slate-500")

                    def on_triggers_changed(val: str):
                        new_triggers = build_regex_from_words(val)
                        tool["triggers"] = new_triggers
                        regex_preview.set_text(f"Schema-Regex: {new_triggers[0] if new_triggers else '—'}")
                        update_json_view()

                    ui.input(
                        placeholder="z. B. licht, lampe, leuchte, deckenlicht",
                        value=readable_words,
                        on_change=lambda e: on_triggers_changed(e.value)
                    ).classes("w-full bg-white").props("outlined dense clearable")

                    regex_preview.set_text(f"Schema-Regex: {first_pattern if first_pattern else '—'}")

                # Shell Command
                ui.input(
                    label="Shell Command / Skript Template",
                    value=tool.get("command", ""),
                    on_change=lambda e: (tool.update({"command": e.value}), update_json_view())
                ).classes("w-full font-mono").props("outlined dense")

                # Parameters (Action Enums)
                with ui.card().classes("w-full bg-slate-50 border p-3 gap-2"):
                    ui.label("Parameter: 'action'").classes("text-sm font-bold text-slate-700")
                    
                    action_param = tool.setdefault("parameters", {}).setdefault("action", {
                        "type": "Literal",
                        "enum": ["on", "off"],
                        "description": "Switch action."
                    })

                    enum_str = ", ".join(action_param.get("enum", []))
                    ui.input(
                        label="Erlaubte Werte / Aktionen (kommagetrennt)",
                        value=enum_str,
                        on_change=lambda e: (action_param.update({"enum": [x.strip() for x in e.value.split(",") if x.strip()]}), update_json_view())
                    ).classes("w-full bg-white").props("outlined dense").tooltip("z. B. on, off, status, toggle")

                    ui.input(
                        label="Parameter Beschreibung",
                        value=action_param.get("description", ""),
                        on_change=lambda e: (action_param.update({"description": e.value}), update_json_view())
                    ).classes("w-full bg-white").props("outlined dense")

                # Live JSON Output
                ui.label("Live Schema Vorschau (Cactus Needle 3 kompatibel):").classes("text-xs font-semibold text-slate-500 uppercase tracking-wider mt-2")
                code_view = ui.code("", language="json").classes("w-full font-mono text-xs rounded border bg-slate-900 text-slate-100 p-2")

                def update_json_view():
                    code_view.set_content(json.dumps(tool, indent=2, ensure_ascii=False))

                update_json_view()

        refresh_editor()

ui.run(title="Needle 3 Tool Editor", host="0.0.0.0", port=8080, reload=False)