from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


class Translator:
    """
    Simple JSON key/value translator.
    Loads files from ../lang/<lang>.json (relative to this file), so it works
    regardless of the current working directory.
    """

    def __init__(self, lang: str = "en"):
        self.lang: str = lang
        self.data: Dict[str, Any] = {}
        self.load_language(lang)

    @staticmethod
    def _lang_dir() -> Path:
        # .../app/translator.py -> .../lang
        return Path(__file__).resolve().parent.parent / "lang"

    def available_languages(self) -> list[str]:
        d = self._lang_dir()
        if not d.exists():
            return []
        return sorted([p.stem for p in d.glob("*.json") if p.is_file()])

    def load_language(self, lang: str) -> None:
        self.lang = lang
        path = self._lang_dir() / f"{lang}.json"
        if not path.exists():
            raise FileNotFoundError(f"Language file not found: {path}")

        self.data = json.loads(path.read_text(encoding="utf-8"))

    def t(self, key: str, default: Optional[str] = None) -> str:
        # Fallback: default oder [key], falls Übersetzung fehlt
        if key in self.data:
            try:
                return str(self.data.get(key))
            except Exception:
                return f"[{key}]"
        if default is not None:
            return default
        return f"[{key}]"

    @staticmethod
    def language_display_names() -> dict[str, str]:
        """
        Human-readable language names for UI.
        Keys = language codes, values = native display names.
        """
        return {
            "en": "English",
            "de": "Deutsch",
            "fr": "Français",
            "it": "Italiano",
            "es": "Español",
            "pt": "Português",
            "lk": "සිංහල"
        }

