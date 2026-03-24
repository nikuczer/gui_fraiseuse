"""
HX-K12 Macro Pad - Upload config depuis hx_k12_keymap.yaml
Protocole CH57x (k8890) via hidapi.
Lance EN ADMIN.

Usage:
    python hx_k12_hid_upload.py                     # utilise hx_k12_keymap.yaml
    python hx_k12_hid_upload.py ma_config.yaml      # fichier custom
"""
import hid
import sys
import time
import os

try:
    import yaml
except ImportError:
    print("ERREUR: pip install pyyaml")
    sys.exit(1)

VID = 0x1189
PID = 0x8890

# USB HID keycodes (WellKnownCode enum du firmware CH57x)
KEYBOARD_CODES = {
    "a": 0x04, "b": 0x05, "c": 0x06, "d": 0x07, "e": 0x08, "f": 0x09,
    "g": 0x0A, "h": 0x0B, "i": 0x0C, "j": 0x0D, "k": 0x0E, "l": 0x0F,
    "m": 0x10, "n": 0x11, "o": 0x12, "p": 0x13, "q": 0x14, "r": 0x15,
    "s": 0x16, "t": 0x17, "u": 0x18, "v": 0x19, "w": 0x1A, "x": 0x1B,
    "y": 0x1C, "z": 0x1D,
    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22,
    "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "enter": 0x28, "escape": 0x29, "backspace": 0x2A, "tab": 0x2B,
    "space": 0x2C, "minus": 0x2D, "equal": 0x2E,
    "leftbracket": 0x2F, "rightbracket": 0x30, "backslash": 0x31,
    "semicolon": 0x33, "quote": 0x34, "grave": 0x35,
    "comma": 0x36, "dot": 0x37, "slash": 0x38, "capslock": 0x39,
    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E, "f6": 0x3F,
    "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43, "f11": 0x44, "f12": 0x45,
    "printscreen": 0x46, "insert": 0x49, "home": 0x4A, "pageup": 0x4B,
    "delete": 0x4C, "end": 0x4D, "pagedown": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
    "f13": 0x68, "f14": 0x69, "f15": 0x6A, "f16": 0x6B,
    "f17": 0x6C, "f18": 0x6D, "f19": 0x6E, "f20": 0x6F,
    "f21": 0x70, "f22": 0x71, "f23": 0x72, "f24": 0x73,
}

MODIFIER_CODES = {
    "ctrl": 0x01, "shift": 0x02, "alt": 0x04, "win": 0x08,
    "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rwin": 0x80,
    "opt": 0x04, "cmd": 0x08,
}

MEDIA_CODES = {
    "next": 0xB5, "previous": 0xB6, "prev": 0xB6, "stop": 0xB7,
    "play": 0xCD, "mute": 0xE2, "volumeup": 0xE9, "volumedown": 0xEA,
    "favorites": 0x182, "calculator": 0x192, "screenlock": 0x19E,
}

MOUSE_ACTIONS = {"click", "wheelup", "wheeldown", "wheel"}


# ── Protocole CH57x k8890 ──

def send(h, *data):
    pkt = list(data) + [0] * (64 - len(data))
    n = h.write(pkt)
    if n < 0:
        raise Exception(f"write error: {n}")
    time.sleep(0.01)


def parse_key(key_str):
    """
    Parse une chaîne comme 'ctrl-shift-a', 'volumeup', 'click', 'F13'.
    Retourne (type, modifier, keycode/mediacode).
    """
    key_str = key_str.strip().lower()

    # Media?
    if key_str in MEDIA_CODES:
        return ("media", 0, MEDIA_CODES[key_str])

    # Mouse?
    if key_str == "click":
        return ("mouse_click", 0, 1)  # left click
    if key_str == "wheelup":
        return ("mouse_wheel", 0, -1)
    if key_str == "wheeldown":
        return ("mouse_wheel", 0, 1)

    # Keyboard (possiblement avec modifiers: ctrl-shift-a)
    parts = key_str.split("-")
    modifier = 0
    keycode = 0
    for part in parts:
        if part in MODIFIER_CODES:
            modifier |= MODIFIER_CODES[part]
        elif part in KEYBOARD_CODES:
            keycode = KEYBOARD_CODES[part]
        else:
            print(f"  WARNING: touche inconnue '{part}'")

    return ("keyboard", modifier, keycode)


