"""配置管理器 - 统一管理项目配置.

支持从以下位置读取配置（优先级从高到低）：
1. 环境变量
2. .env 文件
3. settings.yaml 文件
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Optional

# 缓存配置
_config_cache: Optional[dict] = None


def get_project_root() -> Path:
    """获取项目根目录."""
    return Path(__file__).resolve().parent.parent


def load_yaml_config() -> dict:
    """加载 settings.yaml 配置."""
    config_file = get_project_root() / "settings.yaml"
    if not config_file.exists():
        return {}

    try:
        import yaml
        with open(config_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def load_env_file() -> dict:
    """加载 .env 文件配置."""
    env_file = get_project_root() / ".env"
    if not env_file.exists():
        return {}

    config = {}
    try:
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip().strip('"\'')
    except Exception:
        pass
    return config


def get_config(key: str, default: Any = None) -> Any:
    """获取配置项（带缓存）.

    优先级：环境变量 > .env > settings.yaml

    Args:
        key: 配置键名
        default: 默认值

    Returns:
        配置值或默认值
    """
    global _config_cache

    # 1. 环境变量（最高优先级）
    env_value = os.getenv(key)
    if env_value:
        return env_value

    # 加载缓存
    if _config_cache is None:
        _config_cache = {}
        _config_cache.update(load_yaml_config())
        _config_cache.update(load_env_file())

    # 2. 从缓存获取
    return _config_cache.get(key, default)


def get_aholo3d_api_key() -> str:
    """获取 Aholo 3D API Key.

    Returns:
        API Key 字符串

    Raises:
        ValueError: 未配置 API Key
    """
    # 尝试多种可能的键名
    for key in ['AHOLO3D_API_KEY', 'aholo3d_api_key', 'AHOLO_API_KEY']:
        value = get_config(key)
        if value:
            return value

    raise ValueError(
        "Aholo 3D API Key 未配置。\n"
        "请通过以下方式之一配置:\n"
        "1. 环境变量: export AHOLO3D_API_KEY='your_key'\n"
        "2. .env 文件: echo 'AHOLO3D_API_KEY=your_key' > .env\n"
        "3. settings.yaml: 设置 aholo3d_api_key: your_key\n"
        "\n获取 API Key: https://labs.aholo3d.cn"
    )


def reload_config():
    """重新加载配置（清除缓存）."""
    global _config_cache
    _config_cache = None
