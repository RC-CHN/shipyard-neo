"""Runtime adapters.

See: plans/phase-1/capability-adapter-design.md
"""

from app.adapters.base import BaseAdapter, ExecutionResult, RuntimeMeta
from app.adapters.gull import GullAdapter
from app.adapters.ship import ShipAdapter

__all__ = [
    "BaseAdapter",
    "ExecutionResult",
    "RuntimeMeta",
    "ShipAdapter",
    "GullAdapter",
]
