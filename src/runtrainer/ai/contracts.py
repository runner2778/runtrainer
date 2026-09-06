"""AI 输出 JSON 契约（pydantic 严格校验）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 加练建议的允许类型
EXTRA_KINDS = ("E", "RECOVERY", "CROSS")
# 调整动作全集
ACTIONS = ("keep", "modify", "decrease", "rest", "add_easy", "shift", "skip")


class ChangeSet(BaseModel):
    """modify/decrease/shift 的具体变更，全部可选（缺省由服务端推导）。"""
    kind: str | None = None
    title: str | None = None
    description: str | None = None
    distance_km: float | None = None
    duration_min: float | None = None
    pace_zone: str | None = None
    date: str | None = None      # shift 目标日期
    slot: int | None = None      # 一天两练时段（1/2）；add_easy 落在已有课当日时用 2
    note: str | None = None

    @field_validator("slot", mode="before")
    @classmethod
    def _coerce_slot(cls, v):
        # 弱模型（glm-4-flash 等）偶尔把 slot 写成字符串（如与 pace_zone 混淆
        # 写成 "E"）：能转数字就转，语义不对按缺省处理（护栏已按 slot∈{1,2} 防御）
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return None
        return v


class AdjustmentItem(BaseModel):
    date: str = Field(description="调整生效日期 yyyy-mm-dd")
    planned_workout_id: int | None = Field(default=None, description="指向的课表 ID；add_easy 为 null")
    action: Literal["keep", "modify", "decrease", "rest", "add_easy", "shift", "skip"]
    changes: ChangeSet | None = None
    reason: str = Field(default="", description="调整理由（弱模型偶尔漏写，允许为空）")


class ExtraSuggestion(BaseModel):
    kind: Literal["E", "RECOVERY", "CROSS"]
    duration_min: float
    max_duration_min: float = 45.0
    pace_zone: str | None = None
    reason: str


class AddExtraAdvice(BaseModel):
    allowed: bool
    suggestion: ExtraSuggestion | None = None


class CoachOutput(BaseModel):
    summary: str = Field(min_length=1, description="一句话总结今日状态与调整")
    readiness: Literal["good", "ok", "low"]
    key_signals: list[str] = Field(default_factory=list, description="判断依据（1–5 条）")
    adjustments: list[AdjustmentItem] = Field(default_factory=list)
    add_extra_advice: AddExtraAdvice | None = Field(default=None)
    weekly_notes: str = Field(default="", description="本周训练提示")


# 聊天模式允许 AI 更新的档案键（服务端还要做取值范围钳制）
PROFILE_UPDATE_KEYS = ("max_hr", "rest_hr", "weight_kg", "run_experience")
PROFILE_UPDATE_RANGES = {
    "max_hr": (140, 230),
    "rest_hr": (30, 100),
    "weight_kg": (30, 200),
    "run_experience": None,  # 自由文本，仅限长度
}


class ChatOutput(BaseModel):
    """教练聊天输出：直接回复 + 可选的课表调整建议 + 可选的档案更新。"""
    reply: str = Field(min_length=1, description="对用户消息的直接回复（中文）")
    user_requested: bool = Field(
        default=False,
        description="用户在本条消息中明确要求调整课表（无论语气坚决还是商量）。"
                    "置 true 时必须至少给出 1 条 adjustments，不许空数组敷衍、不许以“不建议”回绝；"
                    "请求不合理时用降低强度/调整课表的方式落地")
    adjustments: list[AdjustmentItem] = Field(default_factory=list,
                                              description="仅当用户明确要求改课时给出，日期限未来 7 天")
    profile_updates: dict = Field(default_factory=dict,
                                  description="仅当用户明确提供新档案信息或健康数据与档案明显矛盾时给出，键限 max_hr/rest_hr/weight_kg/run_experience")
    rebuild_plan: bool = Field(default=False,
                               description="档案更新影响水平预估时置 true，服务端会按最新水平重建课表")
