"""A Pipecat phone bot over Twilio Media Streams with the customer's memory."""

from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

from niadra import AsyncNiadra
from niadra.integrations.pipecat import NiadraMemoryProcessor, conversation_for_call, history_tools

niadra = AsyncNiadra(channel="voice")


def build(transport, stt, llm, tts, call_sid: str, caller: str, stir_verstat: str | None) -> Pipeline:
    conversation = conversation_for_call(niadra, call_sid, caller)
    memory = NiadraMemoryProcessor(conversation, attestation=stir_verstat)
    context = LLMContext(
        [{"role": "system", "content": "You are Acme's agent."}], tools=history_tools(conversation)
    )
    aggregators = LLMContextAggregatorPair(context)
    memory.observe(aggregators)
    user, assistant = aggregators.user(), aggregators.assistant()
    return Pipeline([transport.input(), stt, user, memory, llm, tts, transport.output(), assistant])
