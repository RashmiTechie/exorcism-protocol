# Exorcism Protocol — starter code

A supervised agent swarm contains a paranormal outbreak in a simulated facility, and is
scored live against a single-agent baseline on the same seeded outbreak.

Everything here runs **without any API keys**: the agents are rule-based, and that rule layer
stays as the fallback once you put LLMs behind the roles.

## Run it

```bash
pip install -r requirements.txt

python runner.py --seed 42          # one outbreak, swarm vs baseline
python runner.py --seeds 30         # averaged over 30 outbreaks

uvicorn app:app --port 8000         # live split-screen dashboard
# open http://localhost:8000 and press Run   (TICK_SECONDS=0.5 to speed it up)
```

Result from the rule-based agents, averaged over 30 seeds, 90 ticks:

| metric | swarm | baseline |
| --- | --- | --- |
| containment speed (ticks, lower is better) | 5.3 | 11.9 |
| false-alarm rate | 0% | 84% |
| civilians lost | 0.3 | 4.4 |

## Files

| file | what it does |
| --- | --- |
| `sim.py` | Facility: 20 rooms, civilians, 5 anomaly types, noisy sensors, actions, scoring. No AI. |
| `events.py` | The blackboard: one event envelope, in-memory log, optional DynamoDB sink, and `SCOPES` — what each role may see. |
| `agents.py` | Sentinels (East/West), Investigator, Researcher, Commander, Evacuation, Supervisor, plus the Baseline. |
| `youcom.py` | Playbook lookup through the You.com Search API, cached per anomaly type, offline fallback. |
| `crew_agents.py` | Template: CrewAI + Parasail behind the Supervisor, with timeout fallback to the rules. |
| `runner.py` | Headless runs and the swarm-vs-baseline table. |
| `app.py`, `static/index.html` | Live dashboard (Starlette, server-sent events). |

## Design rules worth keeping

- **Scopes live in code, not prompts.** `events.SCOPES` is the only read path each agent has.
  That is the "partial individual knowledge" the judges asked for.
- **One `correlation_id` per incident.** `log.trace(run_id, "inc_001")` gives the whole story
  from first sighting to containment.
- **The sim never waits for a model.** Agents act after each tick; slow or failed LLM calls fall
  back to the rule.
- **Same seed, same noise.** Sensor noise is seeded per (seed, tick, room), so both runs face
  identical readings.

## Hackathon-day wiring

1. **You.com:** `export YDC_API_KEY=...` — the researcher's playbooks then come with live sources
   (check `PLAYBOOK_FOUND` events, `live: true`). Confirm the endpoint/params with the You.com team.
2. **Parasail + CrewAI:** `pip install crewai`, set `PARASAIL_API_KEY`, `PARASAIL_BASE_URL`,
   `PARASAIL_MODEL`, then in `agents.Swarm` swap `Supervisor` for `crew_agents.LLMSupervisor`.
   Do the same pattern for the Commander next. `crew_agents.py` is untested against the real
   APIs — check names with the CrewAI and Parasail engineers.
3. **Bedrock instead of Parasail:** `pip install "crewai[bedrock]"`, run `aws configure`, enable
   the model in the Bedrock console, then `export LLM_PROVIDER=bedrock BEDROCK_MODEL_ID=<id from console>`
   and test with `python check_llm.py`. On Fargate, give the task role `bedrock:InvokeModel`.
4. **AWS:**
   ```bash
   aws dynamodb create-table --table-name exorcism-events \
     --attribute-definitions AttributeName=run_id,AttributeType=S AttributeName=sk,AttributeType=S \
     --key-schema AttributeName=run_id,KeyType=HASH AttributeName=sk,KeyType=RANGE \
     --billing-mode PAY_PER_REQUEST
   docker build -t exorcism . && docker run -p 8000:8000 -e DDB_TABLE=exorcism-events exorcism
   ```
   Push the image to ECR and run it as one ECS Fargate service behind an ALB (or on one EC2
   instance). Give the task role `dynamodb:PutItem` on the table.

## Tuning knobs (top of `sim.py`)

`SPREAD_EVERY`, `OVERRUN_AFTER`, `FALSE_POSITIVE_RATE`, `TEAMS`, `CONTAIN_TICKS`. Raise
`FALSE_POSITIVE_RATE` to make the baseline's false alarms more dramatic on stage.
