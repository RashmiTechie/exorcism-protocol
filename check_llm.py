"""Quick check that your LLM provider works before wiring it into the swarm.

    LLM_PROVIDER=bedrock python check_llm.py
"""
from crewai import Agent, Crew, Task

from crew_agents import llm

boss = Agent(role="Supervisor", goal="Make safe containment decisions",
             backstory="You protect civilians in a haunted building.", llm=llm)
task = Task(description="Room E8 has a banshee and 2 people evacuating. Seal the door now "
                        "or wait? Answer in one sentence.",
            expected_output="One sentence decision", agent=boss)
print(Crew(agents=[boss], tasks=[task]).kickoff())
