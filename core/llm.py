"""
LLM clients and streaming helpers.

The ONLY file in the codebase that imports langchain_openai.
Everything else imports chat_model, embeddings_model, stream_text_chunks from here.
LangSmith traces every call automatically via the LangChain wrapper.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from langchain_core.messages import BaseMessageChunk
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings

from core.errors import LLMError
from core.settings import settings

logger = logging.getLogger(__name__)

chat_model = AzureChatOpenAI(
    azure_endpoint=settings.azure_openai_endpoint,
    api_key=settings.azure_openai_api_key,
    api_version=settings.azure_openai_api_version,
    azure_deployment=settings.azure_openai_deployment,
    streaming=True,
    temperature=0.3,
    max_tokens=220,  # short commentaries: brochure, gallery, floor plan, video (1-2 sentences + bold)
)

# Used for responses that include markdown bullet lists: knowledge_query, project_overview, inventory
chat_model_long = AzureChatOpenAI(
    azure_endpoint=settings.azure_openai_endpoint,
    api_key=settings.azure_openai_api_key,
    api_version=settings.azure_openai_api_version,
    azure_deployment=settings.azure_openai_deployment,
    streaming=True,
    temperature=0.3,
    max_tokens=2000,
)

embeddings_model = AzureOpenAIEmbeddings(
    azure_endpoint=settings.azure_openai_endpoint,
    api_key=settings.azure_openai_api_key,
    api_version=settings.azure_openai_api_version,
    azure_deployment=settings.azure_openai_embedding_deployment,
)


async def stream_text_chunks(
    astream_gen: AsyncGenerator[BaseMessageChunk, None],
) -> AsyncGenerator[str, None]:
    """Yield non-empty text strings from a LangChain astream() generator."""
    try:
        async for chunk in astream_gen:
            if chunk.content and isinstance(chunk.content, str):
                yield chunk.content
            elif isinstance(chunk.content, list):
                for block in chunk.content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            yield text
    except Exception as exc:
        logger.error("llm_stream_error", extra={"error": str(exc)})
        raise LLMError(f"LLM streaming failed: {exc}") from exc
