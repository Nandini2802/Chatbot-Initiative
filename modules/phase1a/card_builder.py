"""
Deterministic card builder — NO LLM calls here.

Builds card data from PostgreSQL results and renders Jinja2 templates.
The LLM only generates the commentary that wraps the card (done in agent nodes).

Variable names in Python models must exactly match Jinja2 template variables.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.auth import UserContext
from core.errors import NotFoundError
from core.observability import tracer
from core.storage import get_signed_url
from modules.phase1a.asset_registry import AssetRow, VersionRow, asset_registry
from modules.phase1a.cards import (
    BrochureCard,
    FloorPlanCard,
    FloorPlanImage,
    GalleryCard,
    GalleryImage,
    LanguageEntry,
    LanguageListCard,
    ProjectOverviewCard,
    VersionCheckCard,
    VersionEntry,
    VideoCard,
)

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "cards"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)


def _render(template_name: str, context: dict) -> str:
    """Render a Jinja2 template. Never injects business logic into templates."""
    try:
        tmpl = _jinja_env.get_template(template_name)
        return tmpl.render(**context)
    except Exception as exc:
        logger.error(
            "template_render_failed",
            extra={"template": template_name, "error": str(exc)},
        )
        raise


# ── Public entry point used by retrieve_asset_node ────────────────────────────

async def build_card(
    intent: str,
    entities: dict,
    user: UserContext,
) -> tuple[object, str, str]:
    """
    Build a card for the given intent.

    Returns (card_model, card_type, rendered_html).
    Raises NotFoundError if no asset matches.
    """
    with tracer.start_as_current_span("card_builder.build_card") as span:
        span.set_attribute("intent", intent)
        span.set_attribute("broker_id", user.broker_id)
        span.set_attribute("tenant_id", user.tenant_id)

        project_name = entities.get("project_name", "")
        language = entities.get("language") or "en"
        unit_type = entities.get("unit_type")
        gallery_subfolder = entities.get("gallery_subfolder")  # e.g. "interiors", "exteriors"

        _intent_to_builder = {
            "get_brochure": _build_brochure,
            "get_floor_plan": _build_floor_plan,
            "get_gallery": _build_gallery,
            "get_video": _build_video,
            "get_spec_sheet": _build_brochure,      # spec sheet uses brochure card
            "get_project_overview": _build_project_overview,
        }

        builder_fn = _intent_to_builder.get(intent)
        if builder_fn is None:
            raise NotFoundError(f"No card builder for intent '{intent}'.")

        kwargs: dict = dict(project_name=project_name, language=language, unit_type=unit_type, user=user)
        if intent == "get_gallery":
            kwargs["gallery_subfolder"] = gallery_subfolder
        return await builder_fn(**kwargs)


# ── Individual card builders ───────────────────────────────────────────────────

async def _build_brochure(
    project_name: str,
    language: str,
    user: UserContext,
    unit_type: Optional[str] = None,
) -> tuple[BrochureCard, str, str]:
    row: AssetRow = await asset_registry.get_asset(
        project_name=project_name,
        asset_type="brochure",
        user=user,
        language=language,
    )
    signed_url = await get_signed_url(row.blob_path)

    card = BrochureCard(
        project=row.project_name,
        language=row.language,
        version_label=row.version_label,
        download_url=signed_url,
        file_size_bytes=None,
        content_type=row.content_type,
    )
    html = _render("brochure.html.j2", card.model_dump())
    return card, "brochure", html


async def _build_floor_plan(
    project_name: str,
    language: str,
    user: UserContext,
    unit_type: Optional[str] = None,
) -> tuple[FloorPlanCard, str, str]:
    rows: list[AssetRow] = await asset_registry.get_all_assets(
        project_name=project_name,
        asset_type="floor_plan",
        user=user,
        language=language,
    )
    total = len(rows)
    preview_rows = rows[:6]
    preview_images = []
    for i, row in enumerate(preview_rows):
        signed_url = await get_signed_url(row.blob_path)
        preview_images.append(FloorPlanImage(
            url=signed_url,
            file_name=row.blob_path.split("/")[-1],
            order=i,
        ))

    zip_url = await asset_registry.get_folder_zip_url(project_name, "floor-plans")

    card = FloorPlanCard(
        project=rows[0].project_name,
        language=language,
        preview_images=preview_images,
        total_images=total,
        zip_url=zip_url,
    )
    html = _render("floor_plan.html.j2", card.model_dump())
    return card, "floor_plan", html


# folder_key → human-readable label
_FOLDER_LABELS: dict[str, str] = {
    "interiors":  "Interiors",
    "exteriors":  "Exteriors",
    "amenities":  "Amenities",
    "renders":    "Renders",
}


async def _build_gallery(
    project_name: str,
    language: str,
    user: UserContext,
    unit_type: Optional[str] = None,
    gallery_subfolder: Optional[str] = None,
) -> tuple[GalleryCard, str, str]:
    rows: list[AssetRow] = await asset_registry.get_all_assets(
        project_name=project_name,
        asset_type="gallery",
        user=user,
        language=language,
        folder_key=gallery_subfolder,
    )
    total = len(rows)
    preview_rows = rows[:6]
    preview_images = []
    for i, row in enumerate(preview_rows):
        signed_url = await get_signed_url(row.blob_path)
        preview_images.append(GalleryImage(
            url=signed_url,
            caption=row.blob_path.split("/")[-1],
            order=i,
        ))

    # Determine which folder was actually used
    used_folder = gallery_subfolder or (rows[0].blob_path.split("/")[-2] if rows else "interiors")
    folder_label = _FOLDER_LABELS.get(used_folder, "Gallery")
    zip_url = await asset_registry.get_folder_zip_url(project_name, used_folder)

    card = GalleryCard(
        project=rows[0].project_name,
        preview_images=preview_images,
        total_images=total,
        folder_label=folder_label,
        zip_url=zip_url,
    )
    html = _render("gallery.html.j2", card.model_dump())
    return card, "gallery", html


async def _build_video(
    project_name: str,
    language: str,
    user: UserContext,
    unit_type: Optional[str] = None,
) -> tuple[VideoCard, str, str]:
    row: AssetRow = await asset_registry.get_asset(
        project_name=project_name,
        asset_type="video",
        user=user,
        language=language,
    )
    signed_url = await get_signed_url(row.blob_path)

    card = VideoCard(
        project=row.project_name,
        stream_url=signed_url,
        language=row.language,
    )
    html = _render("video.html.j2", card.model_dump())
    return card, "video", html


async def _build_project_overview(
    project_name: str,
    language: str,
    user: UserContext,
    unit_type: Optional[str] = None,
) -> tuple[ProjectOverviewCard, str, str]:
    languages = await asset_registry.get_available_languages(
        project_name=project_name,
        asset_type="brochure",
        user=user,
    )

    card = ProjectOverviewCard(
        project=project_name,
        available_asset_types=["brochure", "floor_plan", "gallery", "video"],
        available_languages=languages or ["en"],
    )
    html = _render("project_overview.html.j2", card.model_dump())
    return card, "project_overview", html


# ── Version / language card builders (used by check_version_node) ──────────────

async def build_version_card(
    entities: dict,
    user: UserContext,
) -> tuple[str, str]:
    """Build and render a version_check card. Returns (html, card_type)."""
    project_name = entities.get("project_name", "")
    asset_type = entities.get("asset_type", "brochure")
    language = entities.get("language", "en")

    versions: list[VersionRow] = await asset_registry.get_versions(
        project_name=project_name,
        asset_type=asset_type,
        user=user,
        language=language,
    )

    if not versions:
        raise NotFoundError(
            f"No versions found for {asset_type} of '{project_name}'."
        )

    current = next((v for v in versions if v.is_current), versions[0])

    card = VersionCheckCard(
        project=project_name,
        asset_type=asset_type,
        language=language,
        current_version=current.version_label,
        all_versions=[
            VersionEntry(
                version_label=v.version_label,
                is_current=v.is_current,
                released_at=v.released_at,
                release_notes=v.release_notes,
            )
            for v in versions
        ],
        is_latest=True,
    )
    html = _render("version_check.html.j2", card.model_dump())
    return html, "version_check"


async def build_language_list_card(
    entities: dict,
    user: UserContext,
) -> tuple[str, str]:
    """Build and render a language_list card. Returns (html, card_type)."""
    project_name = entities.get("project_name", "")
    asset_type = entities.get("asset_type", "brochure")

    lang_codes = await asset_registry.get_available_languages(
        project_name=project_name,
        asset_type=asset_type,
        user=user,
    )

    _LANG_NAMES = {
        "en": "English",
        "ar": "Arabic",
        "ru": "Russian",
        "zh": "Chinese",
        "fr": "French",
        "de": "German",
        "hi": "Hindi",
        "ur": "Urdu",
    }

    card = LanguageListCard(
        project=project_name,
        asset_type=asset_type,
        languages=[
            LanguageEntry(
                language_code=code,
                language_name=_LANG_NAMES.get(code, code.upper()),
            )
            for code in lang_codes
        ],
    )
    html = _render("language_list.html.j2", card.model_dump())
    return html, "language_list"


def render_clarification_card(missing_param: str, cs: dict) -> str:
    """Render a clarification prompt card."""
    _PARAM_PROMPTS = {
        "project_name": "Which Danube project are you asking about?",
        "unit_type": "Which unit type would you like? (e.g. Studio, 1BR, 2BR, 3BR)",
        "language": "Which language version do you need?",
        "version": "Which version are you looking for?",
    }
    prompt = _PARAM_PROMPTS.get(missing_param, "Could you provide more details?")
    project = (cs.get("collected") or {}).get("project_name", "")
    context = f" for {project}" if project else ""
    return (
        f'<div class="clarification-card" data-param="{missing_param}">'
        f'<p class="clarification-prompt">{prompt}{context}</p>'
        f"</div>"
    )
