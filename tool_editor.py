import json
import os
from nicegui import ui

DEVICES_PATH = "devices.json"
STATES_PATH = "device_states.json"

DEFAULT_DEVICES = [
    {
        "id": "livingroom_light",
        "type": "light",
        "aliases": ["livingroom light", "living room light", "wohnzimmer", "wohnzimmer licht"]
    },
    {
        "id": "bedroom_light",
        "type": "light",
        "aliases": ["bedroom light", "schlafzimmer", "bett zimmer", "schlafzimmer licht"]
    },
    {
        "id": "fan",
        "type": "fan",
        "aliases": ["fan", "ventilator", "lüfter"]
    }
]


def load_data():
    if os.path.exists(DEVICES_PATH):
        try:
            with open(DEVICES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return DEFAULT_DEVICES
    return DEFAULT_DEVICES


devices = load_data()
state = {"current_index": 0 if devices else None}


def save_data():
    with open(DEVICES_PATH, "w", encoding="utf-8") as f:
        json.dump(devices, f, indent=2, ensure_ascii=False)

    # Sync device_states.json
    current_states = {}
    if os.path.exists(STATES_PATH):
        try:
            with open(STATES_PATH, "r", encoding="utf-8") as f:
                current_states = json.load(f)
        except Exception:
            pass

    for d in devices:
        dev_id = d.get("id")
        if dev_id and dev_id not in current_states:
            current_states[dev_id] = "off"

    with open(STATES_PATH, "w", encoding="utf-8") as f:
        json.dump(current_states, f, indent=2)

    ui.notify("Geräte und Status erfolgreich gespeichert!", type="positive")


ui.colors(primary="#1976D2")

with ui.header().classes("items-center justify-between bg-slate-900 text-white px-6 py-3 shadow"):
    ui.label("Needle 3 — Smart Home Device Manager").classes("text-xl font-bold")
    ui.button("💾 Speichern", on_click=save_data).props("elevated color=positive icon=save")

with ui.row().classes("w-full p-4 gap-6 no-wrap items-start"):

    # Linke Spalte
    with ui.card().classes("w-1/3 p-4 shadow-sm"):
        ui.label("Geräte").classes("text-lg font-semibold mb-2")
        list_box = ui.column().classes("w-full gap-2")

        def select(idx):
            state["current_index"] = idx
            refresh_editor()
            refresh_list()

        def add():
            new_id = f"device_{len(devices)+1}"
            devices.append({"id": new_id, "type": "light", "aliases": [new_id]})
            state["current_index"] = len(devices) - 1
            refresh_list()
            refresh_editor()

        def delete(idx):
            if 0 <= idx < len(devices):
                devices.pop(idx)
                state["current_index"] = max(0, len(devices) - 1) if devices else None
                refresh_list()
                refresh_editor()

        def refresh_list():
            list_box.clear()
            with list_box:
                for i, d in enumerate(devices):
                    active = i == state["current_index"]
                    bg = "bg-blue-50 border-l-4 border-blue-600" if active else "bg-white border"
                    with ui.row().classes(f"w-full p-2.5 rounded items-center justify-between {bg} cursor-pointer"):
                        ui.label(d.get("id", "unnamed")).classes("font-mono text-sm font-semibold").on(
                            "click", lambda _, idx=i: select(idx)
                        )
                        ui.button(icon="delete", color="negative", on_click=lambda _, idx=i: delete(idx)).props(
                            "flat dense round size=sm"
                        )
                ui.button("+ Neues Gerät", on_click=add).classes("w-full mt-3").props("outline icon=add")

        refresh_list()

    # Rechte Spalte
    with ui.card().classes("w-2/3 p-5 shadow-sm"):
        editor_box = ui.column().classes("w-full gap-4")

        def refresh_editor():
            editor_box.clear()
            idx = state["current_index"]
            if idx is None or not (0 <= idx < len(devices)):
                with editor_box:
                    ui.label("Kein Gerät ausgewählt.").classes("text-slate-400 italic")
                return

            dev = devices[idx]

            with editor_box:
                ui.label(f"Gerät konfigurieren: {dev.get('id')}").classes("text-xl font-bold")

                ui.input(
                    label="Geräte-ID (--device Parameter)",
                    value=dev.get("id", ""),
                    on_change=lambda e: (dev.update({"id": e.value}), refresh_list(), update_preview()),
                ).classes("w-full").props("outlined dense")

                ui.select(
                    label="Gerätetyp",
                    options=["light", "fan", "switch", "outlet"],
                    value=dev.get("type", "light"),
                    on_change=lambda e: (dev.update({"type": e.value}), update_preview()),
                ).classes("w-full").props("outlined dense")

                aliases_str = ", ".join(dev.get("aliases", []))
                ui.input(
                    label="Aliase & Erkennungswörter (kommagetrennt)",
                    placeholder="z. B. livingroom light, wohnzimmer, salon",
                    value=aliases_str,
                    on_change=lambda e: (
                        dev.update({"aliases": [w.strip() for w in e.value.split(",") if w.strip()]}),
                        update_preview(),
                    ),
                ).classes("w-full").props("outlined dense")

                ui.label("Vorschau in devices.json:").classes("text-xs font-semibold text-slate-500 uppercase mt-2")
                code_view = ui.code("", language="json").classes(
                    "w-full font-mono text-xs rounded border bg-slate-900 text-slate-100 p-2"
                )

                def update_preview():
                    code_view.set_content(json.dumps(dev, indent=2, ensure_ascii=False))

                update_preview()

        refresh_editor()

ui.run(title="Needle 3 Device Manager", host="0.0.0.0", port=8080, reload=False)