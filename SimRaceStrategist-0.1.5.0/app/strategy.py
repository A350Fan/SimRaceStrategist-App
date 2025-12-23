from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List

@dataclass
class StrategyCard:
    name: str
    description: str
    next_pit_lap: Optional[int] = None
    tyre_plan: str = ""
    confidence: float = 0.5

def generate_placeholder_cards() -> List[StrategyCard]:
    return [
        StrategyCard(name="Plan A (Safe)", description="Konservativ / späterer Stop", tyre_plan="M → H"),
        StrategyCard(name="Plan B (Aggro)", description="Früher Stop / Undercut", tyre_plan="S → M → S"),
        StrategyCard(name="Plan C (SC-ready)", description="Flexibel bei SC/VSC", tyre_plan="M → H (flex window)"),
    ]
