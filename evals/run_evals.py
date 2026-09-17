"""
Phase 1A evaluation runner.

Runs golden eval pairs from LangSmith against the live agent and scores them
with the evaluators defined in evals/evaluators/.

Exit code 0 = all evaluators above threshold.
Exit code 1 = one or more evaluators below threshold (blocks CI merge).

Usage:
  python evals/run_evals.py
"""
from __future__ import annotations

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)

# Thresholds — a run scoring below these blocks CI
_THRESHOLDS = {
    "intent_accuracy": 0.90,
    "card_type": 0.90,
    "hallucination": 0.95,
}


async def run_agent_for_eval(inputs: dict) -> dict:
    """
    Run the agent on a single eval input and return output dict.

    Called by langsmith.evaluation.evaluate() for each example.
    """
    from agent.graph import get_graph
    from agent.state import AgentState
    from core.auth import UserContext

    query = inputs.get("query", "")
    session_id = f"eval-{hash(query) % 100000}"

    # Synthetic user for eval runs
    user = UserContext(
        broker_id="eval-broker",
        tenant_id="danube",
        role="broker",
        session_id=session_id,
        display_name="Eval Broker",
        email="eval@danube.ae",
        org_id="danube-org",
    )

    initial_state: AgentState = {
        "query": query,
        "user": user,
        "session_id": session_id,
        "tenant_id": "danube",
        "messages": [],
        "session_history": [],
        "intent": None,
        "entities": None,
        "confidence": None,
        "path": None,
        "clarification": None,
        "card_html": None,
        "card_type": None,
        "commentary": None,
        "chips": None,
        "genie_conv_id": None,
        "filter_state": None,
    }

    graph = await get_graph()
    config = {"configurable": {"thread_id": session_id}}

    final_state: dict = {}
    async for event in graph.astream_events(initial_state, config=config, version="v2"):
        if event["event"] == "on_chain_end" and event.get("name") == "LangGraph":
            output = event.get("data", {}).get("output", {})
            final_state = output if isinstance(output, dict) else {}

    return {
        "intent": final_state.get("intent", ""),
        "card_type": final_state.get("card_type", ""),
        "commentary": final_state.get("commentary", ""),
        "card_data": {},  # populated by card_builder in real runs
    }


def run_phase1a_evals() -> bool:
    """
    Run all Phase 1A evaluators against the LangSmith golden dataset.

    Returns True if all thresholds pass, False otherwise.
    """
    from core.settings import settings

    if not settings.langsmith_api_key:
        logger.error("eval_skipped", extra={"reason": "LANGSMITH_API_KEY not set"})
        return False

    from langsmith import Client
    from langsmith.evaluation import evaluate
    from evals.evaluators import (
        card_type_evaluator,
        hallucination_evaluator,
        intent_accuracy_evaluator,
    )

    client = Client(api_key=settings.langsmith_api_key)

    def _sync_run_agent(inputs: dict) -> dict:
        return asyncio.run(run_agent_for_eval(inputs))

    results = evaluate(
        target=_sync_run_agent,
        data=settings.langsmith_dataset,
        evaluators=[
            intent_accuracy_evaluator,
            card_type_evaluator,
            hallucination_evaluator,
        ],
        experiment_prefix="phase1a",
        metadata={"phase": "1A", "env": settings.environment.value},
        client=client,
    )

    # Check thresholds
    passed = True
    for key, threshold in _THRESHOLDS.items():
        score = results.get(key, {}).get("score")
        if score is None:
            logger.warning("eval_score_missing", extra={"key": key})
            continue
        if score < threshold:
            logger.error(
                "eval_threshold_failed",
                extra={"key": key, "score": score, "threshold": threshold},
            )
            passed = False
        else:
            logger.info(
                "eval_threshold_passed",
                extra={"key": key, "score": score, "threshold": threshold},
            )

    return passed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    success = run_phase1a_evals()
    sys.exit(0 if success else 1)
