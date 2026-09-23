# Niadra Python SDK

[![PyPI](https://img.shields.io/pypi/v/niadra)](https://pypi.org/project/niadra/)
[![Python](https://img.shields.io/pypi/pyversions/niadra)](https://pypi.org/project/niadra/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

**Niadra is the shared customer memory for every AI agent in a company.** The WhatsApp agent, the
voice agent, the billing agent and the human team read the same memory before they act and write
back what they said and did. This package connects a Python agent to it.

[Website](https://niadra.com/en) · [Documentation](https://docs.niadra.com/en) ·
[Talk to us](https://niadra.com/en/enterprise) · [TypeScript SDK](https://github.com/ainiadra/niadra-sdk-ts)

```
pip install niadra
```

## The problem it solves

A customer tells your WhatsApp agent that order 4471 arrived with a broken lid and that she needs a
replacement by Friday. An hour later she calls. Without shared memory, the voice agent asks her to
explain everything again, and nobody remembers the Friday promise. With Niadra, the voice agent
starts the call knowing about the open replacement and its deadline, and when the billing agent
credits her invoice, the other agents see it within seconds.

Niadra does the remembering for you:

- it turns conversations and system events into facts, open items and promises, each with the
  turns that prove it;
- it ties them to the right person across phone numbers, e-mails, WhatsApp ids and CRM ids, and to
  the companies and partners that person acts for;
- it compiles a short context for each agent, holding back what the customer's verification level
  does not allow, and records a receipt of every read.

Your agents keep their own models, prompts and vendors. Niadra is the memory layer they share.

## Quickstart

```python
from niadra import Niadra, phone

niadra = Niadra(channel="whatsapp")  # reads NIADRA_API_KEY
customer = phone("+5511912345678")

with niadra.conversation("thread-81", subject=customer) as conversation:
    conversation.customer(incoming_text)  # what the customer wrote
    context = conversation.context()  # what this agent needs to know now
    prompt = instructions + "\n\n" + context.system_block + "\n\n" + context.turn_block
    conversation.mark_injected(context)
    reply = your_model(prompt)
    conversation.agent(reply)  # what the agent answered
```

Three moments cover most agents: read the context before the model call, record the turns after
it, and record an `action()` when the agent does something in a system (a refund, a new delivery
date). `wrap()` does the reading and recording for you around an OpenAI-compatible client.

Requires Python 3.10+. Depends on `httpx` and `pydantic` only.

## What your agent gets

| Call | What it returns |
|---|---|
| `context()` | a few lines about this customer, compiled from every channel and agent, pinned for the conversation |
| `search()`, `timeline()`, `open()` | the full history on demand, with how often a problem happened before |
| `tools()` | the same history as function-calling tools bound to one customer, for any model provider |
| `subject_token()` | a token that binds an MCP connection to one customer |
| `track()`, `action()` | messages, system events and actions, sent in the background, never blocking the agent |
| `identify()`, `verify()` | which ids belong to the same person, and what the conversation proved about who is there |
| `object_state()`, `object_timeline()` | a business object (an order, an invoice, a ticket) as the systems of record reported it |
| `feedback()` | a correction of what Niadra derived, audited like any other event |

## Questions people ask

**How do I give my AI agent memory of past conversations on other channels?** Record the turns
with `track()` or `conversation()` in every agent, and read `context()` before each model call.
Niadra ties the turns to the person, whichever id each channel uses.

**How is this different from keeping chat history in my database or in a vector store?** Stored
history is raw text for one channel. Niadra keeps derived facts and open items with evidence,
resolves identity across channels and systems, closes items when a system of record confirms an
action, and filters what each agent may read by the verification level of the conversation.

**What happens if Niadra is slow or down?** The agent keeps answering without the memory. Every
call has its own time budget (150 ms for voice context, 300 ms otherwise) and returns an empty
value instead of raising, unless you ask for `strict=True`.

**What about privacy and LGPD or GDPR?** Items carry a verification level and a purpose, and the
policy decides what each agent sees. Every read leaves a receipt, and a person can be erased or
exported on request. The SDK never logs handles or message text.

**Which models and frameworks does it work with?** Any. The context is text you place in your
prompt, the tools follow the common function-calling format (and the Anthropic one), and
`wrap()` covers OpenAI-compatible clients.

## The client

`Niadra` is synchronous and thread-safe; `AsyncNiadra` has the same methods, awaited. One instance
per process is enough. The key names its region and space, and the SDK derives the address from it:
`nia_sk_live_br1_acme_...` talks to `https://acme.br1.api.niadra.com`. Pass `base_url=` (or set
`NIADRA_BASE_URL`) to point it elsewhere, such as the local emulator.

## Context

`context()` returns the context pack for a person, an organization or a business object: the few
lines that matter now, compiled by Niadra from every channel and every agent.

```python
context = niadra.context(
    phone("+5511912345678"),
    view="voice",  # chat, voice, brief, full, account, partner, task:<name>
    verification="V1",  # what you have proven about who is on the line
    conversation_id=call_id,
    target="openai/gpt-4.1",  # lets Niadra aim the pack at the model's prompt-cache floor
)
context.system_block  # the pinned pack: place it after your own instructions
context.turn_block  # live turns from other channels and the delta: place it at the end
context.withheld  # items the policy held back at this verification level
```

Pass `object="invoice:erp:0823"` instead of a subject to center the pack on a business object, and
`about=` to add what the organization the person acts for has that matters here.

With a `conversation_id` (or `task_id`) the server pins the pack to the same bytes on every
turn. `delta=True` asks for what changed since this agent last read the subject; the server sends
each change once, and `conversation()` keeps them for you (below).

### The context cache

Inside a conversation or task, packs are cached in memory:

- younger than 10 s: returned without a request;
- up to 10 minutes older: returned at once while one background request refreshes it;
- when a request fails: the last good pack, if it is less than 30 minutes old;
- at most 1,000 packs, the least recently used evicted first.

Refreshes send the cached ETag, so an unchanged pack costs a `not_modified` answer instead of the
full text, and only one background refresh per pack runs at a time. A 401 or 403 is not an
outage: the cached packs go (all of them on 401, the one requested on 403), so cutting a vendor's
access also cuts what it had cached. A plain read and a `delta` read of one conversation share one
entry, and each delta is handed out once.

Configure it with `CacheOptions(ttl, stale_while_revalidate, max_stale, max_entries)`, in
seconds, or turn it off with `CacheOptions(enabled=False)`.

## Search, timeline and open

For what does not fit in the pack, the whole history is one call away.

```python
found = niadra.search(customer, "technician visit", conversation_id=call_id)
found.recurrence  # occurrences=3, window_days=90: "this happened before"
page = niadra.timeline(customer, limit=20)  # most recent first; pass page.next_cursor to go on
item = niadra.open(found.items[0].id)  # what was asked, promised and by whom, the outcome
```

## Track

`track()` records a message, a system event or an action. It never blocks: items go to a bounded
local queue and a background thread (a task, with `AsyncNiadra`) sends them in batches of 15 or
every second, whichever comes first, with three attempts and backoff.

```python
niadra.track(
    {
        "conversation_id": thread_id,
        "handles": [phone("+5511912345678")],
        "speaker": {"role": "customer"},
        "content": {"text": "I was charged twice"},
    }
)
```

Items take their `idempotency_key` from you (use the provider's message id when there is one) or
from a UUIDv7 minted by the SDK, so a retried batch never duplicates anything. Call `flush()` to send
what is queued now; `close()` and the exit hook flush for you.

## Identity and verification

```python
niadra.identify([phone("+5511912345678"), email("marina@example.com")])
niadra.verify("otp_whatsapp", "V2", handle=phone("+5511912345678"), conversation_id=thread_id)
```

`identify()` states that handles belong to the same person. `verify()` raises the verification
level of one conversation after you proved it; Niadra never infers it. Both are sent at once rather
than queued, so the next `context()` sees them, and `verify()` drops the cached pack of that
conversation.

## Actions

```python
niadra.action(
    "credit",
    subject=customer,
    object="invoice:erp:0823",
    result="R$ 40",
    closes={"object": "invoice:erp:0823", "operation": "credit"},
)
```

An action is what an agent did in a system of record. `closes` names the open item or promise it
fulfils. It shows in every other agent's pack as declared until the system of record confirms it.

## Conversations and tasks

```python
with niadra.conversation(thread_id, subject=customer, view="chat") as conversation:
    conversation.customer(incoming_text)
    context = conversation.context()  # pinned, cached, with every delta since the pin
    conversation.mark_injected(context)  # when the pack went into the prompt
    conversation.agent(reply_text)  # carries the context stamp
    conversation.verify("otp_whatsapp", "V2")
    conversation.handoff("human", reason="asked for a person")
# conversation.ended is emitted on exit, even if the block raised
```

The first `context()` of a conversation gets the pack the server pins for it. Later reads also
ask for the delta, and the conversation keeps every delta it receives, in order, in
`context.delta`, so `turn_block` carries all the changes since the pin, followed by the live
turns. When the server pins a new pack, after `verify()` for instance, the kept deltas are
dropped: the new pack already has them. A read with `query=` is a one-off and leaves them alone.

Call `mark_injected()` each time you put the pack in a prompt. The agent's turns and actions that
follow carry it as `context_stamp`, with the pack's etag, which is how Niadra tells a context
that arrived after the agent spoke from one the agent had and did not use.
`context_injected_at` and `first_agent_turn_at` keep the first of each, for your own checks.

`task()` is the same for internal agents (billing, orders, tickets): it takes an `object` and a task
view such as `task:billing` (the default is `brief`), centers the pack on the object, and emits
`task.ended`.

### Wrapping the OpenAI client

```python
from openai import OpenAI
from niadra import wrap

openai = wrap(OpenAI())
with niadra.conversation(thread_id, subject=customer):
    openai.chat.completions.create(model="gpt-4.1", messages=messages)
```

Inside a conversation block, `chat.completions.create` and `chat.completions.parse`, sync or
async, streaming or not, get the pack after your leading system messages and the turn block at
the end. The injection is stamped, and the model's answer is recorded as the agent's turn: when
a stream ends or is closed, and, through `with_raw_response`, when you call `parse()`. Outside a
block, calls pass through untouched. Nothing the wrapper does can fail your call: a context it
cannot fetch is left out, and a failure to record the answer is logged, without content.

## Business objects

```python
invoice = niadra.object_state("invoice:erp:0823")  # state, as_of, open items
page = niadra.object_timeline("invoice:erp:0823", limit=20)
```

The state comes only from what the systems of record reported; an agent's action counts once a
system confirms it. The timeline lists system events and agent actions, newest first, never what
anyone said. Object ids are record ids, not personal data, so they go in the URL; an id with a
slash cannot be addressed that way.

## Feedback

```python
niadra.feedback("correct_fact", customer, fact_id=fact.id, value="prefers e-mail")
niadra.feedback("resolve_open_item", customer, open_item_id=item.id)
niadra.feedback("conversation_outcome", customer, conversation_id=thread_id, value="resolved")
```

A correction of what Niadra derived. It is sent at once and recorded as an event, so it is
audited like any other; `retract_fact` takes the fact out of every pack.

## Media

```python
upload = niadra.upload_media(recording, "audio/wav", subject=customer)
if upload:
    niadra.track(
        {
            "conversation_id": call_id,
            "handles": [customer],
            "speaker": {"role": "customer"},
            "content": {
                "type": "audio",
                "media_ref": upload.media_ref,
                "media_sha256": upload.media_sha256,
                "transcript": transcript,
            },
        }
    )
```

Media never travels inside an event. `upload_media()` reserves an upload, sends the bytes
straight to storage over a signed URL (HTTPS only, with exactly the headers the signature
covers and nothing else, so never your key), and returns the reference and digest for the event.
Storage checks the body against the declared size and digest. With `subject`, the file is stored under that person, so
erasing them erases it even if no event ever references it.

## Tools and MCP

`tools()` gives any model the history kit as function-calling tools, bound to one customer. The
customer is not a parameter the model can see or change, which is what stops a prompt injection
from switching customers.

```python
kit = niadra.tools(customer, conversation_id=thread_id)
response = openai.chat.completions.create(model="gpt-4.1", messages=messages, tools=kit.definitions)
for call in response.choices[0].message.tool_calls or []:
    result = kit.call(call.function.name, call.function.arguments)  # always a string for the model
```

Inside a conversation or task, `conversation.tools()` gives the same kit bound to it, at the
session's current verification level, including a level raised by `verify()` after the kit was
made. `kit.anthropic_definitions()` returns the same tools in the Anthropic shape. For agents that speak
MCP, mint a subject token from your backend and open the connection with it:

```python
token = niadra.subject_token(customer, conversation_id=thread_id, verification="V1")
# connect to niadra.mcp_url with the source key as Bearer and token.headers
```

## Failure behavior

Niadra must never take your agent down.

- **No key:** the client is a no-op and warns once.
- **Every public method** catches and logs its own failures and returns a safe value: an empty
  `Context` (check `context.error`), an empty result, `False` or `None`.
- **Own time budgets:** `context()` gives up after 150 ms for voice views and 300 ms otherwise;
  search, timeline, open and the object reads after 300 ms (voice) or 600 ms; writes sent at
  once after 5 s per attempt, and each attempt of a media transfer after 60 s. Adjust them with
  `Timeouts`.
- **Retries:** 5xx, 429 and network errors are retried with backoff; 421 (the space is moving
  between cells) is retried at once on a fresh connection; other 4xx are final.
- **Logs** carry method names, status codes, error codes and request ids, never handles or text.

Pass `strict=True` to raise instead, which is what you want in tests.

## Testing without the cloud

`niadra-mock` is a local, in-memory emulator of the same API. In tests, run the SDK against it
in-process:

```python
import httpx
from niadra import Niadra
from niadra_mock import MOCK_KEY, MockApp

mock = MockApp()
http = httpx.Client(
    transport=httpx.WSGITransport(app=mock.wsgi)
)  # ASGITransport(app=mock.asgi) for AsyncNiadra
niadra = Niadra(MOCK_KEY, base_url="http://mock", http_client=http, strict=True)
```

Recent turns become a small pack, deltas are sent once per change, search matches keywords,
verification only rises through `verify()`, objects take their state from system events, feedback
becomes a `feedback.*` event, and media uploads land in `mock.cell.media`. `mock.cell` also lets
you inspect events or inject failures (`fail_next`, `revoke`, `cut`, `put_in_holdout`). From a shell, `niadra-mock --port 8765` serves it over HTTP.

## Em português

A Niadra é a memória de clientes compartilhada por todos os agentes de IA de uma empresa: o agente
do WhatsApp, o de voz, o de cobrança e o time humano leem a mesma memória antes de agir e registram
o que disseram e fizeram. Este pacote conecta um agente em Python a essa memória: `context()` antes
de chamar o modelo, `track()` depois, e `action()` quando o agente faz algo num sistema.
Documentação em [docs.niadra.com](https://docs.niadra.com) e contato em
[niadra.com/enterprise](https://niadra.com/enterprise).

## License

Apache 2.0. See [LICENSE](LICENSE).
