"""Template: put an LLM behind a role with CrewAI, keeping the rule-based agent as fallback.

UNTESTED here (no keys in this sandbox). Check against the CrewAI and Parasail docs at the
event; class names below match recent CrewAI releases (Agent, Task, Crew, LLM, @tool).

    pip install crewai
    export PARASAIL_API_KEY=...  PARASAIL_BASE_URL=...  PARASAIL_MODEL=...

Usage in agents.Swarm: replace Supervisor(log, run_id) with LLMSupervisor(log, run_id).
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from crewai import LLM, Agent, Crew, Task

from agents import Supervisor
from sim import Facility

LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "6"))

def make_llm() -> LLM:
    """Pick the model provider with LLM_PROVIDER=parasail (default) or bedrock."""
    if os.getenv("LLM_PROVIDER", "parasail") == "bedrock":
        # pip install "crewai[bedrock]". Credentials come from the normal AWS chain:
        # `aws configure` on a laptop, the task role on Fargate. Copy the exact model ID
        # from the Bedrock console; IDs vary by region.
        return LLM(
            model=f"bedrock/{os.getenv('BEDROCK_MODEL_ID', 'us.anthropic.claude-sonnet-4-6')}",
            region_name=os.getenv("AWS_REGION_NAME", "us-east-1"),
            temperature=0.2,
        )
    # Parasail is typically reached through an OpenAI-compatible endpoint; confirm at the event.
    return LLM(
        model=f"openai/{os.getenv('PARASAIL_MODEL', 'CONFIRM-MODEL-NAME')}",
        base_url=os.getenv("PARASAIL_BASE_URL", "CONFIRM-BASE-URL"),
        api_key=os.getenv("PARASAIL_API_KEY", ""),
        temperature=0.2,
    )


llm = make_llm()

supervisor_agent = Agent(
    role="Containment Supervisor",
    goal="Approve or reject proposed actions to contain anomalies while saving civilians.",
    backstory=("You command a paranormal containment swarm. You only see proposals from "
               "other agents, never raw sensors. Sealing a room stops spread but blocks "
               "evacuation through it. Teams are scarce (3)."),
    llm=llm,
    allow_delegation=False,
    verbose=False,
)

_pool = ThreadPoolExecutor(max_workers=2)


class LLMSupervisor(Supervisor):
    """Asks the LLM to order and judge proposals; falls back to the rules on timeout."""

    def act(self, fac: Facility):
        props = self.deferred + self.view(fac.tick)
        self.deferred = []
        if not props:
            return self._execute(fac, [])
        brief = [{"id": p.event_id, "action": p.payload["action"], "room": p.room,
                  "civilians_in_room": fac.rooms[p.room].civilians,
                  "evacuating": fac.rooms[p.room].evacuating} for p in props]
        task = Task(
            description=("Proposals this tick:\n" + json.dumps(brief) +
                         "\nReturn JSON: {\"approve\": [ids], \"defer\": [ids], "
                         "\"reason\": \"one sentence\"}. Defer a seal if civilians are "
                         "still evacuating that room and can finish within 2 ticks."),
            expected_output="A JSON object with approve, defer and reason.",
            agent=supervisor_agent,
        )
        try:
            out = _pool.submit(lambda: Crew(agents=[supervisor_agent], tasks=[task]).kickoff()) \
                       .result(timeout=LLM_TIMEOUT_S)
            text = str(out).strip().removeprefix("```json").removeprefix("```").removesuffix("```")
            decision = json.loads(text)
            defer = set(decision.get("defer", []))
            held = [p for p in props if p.event_id in defer]
            approved = [p for p in props if p.event_id not in defer]
            self.emit(fac, "ACTION_APPROVED", payload={"action": "llm_decision",
                                                       "reason": decision.get("reason", "")})
        except (FutureTimeout, Exception):  # timeout, bad JSON, API error
            held, approved = [], props          # rules decide everything this tick
        # hand the approved set to the rule engine, which executes and logs
        self._execute(fac, approved)
        self.deferred += held

    def _execute(self, fac: Facility, props):
        saved_view = self.view
        self.view = lambda tick: props
        try:
            super().act(fac)
        finally:
            self.view = saved_view
