"""Exercise the Lane 3 tool-call contract against the configured chat endpoint.

Start tools/mock_models.py on port 9000, then set
LLM_URL=http://127.0.0.1:9000/v1 before running this script. The same script
can target the GX10 by changing LLM_URL and LLM_MODEL.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import models


TOOLS = [{
    "type": "function",
    "function": {
        "name": "search_screens",
        "description": "Search remembered screenshots.",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    },
}]
# Nemotron 3 33B ignores a one-line "use tools" instruction and answers
# "I don't have access to real-time data" instead (0/5 tool calls in testing).
# Ollama also ignores tool_choice="required". A prompt that tells it what the
# memory is and to search FIRST gets a tool call every time (3/3). Lane 3's real
# agent prompt must be at least this explicit.
SYSTEM = (
    "You are Dockt, a private assistant with a searchable memory of everything the user "
    "has seen on their computer screen today (Slack, email, calendar, docs, browser). "
    "You can NOT answer from general knowledge. For every question, FIRST call "
    "search_screens with a short keyword query, then answer only from the results, "
    "citing each fact with its id like [#9]. If nothing relevant is found, say so."
)
QUESTION = "Where is dinner tonight?"
TOOL_RESULT = "[#9] Sat 13:12 · Slack — #hackathon: Sam: dinner at Nopalito at 8, I booked for 4"


def messages_after_tool(messages, calls):
    raw_calls = [{
        "id": call["id"],
        "type": "function",
        "function": {
            "name": call["name"],
            "arguments": json.dumps(call["arguments"]),
        },
    } for call in calls]
    messages.append({"role": "assistant", "content": "", "tool_calls": raw_calls})
    for call in calls:
        messages.append({
            "role": "tool",
            "tool_call_id": call["id"],
            "content": TOOL_RESULT,
        })


async def non_stream_flow():
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUESTION},
    ]
    first = await models.chat_messages(messages, tools=TOOLS)
    print("non-stream tool calls:", first["tool_calls"])
    if first["tool_calls"]:
        messages_after_tool(messages, first["tool_calls"])
        answer = await models.chat_messages(messages, tools=TOOLS)
        print("non-stream answer:", answer["content"])


async def stream_flow():
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUESTION},
    ]
    first_stream = await models.chat_messages(messages, tools=TOOLS, stream=True)
    first_events = [event async for event in first_stream]
    calls = next((event["tool_calls"] for event in reversed(first_events)
                  if event["type"] == "done"), [])
    print("stream tool calls:", calls)
    if not calls:
        return

    messages_after_tool(messages, calls)
    stream = await models.chat_messages(messages, tools=TOOLS, stream=True)
    print("stream answer:", end=" ", flush=True)
    async for event in stream:
        if event["type"] == "token":
            print(event["text"], end="", flush=True)
        elif event["type"] == "done":
            print()


async def main():
    print(f"LLM endpoint: {models.LLM_URL} ({models.LLM_MODEL})")
    await non_stream_flow()
    await stream_flow()


if __name__ == "__main__":
    asyncio.run(main())
