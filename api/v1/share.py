"""
POST /api/v1/feedback — broker rates a response; annotates the LangSmith trace.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from core.auth import UserContext
from core.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_user(request: Request) -> UserContext:
    return request.state.user


class FeedbackRequest(BaseModel):
    run_id: str = Field(..., min_length=1)
    score: int = Field(..., ge=0, le=1)   # 1 = thumbs up, 0 = thumbs down
    comment: str = Field(default="", max_length=2000)
    corrected_output: str | None = Field(default=None, max_length=10000)


class FeedbackResponse(BaseModel):
    accepted: bool


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    body: FeedbackRequest,
    request: Request,
    user: UserContext = Depends(_get_user),
) -> FeedbackResponse:
    """
    Submit broker feedback on an LLM response.

    Annotates the LangSmith trace identified by run_id.
    If corrected_output is provided for a thumbs-down, adds a golden pair
    to the eval dataset.
    """
    if not settings.langsmith_api_key:
        logger.info("feedback_skipped_no_langsmith", extra={"run_id": body.run_id})
        return FeedbackResponse(accepted=True)

    from langsmith import Client

    client = Client(api_key=settings.langsmith_api_key)

    try:
        client.create_feedback(
            run_id=body.run_id,
            key="broker_rating",
            score=body.score,
            comment=body.comment or None,
        )

        if body.corrected_output and body.score == 0:
            # Add corrected pair to the golden eval dataset
            client.create_example(
                inputs={"query": body.comment},
                outputs={"response": body.corrected_output},
                dataset_name=settings.langsmith_dataset,
            )
            logger.info(
                "golden_pair_added",
                extra={"run_id": body.run_id, "broker_id": user.broker_id},
            )

    except Exception as exc:
        logger.error(
            "feedback_annotation_failed",
            extra={"run_id": body.run_id, "error": str(exc)},
        )
        # Do not surface LangSmith errors to the broker
        return FeedbackResponse(accepted=False)

    logger.info(
        "feedback_recorded",
        extra={
            "run_id": body.run_id,
            "score": body.score,
            "broker_id": user.broker_id,
        },
    )
    return FeedbackResponse(accepted=True)
