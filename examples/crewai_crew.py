"""A CrewAI crew whose task reads the customer's context."""

from crewai import Agent, Crew, Task

from niadra import Niadra, phone
from niadra.integrations.crewai import NiadraCrew

niadra = Niadra(channel="chat")
memory = NiadraCrew(niadra.conversation("thread-81", subject=phone("+5511912345678")))
support = Agent(role="Support", goal="Help the customer", backstory="You work for Acme.", tools=memory.tools)
answer = Task(
    description="Answer the customer: {message}\n\nWhat the company knows:\n{niadra_context}",
    expected_output="A short, specific answer.",
    agent=support,
)
crew = Crew(
    agents=[support],
    tasks=[answer],
    before_kickoff_callbacks=[memory.before_kickoff],
    after_kickoff_callbacks=[memory.after_kickoff],
)
print(crew.kickoff(inputs={"message": "Where is my replacement lid?"}).raw)
