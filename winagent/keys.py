"""Key-name normalisation shared by all backends.

LLMs are creative with key names ("Control", "Ctrl", "Return", "ArrowDown",
"Windows", "Super", "Cmd"...).  Everything is normalised to the canonical
names understood by *pyautogui* so that the backends can rely on them.  A
Windows virtual-key table is included for the global stop hot-key.
"""

from __future__ import annotations

import re

# canonical pyautogui names -------------------------------------------------
_ALIASES: dict[str, str] = {
    # modifiers
    "control": "ctrl", "ctl": "ctrl", "ctrlleft": "ctrlleft", "ctrlright": "ctrlright",
    "lctrl": "ctrlleft", "rctrl": "ctrlright", "leftctrl": "ctrlleft", "rightctrl": "ctrlright",
    "alternate": "alt", "option": "alt", "lalt": "altleft", "ralt": "altright", "altgr": "altright",
    "leftalt": "altleft", "rightalt": "altright",
    "lshift": "shiftleft", "rshift": "shiftright", "leftshift": "shiftleft", "rightshift": "shiftright",
    "windows": "win", "window": "win", "super": "win", "meta": "win", "cmd": "win", "command": "win",
    "lwin": "winleft", "rwin": "winright", "winkey": "win", "start": "win",
    # navigation / editing
    "return": "enter", "ret": "enter", "newline": "enter", "\n": "enter", "\r": "enter",
    "escape": "esc", "spacebar": "space", " ": "space",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "uparrow": "up", "downarrow": "down", "leftarrow": "left", "rightarrow": "right",
    "pgup": "pageup", "pgdn": "pagedown", "pagedn": "pagedown", "page_up": "pageup", "page_down": "pagedown",
    "del": "delete", "ins": "insert", "bksp": "backspace", "back": "backspace", "backspace ": "backspace",
    "caps": "capslock", "caps_lock": "capslock", "num_lock": "numlock", "scroll_lock": "scrolllock",
    "printscreen": "printscreen", "prtsc": "printscreen", "prtscr": "printscreen", "print_screen": "printscreen",
    "printscrn": "printscreen", "prntscrn": "printscreen", "snapshot": "printscreen",
    "menu": "apps", "context": "apps", "contextmenu": "apps", "application": "apps", "app": "apps",
    "break": "pause",
    # punctuation names
    "plus": "+", "minus": "-", "equals": "=", "equal": "=", "comma": ",", "period": ".", "dot": ".",
    "slash": "/", "backslash": "\\", "semicolon": ";", "quote": "'", "apostrophe": "'", "backtick": "`",
    "grave": "`", "tilde": "~", "lbracket": "[", "rbracket": "]", "bracketleft": "[", "bracketright": "]",
    "underscore": "_", "asterisk": "*", "star": "*", "hash": "#", "pound": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "at": "@", "exclamation": "!", "question": "?", "pipe": "|", "colon": ":",
    "less": "<", "greater": ">", "lparen": "(", "rparen": ")", "lbrace": "{", "rbrace": "}",
    # numpad
    "numpad0": "num0", "numpad1": "num1", "numpad2": "num2", "numpad3": "num3", "numpad4": "num4",
    "numpad5": "num5", "numpad6": "num6", "numpad7": "num7", "numpad8": "num8", "numpad9": "num9",
    "kp0": "num0", "kp1": "num1", "kp2": "num2", "kp3": "num3", "kp4": "num4", "kp5": "num5",
    "kp6": "num6", "kp7": "num7", "kp8": "num8", "kp9": "num9",
    "numpadenter": "enter", "kpenter": "enter", "numpadadd": "add", "numpadsubtract": "subtract",
    "numpadmultiply": "multiply", "numpaddivide": "divide", "numpaddecimal": "decimal",
    # media
    "mute": "volumemute", "volume_mute": "volumemute", "volume_up": "volumeup", "volume_down": "volumedown",
    "vol+": "volumeup", "vol-": "volumedown", "play": "playpause", "play_pause": "playpause",
    "next": "nexttrack", "previous": "prevtrack", "prev": "prevtrack",
}

# names that pyautogui accepts verbatim (subset of pyautogui.KEY_NAMES)
_CANONICAL = {
    "tab", "enter", "space", "backspace", "delete", "insert", "home", "end", "pageup", "pagedown",
    "up", "down", "left", "right", "esc", "capslock", "numlock", "scrolllock", "printscreen", "pause",
    "ctrl", "ctrlleft", "ctrlright", "alt", "altleft", "altright", "shift", "shiftleft", "shiftright",
    "win", "winleft", "winright", "apps", "sleep",
    "volumemute", "volumeup", "volumedown", "playpause", "nexttrack", "prevtrack", "stop",
    "browserback", "browserforward", "browserrefresh", "browserhome", "browsersearch", "browserfavorites",
    "browserstop", "launchmail", "launchapp1", "launchapp2", "launchmediaselect",
    "add", "subtract", "multiply", "divide", "decimal", "separator", "clear", "select", "execute", "help",
    "accept", "convert", "nonconvert", "modechange", "final", "hanguel", "hangul", "hanja", "junja",
    "kana", "kanji", "yen", "fn",
    *{f"f{i}" for i in range(1, 25)},
    *{f"num{i}" for i in range(10)},
}