def bind_key(h, layer, key_id, key_str):
    kind, modifier, code = parse_key(key_str)

    if kind == "keyboard":
        layer_kind = ((layer + 1) << 4) | 0x01
        send(h, 0x03, 0xFE, layer + 1, 0x01, 0x01, 0, 0, 0, 0)
        send(h, 0x03, key_id, layer_kind, 1, 0, 0, 0, 0, 0)
        send(h, 0x03, key_id, layer_kind, 1, 1, modifier, code, 0, 0)
        send(h, 0x03, 0xAA, 0xAA, 0, 0, 0, 0, 0, 0)

    elif kind == "media":
        layer_kind = ((layer + 1) << 4) | 0x02
        lo = code & 0xFF
        hi = (code >> 8) & 0xFF
        send(h, 0x03, 0xFE, layer + 1, 0x01, 0x01, 0, 0, 0, 0)
        send(h, 0x03, key_id, layer_kind, lo, hi, 0, 0, 0, 0)
        send(h, 0x03, 0xAA, 0xAA, 0, 0, 0, 0, 0, 0)

    elif kind == "mouse_click":
        layer_kind = ((layer + 1) << 4) | 0x03
        send(h, 0x03, 0xFE, layer + 1, 0x01, 0x01, 0, 0, 0, 0)
        send(h, 0x03, key_id, layer_kind, code, 0, 0, 0, 0, 0)
        send(h, 0x03, 0xAA, 0xAA, 0, 0, 0, 0, 0, 0)

    elif kind == "mouse_wheel":
        layer_kind = ((layer + 1) << 4) | 0x03
        send(h, 0x03, 0xFE, layer + 1, 0x01, 0x01, 0, 0, 0, 0)
        send(h, 0x03, key_id, layer_kind, 0, 0, 0, code & 0xFF, 0, 0)
        send(h, 0x03, 0xAA, 0xAA, 0, 0, 0, 0, 0, 0)


def main():
    # Charger le YAML
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "hx_k12_keymap.yaml"
    )

    print("=" * 50)
    print("  HX-K12 - Upload config")
    print(f"  Config: {config_path}")
    print("=" * 50)
    print()

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Ouvrir le device
    devices = [d for d in hid.enumerate() if d["vendor_id"] == VID and d["product_id"] == PID]
    vendor_devs = [d for d in devices if d["usage_page"] == 0xFF00]
    if not vendor_devs:
        print("ERREUR: HX-K12 non trouvé ! Vérifie qu'il est branché.")
        sys.exit(1)

    h = hid.device()
    h.open_path(vendor_devs[0]["path"])
    print("Clavier connecté.\n")

    # Init
    h.write([0x00] + [0x00] * 64)
    time.sleep(0.1)

    errors = 0
    rows = config.get("rows", 3)
    cols = config.get("columns", 4)

    for layer_idx, layer_data in enumerate(config.get("layers", [])):
        print(f"Layer {layer_idx}:")

        # Boutons
        button_idx = 0
        for row in layer_data.get("buttons", []):
            for key_str in row:
                key_id = button_idx + 1
                try:
                    bind_key(h, layer_idx, key_id, key_str)
                    print(f"  Touche {button_idx:>2}: {key_str:<20} OK")
                except Exception as e:
                    errors += 1
                    print(f"  Touche {button_idx:>2}: {key_str:<20} ERREUR: {e}")
                button_idx += 1

        # Knobs
        knobs = layer_data.get("knobs", [])
        for knob_idx, knob in enumerate(knobs):
            base_id = 13 + (knob_idx * 3)  # ccw, press, cw
            for action_idx, action_name in enumerate(["ccw", "press", "cw"]):
                if action_name in knob:
                    key_id = base_id + action_idx
                    key_str = knob[action_name]
                    try:
                        bind_key(h, layer_idx, key_id, key_str)
                        print(f"  Knob {knob_idx} {action_name:>5}: {key_str:<15} OK")
                    except Exception as e:
                        errors += 1
                        print(f"  Knob {knob_idx} {action_name:>5}: {key_str:<15} ERREUR: {e}")

    h.close()

    print()
    if errors == 0:
        print("SUCCES ! Debranche et rebranche le clavier.")
    else:
        print(f"{errors} erreur(s).")


if __name__ == "__main__":
    main()
