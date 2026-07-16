"""提示词模板加载器。

把每次审核/分析都重复的提示词从 Python 代码抽离到 prompts/*.md，
便于非开发人员维护。模板中使用 {name} 占位符，通过 load_prompt(**kwargs) 填充。
"""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=32)
def _read_template(name: str) -> str:
    """读取 prompts/<name>.md 原始模板（带缓存）."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"提示词模板不存在：{path}")
    return path.read_text(encoding="utf-8").strip()


def load_prompt(name: str, **kwargs) -> str:
    """加载模板并用 kwargs 填充 {占位符}。

    模板缺失的占位符会抛 KeyError；多余的 kwargs 被忽略。
    未提供的占位符默认填空字符串，避免调用方漏传时报错。
    """
    template = _read_template(name)

    class _SafeDict(dict):
        def __missing__(self, key):  # noqa: D401 - 占位符缺省填空
            return ""

    return template.format_map(_SafeDict(kwargs))


def clear_cache():
    """清空模板缓存（编辑 .md 后热更新用）."""
    _read_template.cache_clear()
