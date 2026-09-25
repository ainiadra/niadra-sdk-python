# Niadra for Dify

The customer's memory, shared by every agent of the company, in a Dify app: what the company
knows about the customer before the model answers, the customer's whole history as tools, and
every turn recorded for the other agents.

## Tools

| Tool | What it does | Who fills it |
|---|---|---|
| Get context | Records the customer's message and returns the context for the prompt: the agent's own notes (with Agent memory on), the customer's pinned pack and what changed on other channels | the app |
| Record reply | Records the agent's reply | the app |
| Search customer history | The customer's whole history, by question and period | the model |
| Customer timeline | The customer's conversations and agent actions, newest first | the model |
| Open history item | One conversation or business object from a search or the timeline | the model |
| Search agent memory | The agent's own working notes: procedures, tools, pitfalls; nothing about customers | the model |

The customer (id type and id), the conversation id and the channel are set by the app builder,
usually from the app's variables (`sys.user_id`, `sys.conversation_id`), and never by the model.

## Set up

1. Install the plugin and add the provider with a Niadra source key (from the Niadra Console).
2. In a chatflow: a Get context node before the LLM node (customer message = `sys.query`), its
   text in the LLM's system prompt after your instructions, and a Record reply node after the LLM.
3. In an agent: add the history tools; their descriptions are the same the model gets from every
   Niadra SDK.

Niadra slow or down never fails a tool: the context comes out empty and a history tool answers
that the history is unavailable. Documentation: https://docs.niadra.com/en.

## Develop

`pip install dify_plugin niadra`, then `dify plugin package ./dify` to build the `.difypkg`. The
tests run the plugin with Dify's own plugin SDK and the Niadra emulator:
`uv run --with "dify-plugin==0.10.2" pytest integrations-extras/dify` from the SDK's root.
