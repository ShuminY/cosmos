"""Chatbot logic - RAG prompt building and flexible LLM provider support.

Provider registry pattern supports:
- OpenAI (gpt-3.5-turbo, gpt-4)
- Qianfan / Baidu (ernie-bot)
- Volces Ark / 火山方舟 (ark-code-latest, doubao-pro, etc.)
- Local transformers (TinyLlama, etc.)
"""
from __future__ import annotations
import json
from datetime import datetime
from typing import Callable, Any

from sqlalchemy import select, desc

from .db import session, ChatSession, ChatMessage, User, Project
from .kb import search_kb
from .settings import get_setting, set_setting


# ============ LLM Provider Registry ============
LLMProvider = Callable[[str, dict[str, Any]], tuple[str | None, str | None]]
_providers: dict[str, LLMProvider] = {}
_provider_configs: dict[str, dict] = {
    "openai": {
        "name": "OpenAI",
        "model": "gpt-3.5-turbo",
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
    },
    "qianfan": {
        "name": "Qianfan (百度千帆)",
        "model": "ernie-3.5-8k",
        "api_key": "",
        "secret_key": "",
    },
    "local": {
        "name": "Local (TinyLlama)",
        "model": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    },
    "ark": {
        "name": "火山方舟 (Volces Ark)",
        "model": "ep-xxxxxxxx",  # 在控制台创建接入点获取
        "api_key": "",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    },
}


def register_provider(name: str, provider_fn: LLMProvider):
    """Register an LLM provider."""
    _providers[name] = provider_fn


def get_provider(name: str) -> LLMProvider:
    """Get a provider by name."""
    return _providers.get(name)


def get_available_providers() -> list[str]:
    """Get list of available provider names."""
    return list(_providers.keys())


# ============ OpenAI Provider ============
def provider_openai(prompt: str, config: dict) -> tuple[str | None, str | None]:
    """OpenAI API provider."""
    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "https://api.openai.com/v1")
    model = config.get("model", "gpt-3.5-turbo")

    if not api_key:
        return None, "OpenAI API key not configured"

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        return response.choices[0].message.content, None
    except ImportError:
        return None, "openai package not installed"
    except Exception as e:
        return None, str(e)


register_provider("openai", provider_openai)


# ============ Qianfan Provider ============
def provider_qianfan(prompt: str, config: dict) -> tuple[str | None, str | None]:
    """Qianfan (百度千帆) API provider."""
    api_key = config.get("api_key", "")
    secret_key = config.get("secret_key", "")
    model = config.get("model", "ernie-3.5-8k")

    if not api_key or not secret_key:
        return None, "Qianfan API key or secret key not configured"

    try:
        import qianfan
        client = qianfan.ChatCompletion(ak=api_key, sk=secret_key)
        response = client.do(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.3,
            max_output_tokens=1024,
        )
        return response["body"]["result"], None
    except ImportError:
        return None, "qianfan package not installed"
    except Exception as e:
        return None, str(e)


register_provider("qianfan", provider_qianfan)


# ============ Volces Ark Provider ============
def provider_ark(prompt: str, config: dict) -> tuple[str | None, str | None]:
    """Volces Ark (火山方舟) API provider.
    Uses OpenAI-compatible API format.
    """
    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
    model = config.get("model", "ark-code-latest")

    if not api_key:
        return None, "Ark API key not configured"

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        return response.choices[0].message.content, None
    except ImportError:
        return None, "openai package not installed"
    except Exception as e:
        return None, str(e)


register_provider("ark", provider_ark)


# ============ Local Transformers Provider ============
def provider_local(prompt: str, config: dict) -> tuple[str | None, str | None]:
    """Simple local provider - just returns a mock response for testing."""
    try:
        # Extract context from prompt (between "上下文：---" and "---")
        import re
        ctx_match = re.search(r'上下文：\n---\n(.*?)\n---', prompt, re.DOTALL)
        context = ctx_match.group(1) if ctx_match else "无上下文"

        # Extract question
        q_match = re.search(r'用户问题：(.*?)\n\n回答：', prompt, re.DOTALL)
        question = q_match.group(1).strip() if q_match else prompt

        response = f"""
这是基于知识库检索的测试回答。

**您的问题**：{question}

**检索到的上下文摘要**：
{context[:300]}{'...' if len(context) > 300 else ''}

**说明**：
这是本地测试模式的模拟回答。如需真实的 LLM 回答，请配置并使用 Qianfan 或 OpenAI 提供商。
        """
        return response.strip(), None
    except Exception as e:
        return None, str(e)


register_provider("local", provider_local)


# ============ Provider Config Management ============
def load_provider_config(provider: str) -> dict:
    """Load provider config from settings."""
    key = f"llm_provider_{provider}"
    saved = get_setting(key, None)
    if saved:
        try:
            return json.loads(saved)
        except json.JSONDecodeError:
            pass
    return _provider_configs.get(provider, {})


def save_provider_config(provider: str, config: dict):
    """Save provider config to settings."""
    key = f"llm_provider_{provider}"
    set_setting(key, json.dumps(config))


def get_default_provider() -> str:
    """Get default LLM provider."""
    return get_setting("llm_default_provider", "openai")


def set_default_provider(provider: str):
    """Set default LLM provider."""
    set_setting("llm_default_provider", provider)


