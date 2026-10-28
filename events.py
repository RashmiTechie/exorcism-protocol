"""Blackboard event log: one envelope, typed events, scoped reads per agent."""
from __future__ import annotations

import itertools
import json
import os
from decimal import Decimal
from dataclasses import asdict, dataclass, field
from typing import Callable


@dataclass
class Event:
    event_id: str
    run_id: str
    type: str
    tick: int
    source: str
    zone: str | None = None
    room: str | None = None
    correlation_id: str | None = None
    confidence: float | None = None
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EventLog:
    """In-memory log for speed. Optional DynamoDB sink for replay (set DDB_TABLE)."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._ids = itertools.count(1)
        self._subscribers: list[Callable[[Event], None]] = []
        self._table = None
        table_name = os.getenv("DDB_TABLE")
        if table_name:
            import boto3  # only needed on AWS
            self._table = boto3.resource("dynamodb").Table(table_name)

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def publish(self, run_id: str, tick: int, type_: str, source: str, **kw) -> Event:
        ev = Event(f"evt_{next(self._ids):05d}", run_id, type_, tick, source, **kw)
        self.events.append(ev)
        for fn in self._subscribers:
            fn(ev)
        if self._table is not None and type_ != "SENSOR_READING":
            item = ev.to_dict()
            item["sk"] = f"{tick:05d}#{ev.event_id}"
            item = {k: v for k, v in item.items() if v is not None}
            # DynamoDB rejects Python floats: round-trip through JSON as Decimal
            item = json.loads(json.dumps(item), parse_float=Decimal)
            self._table.put_item(Item=item)
        return ev

    def query(self, run_id: str, types: set[str], zone: str | None = None,
              since_tick: int = 0) -> list[Event]:
        """The ONLY read path agents get. Each agent's tool fixes `types` and `zone`."""
        return [e for e in self.events
                if e.run_id == run_id and e.type in types and e.tick >= since_tick
                and (zone is None or e.zone == zone)]

    def trace(self, run_id: str, correlation_id: str) -> list[Event]:
        return [e for e in self.events
                if e.run_id == run_id and e.correlation_id == correlation_id]


# What each role is allowed to see. Enforced here, not in prompts.
SCOPES: dict[str, tuple[set[str], str | None]] = {
    "sentinel_east": ({"SENSOR_READING"}, "east"),
    "sentinel_west": ({"SENSOR_READING"}, "west"),
    "investigator": ({"SIGHTING_REPORTED"}, None),
    "researcher": ({"SIGHTING_CONFIRMED"}, None),
    "commander": ({"SIGHTING_CONFIRMED", "PLAYBOOK_FOUND", "ANOMALY_CONTAINED",
                   "ACTION_APPROVED", "ACTION_REJECTED"}, None),
    "evacuation": ({"SIGHTING_CONFIRMED", "CIVILIAN_STATUS", "ACTION_EXECUTED"}, None),
    "supervisor": ({"ACTION_PROPOSED"}, None),
    "baseline": ({"SENSOR_READING", "ANOMALY_CONTAINED"}, None),
}


def scoped_view(log: EventLog, run_id: str, role: str, since_tick: int = 0) -> list[Event]:
    types, zone = SCOPES[role]
    return log.query(run_id, types, zone, since_tick)
