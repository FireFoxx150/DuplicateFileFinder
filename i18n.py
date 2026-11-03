"""Translated interface text. Translations live in locale/<language>/LC_MESSAGES/dupefinder.mo."""
import gettext
import os
import sys

DOMAIN = "dupefinder"


def locale_dir():
    if getattr(sys, "frozen", False):  # packaged build: PyInstaller unpacks data files here
        return os.path.join(sys._MEIPASS, "locale")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")


_translation = gettext.translation(DOMAIN, locale_dir(), fallback=True)


def _(text):
    return _translation.gettext(text)