MODIFIERS = {"ctrl", "ctrlleft", "ctrlright", "alt", "altleft", "altright", "shift", "shiftleft",
             "shiftright", "win", "winleft", "winright"}

_COMBO_SPLIT = re.compile(r"\s*(?:\+|-|\s)\s*")


def normalize_key(key: str) -> str:
    """Return the canonical pyautogui name for ``key``.

    Raises ``ValueError`` for names that cannot be mapped.
    """
    if key is None:
        raise ValueError("key is None")
    raw = str(key)
    if len(raw) == 1:
        if raw in _ALIASES:          # " ", "\n", "\r"
            return _ALIASES[raw]
        return raw.lower() if raw.isalpha() else raw
    stripped = raw.strip()
    if len(stripped) == 1:           # " C " -> "c"
        return normalize_key(stripped)
    k = stripped.lower().replace("_", "").replace(" ", "")
    if not k:
        raise ValueError("empty key name")
    if k in _ALIASES:
        return _ALIASES[k]
    if k in _CANONICAL:
        return k
    # "KeyA", "VK_RETURN" style names
    for prefix in ("key", "vk"):
        if k.startswith(prefix) and len(k) > len(prefix):
            rest = k[len(prefix):]
            if rest in _ALIASES:
                return _ALIASES[rest]
            if rest in _CANONICAL:
                return rest
            if len(rest) == 1:
                return rest
    raw_lower = stripped.lower()
    if raw_lower in _ALIASES:
        return _ALIASES[raw_lower]
    if raw_lower in _CANONICAL:
        return raw_lower
    raise ValueError(f"Unknown key name: {raw!r}")


def parse_key_combo(combo: str | list[str]) -> list[str]:
    """Parse ``"ctrl+shift+s"`` / ``["Ctrl", "S"]`` into canonical key names."""
    if isinstance(combo, (list, tuple)):
        parts = [str(p) for p in combo]
    else:
        text = str(combo).strip()
        if len(text) == 1:
            parts = [text]
        elif "+" in text and text != "+":
            # "ctrl++" means ctrl and plus
            parts = []
            for p in text.split("+"):
                parts.append(p if p else "+")
            parts = [p for p in parts if p != ""]
            # collapse the artefact of "ctrl++" -> ["ctrl", "+", "+"]
            cleaned: list[str] = []
            for p in parts:
                if p == "+" and cleaned and cleaned[-1] == "+":
                    continue
                cleaned.append(p)
            parts = cleaned
        elif "-" in text and len(text) > 1 and not text.startswith("-"):
            parts = [p for p in text.split("-") if p]
        else:
            parts = [p for p in text.split() if p]
    keys = [normalize_key(p) for p in parts]
    if not keys:
        raise ValueError("empty key combination")
    # modifiers first (stable), preserving order of the rest
    mods = [k for k in keys if k in MODIFIERS]
    rest = [k for k in keys if k not in MODIFIERS]
    return mods + rest


# --- Windows virtual-key codes (for RegisterHotKey / SendInput) -----------------
VK_CODES: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "select": 0x29,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "help": 0x2F, "win": 0x5B, "winleft": 0x5B,
    "winright": 0x5C, "apps": 0x5D, "sleep": 0x5F, "multiply": 0x6A, "add": 0x6B, "separator": 0x6C,
    "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F, "numlock": 0x90, "scrolllock": 0x91,
    "shiftleft": 0xA0, "shiftright": 0xA1, "ctrlleft": 0xA2, "ctrlright": 0xA3, "altleft": 0xA4,
    "altright": 0xA5, "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF, "nexttrack": 0xB0,
    "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    **{f"num{i}": 0x60 + i for i in range(10)},
    **{str(i): 0x30 + i for i in range(10)},
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
}

MOD_FLAGS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}


def hotkey_to_vk(combo: str) -> tuple[int, int]:
    """Convert ``"ctrl+alt+esc"`` into ``(modifier_flags, vk_code)`` for RegisterHotKey."""
    keys = parse_key_combo(combo)
    mods = 0
    vk = None
    for k in keys:
        base = k.replace("left", "").replace("right", "") if k in MODIFIERS else k
        if base in MOD_FLAGS:
            mods |= MOD_FLAGS[base]
        else:
            if vk is not None:
                raise ValueError("hotkey may contain only one non-modifier key")
            if k not in VK_CODES:
                raise ValueError(f"cannot map {k!r} to a virtual-key code")
            vk = VK_CODES[k]
    if vk is None:
        raise ValueError("hotkey needs a non-modifier key")
    return mods, vk
