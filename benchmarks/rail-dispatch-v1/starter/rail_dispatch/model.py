from dataclasses import dataclass, asdict
from typing import Any

@dataclass
class Train:
    train_id: str
    priority: str = 'passenger'
    length: float = 1
    status: str = 'active'
    section: str = 'A'
    last_arrive: float = 0
    def json(self): return asdict(self)

PRIORITY = {'freight': 0, 'passenger': 1, 'emergency': 2}
def priority_of(value: Any) -> str:
    return value if value in PRIORITY else 'passenger'