# ============ RAG Prompt Building ============
SYSTEM_PROMPT = """
你是一个专业的知识库助手，基于提供的上下文回答用户的问题。

回答规则：
1. 只使用上下文中提供的信息，不要编造
2. 如果上下文中没有答案，明确说明"在提供的文档中没有找到相关信息"
3. 回答要简洁、准确，引用相关的文档片段
4. 优先回答与工程建设、项目管理相关的内容

上下文：
---
{context}
---

请基于以上上下文回答用户的问题。
"""


def build_prompt(query: str, context_chunks: list[dict]) -> str:
    """Build RAG prompt from query and context chunks."""
    context_text = ""
    for i, chunk in enumerate(context_chunks, 1):
        score_pct = int(chunk["score"] * 100)
        context_text += f"\n[文档{i} - {chunk['filename']} - 相似度{score_pct}%]\n"
        context_text += f"{chunk['text']}\n"

    return SYSTEM_PROMPT.format(context=context_text) + f"\n用户问题：{query}\n\n回答："


# ============ Chat Logic ============
def generate_answer(query: str, context_chunks: list[dict], provider: str | None = None) -> dict:
    """Generate an answer using the specified LLM provider.
    Returns {answer, error, provider_used}.
    """
    if provider is None:
        provider = get_default_provider()

    if provider not in _providers:
        return {
            "answer": None,
            "error": f"Unknown provider: {provider}",
            "provider_used": provider,
        }

    provider_fn = _providers[provider]
    provider_config = load_provider_config(provider)

    if not context_chunks:
        return {
            "answer": "知识库中没有相关文档或找不到与您问题相关的内容。请先上传文档并建立索引。",
            "error": None,
            "provider_used": provider,
            "context_used": [],
        }

    prompt = build_prompt(query, context_chunks)
    answer, error = provider_fn(prompt, provider_config)

    return {
        "answer": answer,
        "error": error,
        "provider_used": provider,
        "context_used": context_chunks,
    }


def chat(project_id: int, query: str, session_id: int | None = None,
         user_id: int | None = None, provider: str | None = None,
         top_k: int = 5) -> dict:
    """Full chat flow: search KB -> generate answer -> save conversation.
    Returns {session_id, message_id, answer, error, context_chunks}.
    """
    # Search KB
    context_chunks = search_kb(project_id, query, top_k=top_k)

    # Generate answer
    result = generate_answer(query, context_chunks, provider)
    if result.get("error"):
        return {
            "session_id": session_id,
            "message_id": None,
            "answer": None,
            "error": result["error"],
            "context_chunks": context_chunks,
        }

    answer = result["answer"]
    used_chunk_ids = [c["chunk_id"] for c in context_chunks]

    # Save to DB
    with session() as s:
        if session_id is None:
            # Auto-generate title from first query
            title = query[:60] + ("..." if len(query) > 60 else "")
            chat_sess = ChatSession(
                project_id=project_id,
                user_id=user_id,
                title=title,
            )
            s.add(chat_sess)
            s.flush()
            session_id = chat_sess.id
        else:
            chat_sess = s.get(ChatSession, session_id)
            chat_sess.updated_at = datetime.utcnow()

        # User message
        user_msg = ChatMessage(
            session_id=session_id,
            role="user",
            content=query,
        )
        s.add(user_msg)

        # Assistant message
        assistant_msg = ChatMessage(
            session_id=session_id,
            role="assistant",
            content=answer or "",
            context_chunk_ids=json.dumps(used_chunk_ids),
        )
        s.add(assistant_msg)
        s.flush()
        message_id = assistant_msg.id

        s.commit()

    return {
        "session_id": session_id,
        "message_id": message_id,
        "answer": answer,
        "error": None,
        "context_chunks": context_chunks,
    }


# ============ Session Management ============
def list_chat_sessions(project_id: int | None = None,
                       user_id: int | None = None,
                       limit: int = 50) -> list[ChatSession]:
    """List chat sessions, optionally filtered by project or user."""
    with session() as s:
        stmt = select(ChatSession)
        if project_id is not None:
            stmt = stmt.where(ChatSession.project_id == project_id)
        if user_id is not None:
            stmt = stmt.where(ChatSession.user_id == user_id)
        stmt = stmt.order_by(desc(ChatSession.updated_at)).limit(limit)
        sessions = s.execute(stmt).scalars().all()
        for sess in sessions:
            s.expunge(sess)
        return sessions


def get_chat_messages(session_id: int) -> list[dict]:
    """Get all messages in a chat session with context info."""
    with session() as s:
        messages = s.execute(
            select(ChatMessage).where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at)
        ).scalars().all()

        result = []
        for msg in messages:
            item = {
                "id": msg.id,
                "role": msg.role,
                "content": msg.content,
                "created_at": msg.created_at,
            }
            if msg.context_chunk_ids:
                try:
                    item["context_chunk_ids"] = json.loads(msg.context_chunk_ids)
                except json.JSONDecodeError:
                    item["context_chunk_ids"] = []
            result.append(item)
        return result


def delete_chat_session(session_id: int):
    """Delete a chat session and all its messages."""
    with session() as s:
        sess = s.get(ChatSession, session_id)
        if sess:
            s.delete(sess)
            s.commit()
