# WIP, only base

import json
from pathlib import Path

class Translator:
    def __init__(self, lang="en"):
        self.lang = lang
        self.data = {}
        self.load_language(lang)

    def load_language(self, lang):
        self.lang = lang
        path = Path("lang") / f"{lang}.json"

        if not path.exists():
            raise FileNotFoundError(f"Language file not found: {path}")

        with open(path, encoding="utf-8") as f:
            self.data = json.load(f)

    def t(self, key):
        # Fallback: Key anzeigen, falls Übersetzung fehlt
        return self.data.get(key, f"[{key}]")
