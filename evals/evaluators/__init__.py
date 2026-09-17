"""
Evaluators for Phase 1A LangSmith evaluation runs.

Each evaluator receives a run (agent execution) and an example (golden pair)
and returns a score between 0 and 1.
"""
from __future__ import annotations

from langsmith.schemas import Example, Run


def intent_accuracy_evaluator(run: Run, example: Example) -> dict:
    """
    Check if the classified intent matches the expected intent.

    Returns score 1.0 (correct) or 0.0 (incorrect).
    """
    expected_intent = (example.outputs or {}).get("expected_intent", "")
    if not expected_intent:
        return {"key": "intent_accuracy", "score": None, "comment": "no expected_intent in example"}

    # Extract intent from run output
    outputs = run.outputs or {}
    actual_intent = outputs.get("intent", "")

    score = 1.0 if actual_intent == expected_intent else 0.0
    return {
        "key": "intent_accuracy",
        "score": score,
        "comment": f"expected={expected_intent}, actual={actual_intent}",
    }


def card_type_evaluator(run: Run, example: Example) -> dict:
    """
    Check if the returned card type matches the expected card type.

    Returns score 1.0 (correct) or 0.0 (incorrect).
    """
    expected_card_type = (example.outputs or {}).get("expected_card_type", "")
    if not expected_card_type:
        return {"key": "card_type", "score": None, "comment": "no expected_card_type in example"}

    outputs = run.outputs or {}
    actual_card_type = outputs.get("card_type", "")

    score = 1.0 if actual_card_type == expected_card_type else 0.0
    return {
        "key": "card_type",
        "score": score,
        "comment": f"expected={expected_card_type}, actual={actual_card_type}",
    }


def hallucination_evaluator(run: Run, example: Example) -> dict:
    """
    Check for numeric hallucination in the LLM commentary.

    Compares numbers mentioned in the commentary against verified card data.
    Returns score 1.0 (no hallucination) or 0.0 (hallucination detected).

    Phase 1A commentary is 1-2 sentences — hallucination risk is low but
    price/area claims must be verified against the card data.
    """
    import re

    outputs = run.outputs or {}
    commentary: str = outputs.get("commentary", "")
    card_data: dict = outputs.get("card_data", {})

    if not commentary or not card_data:
        return {"key": "hallucination", "score": None, "comment": "insufficient data"}

    # Extract all numbers from commentary
    commentary_numbers = set(re.findall(r"\b\d[\d,\.]*\b", commentary))
    if not commentary_numbers:
        return {"key": "hallucination", "score": 1.0, "comment": "no numbers in commentary"}

    # Collect all numbers present in verified card data
    card_text = str(card_data)
    verified_numbers = set(re.findall(r"\b\d[\d,\.]*\b", card_text))

    # Any number in commentary that is not in verified card data is a hallucination signal
    unverified = commentary_numbers - verified_numbers
    if unverified:
        return {
            "key": "hallucination",
            "score": 0.0,
            "comment": f"Unverified numbers in commentary: {unverified}",
        }

    return {"key": "hallucination", "score": 1.0, "comment": "all numbers verified"}
