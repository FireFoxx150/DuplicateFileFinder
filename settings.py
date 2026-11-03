"""Remember the last-used scan settings between runs."""
import json
import os

DEFAULTS = {
    "folder": "",
    "folder_b": "",
    "mode": "Exact copies",
    "min_size_kb": "0",
    "extensions": "",
    "exclude_dirs": "",
    "include_hidden": False,
    "max_distance": "5",
    "keep_rule": "newest",
    "theme": "system",
    "font_size": 10,
    "geometry": "",
    "recent": [],
    "skip_presets": False,
    "max_depth": "",
    "verify_bytes": False,
    "keep_folder": "",
    "quarantine": "",
    "profiles": {},
    "actions": [],
}


def default_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "dupefinder", "settings.json")


def load(path=None):
    try:
        with open(path or default_path()) as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(data, path=None):
    path = path or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
