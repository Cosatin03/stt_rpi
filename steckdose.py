#!/usr/bin/env python3
import argparse
import json
import os
import sys

STATE_FILE = "device_states.json"


def load_states():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_states(states):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(states, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Steckdosen- und Gerätesteuerung")
    parser.add_argument(
        "--device",
        type=str,
        required=True,
        help="Gerätename (z. B. light, fan)",
    )
    parser.add_argument(
        "--action",
        type=str,
        required=True,
        choices=["on", "off", "status"],
        help="Aktion: on, off, status",
    )

    args = parser.parse_args()
    device = args.device.lower()
    action = args.action.lower()

    states = load_states()
    current_state = states.get(device, "off")

    if action == "status":
        result = {
            "success": True,
            "device": device,
            "state": current_state,
            "message": f"{device.capitalize()} is currently {current_state}.",
        }
    elif action in ["on", "off"]:
        states[device] = action
        save_states(states)
        result = {
            "success": True,
            "device": device,
            "new_state": action,
            "message": f"{device.capitalize()} set to '{action}'.",
        }
    else:
        result = {
            "success": False,
            "device": device,
            "error": f"Invalid action: {action}",
        }

    print(json.dumps(result))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()