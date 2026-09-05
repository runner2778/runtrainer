"""设置服务：敏感凭据存 Windows 凭据管理器（keyring），非敏感配置存 SQLite。"""
from __future__ import annotations

import logging

import keyring

from ..db.repos import kv_repo

log = logging.getLogger(__name__)

KEYRING_SERVICE = "runtrainer"
K_GARMIN_USERNAME = "garmin_username"
K_GARMIN_PASSWORD = "garmin_password"
K_DEEPSEEK_KEY = "deepseek_api_key"

# 非敏感 KV 键
S_THEME = "theme"
S_AI_MODEL = "ai_model"
S_MOCK_MODE = "mock_mode"
S_GARMIN_CN = "garmin_cn"
S_GARMIN_USERNAME_VISIBLE = "garmin_username_visible"  # 账号名非敏感，明文存便于回显


def _get(entry: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, entry)
    except Exception as e:  # keyring 后端异常时降级为空
        log.warning("keyring 读取失败 %s: %s", entry, e)
        return None


def _set(entry: str, value: str) -> None:
    keyring.set_password(KEYRING_SERVICE, entry, value)


def _delete(entry: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, entry)
    except keyring.errors.PasswordDeleteError:
        pass


# ---- Garmin ----
def get_garmin_credentials() -> tuple[str | None, str | None]:
    username = kv_repo.get_setting(S_GARMIN_USERNAME_VISIBLE) or _get(K_GARMIN_USERNAME)
    return username, _get(K_GARMIN_PASSWORD)


def set_garmin_credentials(username: str, password: str) -> None:
    kv_repo.set_setting(S_GARMIN_USERNAME_VISIBLE, username)
    _set(K_GARMIN_USERNAME, username)
    _set(K_GARMIN_PASSWORD, password)


def clear_garmin_credentials() -> None:
    kv_repo.set_setting(S_GARMIN_USERNAME_VISIBLE, "")
    _delete(K_GARMIN_USERNAME)
    _delete(K_GARMIN_PASSWORD)


# ---- DeepSeek ----
def get_deepseek_key() -> str | None:
    return _get(K_DEEPSEEK_KEY)


def set_deepseek_key(api_key: str) -> None:
    _set(K_DEEPSEEK_KEY, api_key)


def clear_deepseek_key() -> None:
    _delete(K_DEEPSEEK_KEY)


# ---- 非敏感设置 ----
def get_ai_model() -> str:
    return kv_repo.get_setting(S_AI_MODEL, "deepseek-v4-pro")


def set_ai_model(model: str) -> None:
    kv_repo.set_setting(S_AI_MODEL, model)


def get_theme() -> str:
    return kv_repo.get_setting(S_THEME, "system")


def set_theme(theme: str) -> None:
    kv_repo.set_setting(S_THEME, theme)


def is_mock_mode() -> bool:
    return kv_repo.get_setting(S_MOCK_MODE, "1") == "1"


def set_mock_mode(enabled: bool) -> None:
    kv_repo.set_setting(S_MOCK_MODE, "1" if enabled else "0")


def is_garmin_cn() -> bool:
    """Garmin 中国区账号（使用独立服务器）。默认开启。"""
    return kv_repo.get_setting(S_GARMIN_CN, "1") == "1"


def set_garmin_cn(enabled: bool) -> None:
    kv_repo.set_setting(S_GARMIN_CN, "1" if enabled else "0")
