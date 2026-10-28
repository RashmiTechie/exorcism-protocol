"""Rule-based agents. Each one reads ONLY its scoped view of the event log.

These are the fallback layer: swap any role's `decide` for an LLM call (see crew_agents.py)
and keep the rule as the timeout fallback.
"""
from __future__ import annotations

import itertools
from collections import defaultdict, deque

import youcom
from events import SCOPES, EventLog
from sim import EVAC_RATE, SIGNATURES, Facility

KIND_BY_SIG = {v: k for k, v in SIGNATURES.items()}
_inc = itertools.count(1)


class Agent:
    role = "agent"

    def __init__(self, log: EventLog, run_id: str):
        self.log, self.run_id, self.cursor = log, run_id, 0

    def view(self, tick: int):
        """New events since this agent last looked, filtered to its scope."""
        types, zone = SCOPES[self.role]
        new = self.log.events[self.cursor:]
        self.cursor = len(self.log.events)
        return [e for e in new if e.run_id == self.run_id and e.type in types
                and (zone is None or e.zone == zone)]

    def emit(self, fac: Facility, type_: str, **kw):
        return self.log.publish(self.run_id, fac.tick, type_, self.role, **kw)


class Sentinel(Agent):
    """Reports a sighting when a room reads positive on 2 of the last 3 ticks."""

    def __init__(self, log, run_id, zone):
        super().__init__(log, run_id)
        self.role, self.zone = f"sentinel_{zone}", zone
        self.history = defaultdict(lambda: deque(maxlen=3))
        self.cooldown: dict[str, int] = {}

    def act(self, fac: Facility):
        for e in self.view(fac.tick):
            self.history[e.room].append(e.payload.get("signature"))
        for room, hist in self.history.items():
            hits = [s for s in hist if s]
            if len(hits) >= 2 and self.cooldown.get(room, -99) < fac.tick:
                self.cooldown[room] = fac.tick + 6
                suspected = KIND_BY_SIG.get(max(set(hits), key=hits.count))
                self.emit(fac, "SIGHTING_REPORTED", zone=self.zone, room=room,
                          correlation_id=f"inc_{next(_inc):03d}",
                          confidence=round(len(hits) / 3, 2),
                          payload={"suspected": suspected, "signatures": hits})


class Investigator(Agent):
    role = "investigator"
    CAPACITY, DURATION = 2, 2

    def __init__(self, log, run_id):
        super().__init__(log, run_id)
        self.queue, self.active = [], []   # active: (done_tick, event)

    def act(self, fac: Facility):
        self.queue += self.view(fac.tick)
        for done, ev in list(self.active):
            if fac.tick >= done:
                self.active.remove((done, ev))
                kind = fac.inspect(ev.room)   # the investigator's only window on truth
                if kind:
                    self.emit(fac, "SIGHTING_CONFIRMED", zone=ev.zone, room=ev.room,
                              correlation_id=ev.correlation_id, confidence=1.0,
                              payload={"kind": kind})
                else:
                    self.emit(fac, "SIGHTING_DISMISSED", zone=ev.zone, room=ev.room,
                              correlation_id=ev.correlation_id,
                              payload={"reason": "nothing found on inspection"})
        self.queue.sort(key=lambda e: -(e.confidence or 0))
        while self.queue and len(self.active) < self.CAPACITY:
            self.active.append((fac.tick + self.DURATION, self.queue.pop(0)))


class Researcher(Agent):
    role = "researcher"

    def act(self, fac: Facility):
        for ev in self.view(fac.tick):
            pb = youcom.lookup(ev.payload["kind"])
            self.emit(fac, "PLAYBOOK_FOUND", zone=ev.zone, room=ev.room,
                      correlation_id=ev.correlation_id, payload=pb)


class Commander(Agent):
    role = "commander"

    def __init__(self, log, run_id):
        super().__init__(log, run_id)
        self.open: dict[str, object] = {}       # room -> confirming event
        self.playbooks: set[str] = set()
        self.sealed_req: set[str] = set()

    def act(self, fac: Facility):
        for ev in self.view(fac.tick):
            if ev.type == "SIGHTING_CONFIRMED":
                self.open[ev.room] = ev
                if ev.room not in self.sealed_req:   # seal to stop the spread
                    self.sealed_req.add(ev.room)
                    self.emit(fac, "ACTION_PROPOSED", zone=ev.zone, room=ev.room,
                              correlation_id=ev.correlation_id,
                              payload={"action": "seal", "why": "confirmed, stop the spread"})
            elif ev.type == "PLAYBOOK_FOUND":
                self.playbooks.add(ev.payload["kind"])
            elif ev.type == "ANOMALY_CONTAINED":
                self.open.pop(ev.room, None)
            elif ev.type == "ACTION_APPROVED" and ev.payload.get("action") == "deploy":
                self.open.pop(ev.room, None)      # a team is on it
            elif ev.type == "ACTION_REJECTED" and ev.payload.get("reason") == "no_team" \
                    and ev.room in self.open and ev.room not in self.sealed_req:
                self.sealed_req.add(ev.room)
                self.emit(fac, "ACTION_PROPOSED", zone=ev.zone, room=ev.room,
                          correlation_id=ev.correlation_id,
                          payload={"action": "seal", "why": "no team free, stop the spread"})
        for room, ev in self.open.items():
            self.emit(fac, "ACTION_PROPOSED", zone=ev.zone, room=room,
                      correlation_id=ev.correlation_id,
                      payload={"action": "deploy",
                               "playbook": ev.payload["kind"] in self.playbooks})


