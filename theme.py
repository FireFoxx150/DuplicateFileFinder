"""Light and dark colour schemes, plus the font size, for the Tk window."""
import subprocess
from tkinter import font, ttk

LIGHT = {"bg": "#f0f0f0", "fg": "#000000", "field": "#ffffff", "select": "#cce4f7",
         "button": "#e1e1e1", "trough": "#d9d9d9", "border": "#b5b5b5", "muted": "#666666"}
DARK = {"bg": "#2b2b2b", "fg": "#e6e6e6", "field": "#3c3c3c", "select": "#44607a",
        "button": "#3a3a3a", "trough": "#333333", "border": "#555555", "muted": "#a0a0a0"}
FONT_NAMES = ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont")


def system_prefers_dark():
    """Ask the desktop for its colour scheme. Falls back to light when it can't be read."""
    for schema, key in (("org.gnome.desktop.interface", "color-scheme"),
                        ("org.cinnamon.desktop.interface", "gtk-theme")):
        try:
            out = subprocess.run(["gsettings", "get", schema, key],
                                 capture_output=True, text=True, timeout=2).stdout.lower()
        except (OSError, subprocess.SubprocessError):
            continue
        if "dark" in out:
            return True
    return False


HIGH_CONTRAST = {"bg": "#000000", "fg": "#ffffff", "field": "#000000", "select": "#ffff00",
                 "button": "#000000", "trough": "#000000", "border": "#ffffff", "muted": "#cccccc"}


def palette(mode):
    if mode == "high contrast":
        return HIGH_CONTRAST
    if mode == "dark" or (mode == "system" and system_prefers_dark()):
        return DARK
    return LIGHT


def apply(root, mode, font_size):
    """Restyle the ttk widgets and default fonts. Returns the palette so plain Tk widgets can match it."""
    p = palette(mode)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
                    bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                    troughcolor=p["trough"], selectbackground=p["select"], selectforeground=p["fg"],
                    insertcolor=p["fg"])
    style.configure("TButton", background=p["button"], foreground=p["fg"])
    style.map("TButton", background=[("active", p["select"]), ("disabled", p["bg"])],
              foreground=[("disabled", p["muted"])])
    style.configure("TMenubutton", background=p["button"], foreground=p["fg"])
    style.map("TMenubutton", background=[("active", p["select"])])
    style.map("TEntry", fieldbackground=[("disabled", p["bg"])], foreground=[("disabled", p["muted"])])
    style.map("TCombobox", fieldbackground=[("readonly", p["field"]), ("disabled", p["bg"])],
              foreground=[("readonly", p["fg"]), ("disabled", p["muted"])], selectbackground=[("readonly", p["field"])],
              selectforeground=[("readonly", p["fg"])])
    style.configure("TCheckbutton", background=p["bg"], foreground=p["fg"])
    style.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["fg"])
    style.configure("Treeview.Heading", background=p["button"], foreground=p["fg"])
    style.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", p["fg"])])
    style.configure("TProgressbar", background=p["select"], troughcolor=p["trough"])
    for name in FONT_NAMES:
        font.nametofont(name).configure(size=font_size)
    return p
