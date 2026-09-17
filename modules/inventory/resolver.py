"""
Fuzzy project-name resolver.

Given a raw user string (potentially misspelled, abbreviated, or partial),
resolves it to a single ProjectRecord — or returns enough information for
the node to ask the user for clarification.

Matching strategy (in order):
1. Exact substring match (case-insensitive) — fast path.
2. rapidfuzz WRatio score ≥ FUZZY_THRESHOLD against all ProjectNames.
   WRatio combines token_sort_ratio, partial_ratio, and Levenshtein in one
   call, handling transpositions, missing words, and word-order differences.
3. If rapidfuzz is not installed, falls back to difflib.SequenceMatcher.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from modules.inventory.project_cache import ProjectCache, ProjectRecord, get_project_cache

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 80  # 0-100; WRatio score required for a fuzzy match


class ResolveStatus(str, Enum):
    RESOLVED = "resolved"       # exactly one match found
    AMBIGUOUS = "ambiguous"     # 2+ plausible matches
    NOT_FOUND = "not_found"     # zero matches above threshold


@dataclass
class ResolveResult:
    status: ResolveStatus
    project_id: Optional[str] = None        # set only when RESOLVED
    project_name: Optional[str] = None      # set only when RESOLVED
    candidates: list[ProjectRecord] = field(default_factory=list)  # set when AMBIGUOUS


def _fuzzy_score(a: str, b: str) -> float:
    """
    Return a 0-100 similarity score between two lowercase strings.
    Prefers rapidfuzz; falls back to difflib.SequenceMatcher.
    """
    try:
        from rapidfuzz import fuzz
        return fuzz.WRatio(a, b)
    except ImportError:
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio() * 100


async def resolve_project_name(
    name: str,
    cache: Optional[ProjectCache] = None,
) -> ResolveResult:
    """
    Resolve a raw user-supplied project name to a ProjectRecord.

    Args:
        name:  Raw string from the user (e.g. "Grenz", "greenz", "Greenz by danube").
        cache: ProjectCache instance; uses the module singleton if None.

    Returns:
        ResolveResult with status RESOLVED, AMBIGUOUS, or NOT_FOUND.
    """
    if cache is None:
        cache = get_project_cache()

    projects = await cache.get_all()
    if not projects:
        logger.warning("resolve_project_name: project cache is empty")
        return ResolveResult(status=ResolveStatus.NOT_FOUND, candidates=[])

    query = name.strip().lower()

    # ── 1. Substring match ────────────────────────────────────────────────
    substring_hits = [
        p for p in projects
        if query in p.project_name.lower() or p.project_name.lower() in query
    ]
    if len(substring_hits) == 1:
        p = substring_hits[0]
        logger.info(
            "project_resolved_substring",
            extra={"query": name, "matched": p.project_name},
        )
        return ResolveResult(
            status=ResolveStatus.RESOLVED,
            project_id=p.project_id,
            project_name=p.project_name,
            candidates=[p],
        )
    if len(substring_hits) > 1:
        logger.info(
            "project_ambiguous_substring",
            extra={"query": name, "candidates": [p.project_name for p in substring_hits]},
        )
        return ResolveResult(status=ResolveStatus.AMBIGUOUS, candidates=substring_hits)

    # ── 2. Fuzzy match ────────────────────────────────────────────────────
    scored = [
        (p, _fuzzy_score(query, p.project_name.lower()))
        for p in projects
    ]
    above = [(p, score) for p, score in scored if score >= FUZZY_THRESHOLD]
    above.sort(key=lambda x: x[1], reverse=True)

    if not above:
        logger.info(
            "project_not_found",
            extra={"query": name, "best_score": max(s for _, s in scored) if scored else 0},
        )
        return ResolveResult(status=ResolveStatus.NOT_FOUND, candidates=[])

    if len(above) == 1:
        p, score = above[0]
        logger.info(
            "project_resolved_fuzzy",
            extra={"query": name, "matched": p.project_name, "score": score},
        )
        return ResolveResult(
            status=ResolveStatus.RESOLVED,
            project_id=p.project_id,
            project_name=p.project_name,
            candidates=[p],
        )

    # Multiple fuzzy matches — return top 3 as candidates
    candidates = [p for p, _ in above[:3]]
    logger.info(
        "project_ambiguous_fuzzy",
        extra={"query": name, "candidates": [p.project_name for p in candidates]},
    )
    return ResolveResult(status=ResolveStatus.AMBIGUOUS, candidates=candidates)
