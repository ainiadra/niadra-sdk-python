"""A LiveKit voice agent that starts every call knowing the caller. Run: python livekit_agent.py dev"""

from livekit.agents import AgentServer, AgentSession, JobContext, cli, inference

from niadra import AsyncNiadra
from niadra.integrations.livekit import NiadraAgent, conversation_for

niadra = AsyncNiadra(channel="voice")
server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    caller = await ctx.wait_for_participant()
    conversation = conversation_for(niadra, caller, room=ctx.room)  # the SIP number and call id
    session = AgentSession(
        stt=inference.STT("deepgram/nova-3"),
        llm=inference.LLM("openai/gpt-4.1-mini"),
        tts=inference.TTS("cartesia/sonic-2"),
    )
    agent = NiadraAgent(
        conversation, instructions="You are Acme's support agent. Be brief.", agent_memory=True
    )
    await session.start(agent, room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
