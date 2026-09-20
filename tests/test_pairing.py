"""手机伴侣屏 PIN 与开关（批24 M1）：默认关/持久化/6 位/不重生成/重置/
关闭不删 PIN，以及 PinGate 连续失败退避窗口。
"""
import pytest

from runtrainer.services import settings_service
from runtrainer.companion.handler import PinGate


@pytest.fixture(autouse=True)
def _pin_env(fake_keyring, monkeypatch):
    """keyring 打桩 + secrets 固定输出（42 → "000042"，7 → "000007"）。"""
    monkeypatch.setattr(settings_service.secrets, "randbelow", lambda n: 42)
    return fake_keyring


# ---- 开关与 PIN 设置 ----

def test_companion_disabled_by_default():
    assert settings_service.get_companion_enabled() is False


def test_companion_toggle_persists():
    settings_service.set_companion_enabled(True)
    assert settings_service.get_companion_enabled() is True
    settings_service.set_companion_enabled(False)
    assert settings_service.get_companion_enabled() is False


def test_generate_pin_is_6_digit(_pin_env):
    pin = settings_service.generate_companion_pin()
    assert pin == "000042"
    assert _pin_env["companion_pin"] == "000042"


def test_get_or_create_is_stable(_pin_env):
    first = settings_service.get_or_create_companion_pin()
    assert settings_service.get_or_create_companion_pin() == first


def test_reset_generates_new_pin(_pin_env, monkeypatch):
    old = settings_service.get_or_create_companion_pin()
    assert old == "000042"
    monkeypatch.setattr(settings_service.secrets, "randbelow", lambda n: 7)
    new = settings_service.reset_companion_pin()
    assert new == "000007" and new != old


def test_disable_keeps_pin(_pin_env):
    pin = settings_service.generate_companion_pin()
    settings_service.set_companion_enabled(True)
    settings_service.set_companion_enabled(False)
    assert settings_service.get_companion_pin() == pin


# ---- PinGate 退避 ----

@pytest.fixture
def gate_clock(monkeypatch):
    """固定单调钟：测试手动推进时间。"""
    from runtrainer.companion import handler
    clock = {"now": 1000.0}
    monkeypatch.setattr(handler, "_monotonic", lambda: clock["now"])
    return clock


def test_gate_locks_after_5_failures(gate_clock):
    gate = PinGate()
    for _ in range(4):
        gate.register_failure()
    assert gate.retry_after() == 0  # 未满 5 次不锁
    gate.register_failure()
    assert gate.retry_after() == 5 and gate.is_locked


def test_gate_lock_window_increases(gate_clock):
    """锁窗 5→15→60→300s 递增，封顶 300s；到期自动解锁。"""
    gate = PinGate()
    for fails, expected in ((5, 5), (5, 15), (5, 60), (5, 300), (5, 300)):
        for _ in range(fails):
            gate.register_failure()
        assert gate.retry_after() == expected
        gate_clock["now"] += expected  # 到期
        assert not gate.is_locked


def test_gate_success_resets_counter(gate_clock):
    gate = PinGate()
    for _ in range(4):
        gate.register_failure()
    gate.on_success()  # 4 错后成功 → 计数清零
    for _ in range(4):
        gate.register_failure()
    assert gate.retry_after() == 0  # 仍需再错 1 次才锁（从头计）
    gate.register_failure()
    assert gate.retry_after() == 5  # 且窗口回到第一档
