from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List

# WIP/PROTOTYPE: Data model for the Strategy Cards UI.
# This is already used by the UI, but the *contents* are currently static demo data.
@dataclass
class StrategyCard:
    name: str
    description: str
    next_pit_lap: Optional[int] = None
    tyre_plan: str = ""
    confidence: float = 0.5


# WIP/PROTOTYPE: Static cards used to validate UI layout and interactions.
# Later this should be replaced by real strategy generation based on live telemetry + model outputs.
def generate_placeholder_cards() -> List[StrategyCard]:
    return [
        StrategyCard(name="Plan A (Safe)", description="Conservative / later stop", tyre_plan="M → H"),
        StrategyCard(name="Plan B (Aggro)", description="Earlier stop / undercut", tyre_plan="S → M → S"),
        StrategyCard(name="Plan C (SC-ready)", description="Flexible for SC/VSC", tyre_plan="M → H (flex window)"),
    ]