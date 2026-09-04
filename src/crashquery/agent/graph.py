"""Agent construction.

Uses `langchain.agents.create_agent` (the LangChain 1.x API — the older
`initialize_agent` and `create_sql_agent` helpers are gone). It returns a
compiled LangGraph graph, so you get streaming, checkpointing and interrupts
without writing the graph by hand.

Deliberately NOT using the built-in SQLDatabaseToolkit: it dumps the schema
into the prompt and executes SQL with no cost gate or code-lookup step, which
is precisely the behaviour this project exists to improve on.
"""

from __future__ import annotations

import os
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

from crashquery.agent.prompts import SYSTEM_PROMPT
from crashquery.agent.tools import ALL_TOOLS
from crashquery.settings import get_settings


def _require_api_key(model: str) -> None:
    provider = model.split(":", 1)[0].lower()
    if provider.startswith("anthropic") and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    if provider.startswith("openai") and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key "
            "(poetry install --extras openai)."
        )


def build_agent(model: str | None = None, checkpointer: Any = None):
    """Create the SQL analyst agent.

    Args:
        model: Provider-prefixed model id, e.g. 'anthropic:claude-sonnet-4-5'
            or 'openai:gpt-4o'. Needs the matching API key in the environment.
        checkpointer: Optional LangGraph checkpointer for multi-turn memory.
    """
    resolved = model or get_settings().model
    _require_api_key(resolved)
    return create_agent(
        model=resolved,
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
        name="road_safety_analyst",
    )


def ask(agent, question: str, thread_id: str = "default") -> dict[str, Any]:
    """Run one question and return the answer plus a tool-call trace.

    The trace is the interesting part for evaluation: it shows whether the
    agent looked up codes before filtering, which is the behaviour that
    separates a correct answer from a lucky one.
    """
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": get_settings().max_iterations,
    }

    result = agent.invoke({"messages": [HumanMessage(content=question)]}, config)

    messages = result["messages"]
    trace: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            for call in message.tool_calls:
                trace.append({"tool": call["name"], "args": call["args"]})

    final = ""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content:
            final = (
                message.content
                if isinstance(message.content, str)
                else "".join(
                    block.get("text", "")
                    for block in message.content
                    if isinstance(block, dict)
                )
            )
            break

    return {
        "question": question,
        "answer": final,
        "trace": trace,
        "tool_calls": len(trace),
        "sql_executed": [
            call["args"].get("query", "") for call in trace if call["tool"] == "run_sql"
        ],
        "consulted_codes": any(call["tool"] == "lookup_codes" for call in trace),
    }