class Evacuation(Agent):
    role = "evacuation"

    def __init__(self, log, run_id):
        super().__init__(log, run_id)
        self.done: set[str] = set()

    def act(self, fac: Facility):
        for ev in self.view(fac.tick):
            if ev.type != "SIGHTING_CONFIRMED":
                continue
            for room in [ev.room] + fac.neighbors(ev.room):
                r = fac.rooms[room]                   # allowed view: civilians + doors
                if room not in self.done and r.civilians > 0 and not r.sealed:
                    self.done.add(room)
                    self.emit(fac, "ACTION_PROPOSED", zone=r.zone, room=room,
                              correlation_id=ev.correlation_id,
                              payload={"action": "evacuate", "civilians": r.civilians})


class Supervisor(Agent):
    """Approves proposals and resolves seal-versus-evacuation conflicts."""
    role = "supervisor"

    def __init__(self, log, run_id):
        super().__init__(log, run_id)
        self.deferred: list = []
        self.deployed: set[str] = set()
        self.rejected: set[str] = set()

    def act(self, fac: Facility):
        props = self.deferred + self.view(fac.tick)
        self.deferred = []
        evac_rooms = {p.room for p in props if p.payload["action"] == "evacuate"}
        for p in props:
            action, room = p.payload["action"], p.room
            if action == "deploy":
                if room in self.deployed:
                    continue
                if fac.deploy(room, "supervisor", p.payload.get("playbook", False),
                              p.correlation_id):
                    self.deployed.add(room)
                    self.emit(fac, "ACTION_APPROVED", zone=p.zone, room=room,
                              correlation_id=p.correlation_id, payload={"action": "deploy"})
                elif room not in self.rejected:
                    self.rejected.add(room)
                    self.emit(fac, "ACTION_REJECTED", zone=p.zone, room=room,
                              correlation_id=p.correlation_id,
                              payload={"action": "deploy", "reason": "no_team"})
            elif action == "seal":
                r = fac.rooms[room]
                if (room in evac_rooms or r.evacuating) and r.civilians <= 2 * EVAC_RATE \
                        and r.civilians > 0:
                    self.deferred.append(p)      # let evacuation finish first
                    self.emit(fac, "ACTION_REJECTED", zone=p.zone, room=room,
                              correlation_id=p.correlation_id,
                              payload={"action": "seal", "reason": "conflict",
                                       "decision": f"evacuate {r.civilians} civilians first"})
                    continue
                fac.seal(room, "supervisor", p.correlation_id)
                self.emit(fac, "ACTION_APPROVED", zone=p.zone, room=room,
                          correlation_id=p.correlation_id, payload={"action": "seal"})
            elif action == "evacuate":
                if fac.evacuate(room, "supervisor", p.correlation_id):
                    self.emit(fac, "ACTION_APPROVED", zone=p.zone, room=room,
                              correlation_id=p.correlation_id, payload={"action": "evacuate"})
        # retry deploys once a team frees up: re-open rooms whose anomaly is still active
        self.deployed = {r for r in self.deployed if fac.active_in(r) is not None
                         and any(t.room == r and t.busy_until >= fac.tick for t in fac.teams)}


class Swarm:
    def __init__(self, log: EventLog, run_id: str):
        self.agents = [Sentinel(log, run_id, "east"), Sentinel(log, run_id, "west"),
                       Investigator(log, run_id), Researcher(log, run_id),
                       Commander(log, run_id), Evacuation(log, run_id), Supervisor(log, run_id)]

    def act(self, fac: Facility):
        for a in self.agents:
            a.act(fac)


class Baseline(Agent):
    """One agent, all raw sensors, no verification, no playbook. Acts on any positive reading."""
    role = "baseline"

    def __init__(self, log, run_id):
        super().__init__(log, run_id)
        self.cooldown: dict[str, int] = {}

    def act(self, fac: Facility):
        for e in self.view(fac.tick):
            if e.type != "SENSOR_READING" or not e.payload["positive"]:
                continue
            if self.cooldown.get(e.room, -99) >= fac.tick:
                continue
            self.cooldown[e.room] = fac.tick + 6
            fac.evacuate(e.room, "baseline")
            if not fac.deploy(e.room, "baseline", has_playbook=False):
                fac.seal(e.room, "baseline")
