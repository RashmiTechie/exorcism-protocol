"""Facility simulator: ground truth, noisy sensors, actions and scoring. No AI here."""
from __future__ import annotations

import random
import zlib
from dataclasses import dataclass, field

from events import EventLog

ANOMALY_TYPES = ["zombie", "phantom", "poltergeist", "banshee", "wraith"]
SIGNATURES = {
    "zombie": "moaning+motion_cluster",
    "phantom": "cold_spot+emf_spike",
    "poltergeist": "object_motion+vibration",
    "banshee": "audio_shriek+glass_stress",
    "wraith": "light_drop+cold_spot",
}

ROWS, COLS = 4, 5            # 20 rooms; rows 0-1 = West, rows 2-3 = East
SPREAD_EVERY = 4             # an uncontained anomaly spreads every N ticks
OVERRUN_AFTER = 7            # civilians in a room are lost after N uncontained ticks
EVAC_RATE = 4                # civilians leaving an evacuating room per tick
CONTAIN_TICKS = {True: 2, False: 4}   # with playbook / without
FALSE_POSITIVE_RATE = 0.06   # chance an empty room reads positive on a tick
TRUE_POSITIVE_RATE = 0.9
TEAMS = 3


def room_id(r: int, c: int) -> str:
    zone = "W" if r < 2 else "E"
    return f"{zone}{(r % 2) * COLS + c + 1}"


@dataclass
class Room:
    id: str
    zone: str
    r: int
    c: int
    civilians: int = 2
    sealed: bool = False
    evacuating: bool = False
    overrun: bool = False


@dataclass
class Anomaly:
    id: str
    kind: str
    room: str
    spawned: int
    age: int = 0
    contained_at: int | None = None
    containing_until: int | None = None
    corr: str | None = None


@dataclass
class Team:
    id: str
    busy_until: int = -1
    room: str | None = None


@dataclass
class Facility:
    seed: int
    log: EventLog
    run_id: str
    tick: int = 0
    rooms: dict[str, Room] = field(default_factory=dict)
    anomalies: list[Anomaly] = field(default_factory=list)
    teams: list[Team] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)   # for false-alarm scoring
    lost: int = 0
    evacuated: int = 0
    spawn_plan: list[tuple[int, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        plan_rng = random.Random(self.seed)
        for r in range(ROWS):
            for c in range(COLS):
                rid = room_id(r, c)
                self.rooms[rid] = Room(rid, "west" if r < 2 else "east", r, c,
                                       civilians=plan_rng.randint(1, 3))
        self.teams = [Team(f"team{i + 1}") for i in range(TEAMS)]
        ids = list(self.rooms)
        for _ in range(6):
            self.spawn_plan.append((plan_rng.randint(1, 45), plan_rng.choice(ids),
                                    plan_rng.choice(ANOMALY_TYPES)))
        self.spawn_plan.sort()

    # ---------- helpers ----------
    def _rng(self, *parts) -> random.Random:
        """Deterministic per (seed, tick, ...) so swarm and baseline see identical noise."""
        return random.Random(zlib.crc32(repr((self.seed, self.tick) + parts).encode()))

    def neighbors(self, rid: str) -> list[str]:
        room = self.rooms[rid]
        out = []
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            r, c = room.r + dr, room.c + dc
            if 0 <= r < ROWS and 0 <= c < COLS:
                out.append(room_id(r, c))
        return out

    def active_in(self, rid: str) -> Anomaly | None:
        for a in self.anomalies:
            if a.room == rid and a.contained_at is None:
                return a
        return None

    def free_teams(self) -> list[Team]:
        return [t for t in self.teams if t.busy_until < self.tick]

    def _emit(self, type_: str, source: str = "simulator", **kw):
        return self.log.publish(self.run_id, self.tick, type_, source, **kw)

    # ---------- actions (called by agents after supervisor approval) ----------
    def seal(self, rid: str, by: str, corr: str | None = None) -> bool:
        room = self.rooms[rid]
        if room.sealed:
            return False
        room.sealed = True
        room.evacuating = False
        real = self.active_in(rid) is not None
        self.actions.append({"tick": self.tick, "kind": "seal", "room": rid, "real": real})
        self._emit("ACTION_EXECUTED", room=rid, zone=room.zone, correlation_id=corr,
                   payload={"action": "seal", "by": by, "real": real})
        return True

    def deploy(self, rid: str, by: str, has_playbook: bool, corr: str | None = None) -> bool:
        free = self.free_teams()
        if not free:
            return False
        team = free[0]
        anomaly = self.active_in(rid)
        real = anomaly is not None
        duration = CONTAIN_TICKS[has_playbook] if real else 3
        team.busy_until = self.tick + duration
        team.room = rid
        if anomaly and anomaly.containing_until is None:
            anomaly.containing_until = self.tick + duration
            anomaly.corr = corr
        self.actions.append({"tick": self.tick, "kind": "deploy", "room": rid, "real": real})
        self._emit("ACTION_EXECUTED", room=rid, zone=self.rooms[rid].zone, correlation_id=corr,
                   payload={"action": "deploy", "team": team.id, "by": by,
                            "playbook": has_playbook, "real": real})
        return True

    def evacuate(self, rid: str, by: str, corr: str | None = None) -> bool:
        room = self.rooms[rid]
        if room.sealed or room.overrun or room.civilians == 0 or room.evacuating:
            return False
        room.evacuating = True
        self._emit("ACTION_EXECUTED", room=rid, zone=room.zone, correlation_id=corr,
                   payload={"action": "evacuate", "by": by, "civilians": room.civilians})
        return True

    # ---------- the tick ----------
    def step(self) -> None:
        self.tick += 1
        t = self.tick

        for when, rid, kind in self.spawn_plan:
            if when == t and self.active_in(rid) is None and not self.rooms[rid].overrun:
                self.anomalies.append(Anomaly(f"anom_{len(self.anomalies) + 1:02d}", kind, rid, t))

        # containment completes
        for a in self.anomalies:
            if a.contained_at is None and a.containing_until is not None and t >= a.containing_until:
                a.contained_at = t
                self._emit("ANOMALY_CONTAINED", room=a.room, zone=self.rooms[a.room].zone,
                           correlation_id=a.corr, payload={"anomaly": a.id, "kind": a.kind, "ticks": t - a.spawned})

        # age, spread, overrun
        for a in list(self.anomalies):
            if a.contained_at is not None:
                continue
            a.age += 1
            room = self.rooms[a.room]
            if a.age >= OVERRUN_AFTER and not room.overrun:
                room.overrun = True
                self.lost += room.civilians
                self._emit("CIVILIAN_STATUS", room=room.id, zone=room.zone,
                           payload={"lost": room.civilians, "reason": "overrun"})
                room.civilians = 0
                room.evacuating = False
            if a.age % SPREAD_EVERY == 0 and not room.sealed:
                options = [n for n in self.neighbors(a.room)
                           if self.active_in(n) is None and not self.rooms[n].overrun]
                if options:
                    target = self._rng("spread", a.id).choice(sorted(options))
                    self.anomalies.append(Anomaly(f"anom_{len(self.anomalies) + 1:02d}",
                                                  a.kind, target, t))

        # evacuation
        for room in self.rooms.values():
            if room.evacuating and room.civilians > 0:
                moved = min(EVAC_RATE, room.civilians)
                room.civilians -= moved
                self.evacuated += moved
                if room.civilians == 0:
                    room.evacuating = False
                    self._emit("CIVILIAN_STATUS", room=room.id, zone=room.zone,
                               payload={"evacuated_room": True})

        # noisy sensors
        for room in self.rooms.values():
            anomaly = self.active_in(room.id)
            rng = self._rng("sensor", room.id)
            if anomaly:
                positive = rng.random() < TRUE_POSITIVE_RATE
                sig = SIGNATURES[anomaly.kind]
            else:
                positive = rng.random() < FALSE_POSITIVE_RATE
                sig = SIGNATURES[rng.choice(ANOMALY_TYPES)]
            self._emit("SENSOR_READING", room=room.id, zone=room.zone,
                       payload={"positive": positive, "signature": sig if positive else None})

        self._emit("SCORE_UPDATE", payload=self.score())

    # ---------- inspection (investigator's only view of truth) ----------
    def inspect(self, rid: str) -> str | None:
        a = self.active_in(rid)
        return a.kind if a else None

    # ---------- scoring ----------
    def score(self) -> dict:
        speeds = [(a.contained_at or self.tick) - a.spawned for a in self.anomalies]
        acts = [a for a in self.actions if a["kind"] in ("seal", "deploy")]
        false = sum(1 for a in acts if not a["real"])
        total_civ = self.lost + self.evacuated + sum(r.civilians for r in self.rooms.values())
        return {
            "tick": self.tick,
            "anomalies": len(self.anomalies),
            "contained": sum(1 for a in self.anomalies if a.contained_at is not None),
            "containment_speed": round(sum(speeds) / len(speeds), 1) if speeds else 0.0,
            "false_alarm_rate": round(false / len(acts), 2) if acts else 0.0,
            "actions": len(acts),
            "civilians_saved": total_civ - self.lost,
            "civilians_lost": self.lost,
            "civilians_total": total_civ,
        }

    def snapshot(self) -> dict:
        return {
            "tick": self.tick,
            "rooms": [{"id": r.id, "r": r.r, "c": r.c, "civ": r.civilians, "sealed": r.sealed,
                       "evac": r.evacuating, "overrun": r.overrun,
                       "anomaly": (self.active_in(r.id).kind if self.active_in(r.id) else None),
                       "team": any(t.room == r.id and t.busy_until >= self.tick for t in self.teams)}
                      for r in self.rooms.values()],
            "score": self.score(),
        }
