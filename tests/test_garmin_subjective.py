"""佳明活动详情里的主观标签（感受 / 用力程度）解析。

打不到真机 API，这里覆盖三条已知的取值路径：
  1. CN 区：顶层 metricDescriptors 描述符 + activityDetailMetrics 行对象（按 metricsIndex 对齐）
  2. 国际区：activityDetailMetrics 为 [{key, metrics:[...]}]（每列自描述）
  3. summaryDTO / 顶层字段兜底（workoutFeel / directWorkoutFeel）
解析返回的是**原始量纲**（佳明是 0/25/50/75/100 与 0…100），
归一成 1-5 / 1-10 由 sync_service 调用 activity_edit.norm_feel/norm_rpe 完成——
所以这里既断言原始值，也断言「经过 sync_service 归一后落在正确档位」。
真机回读（佳明 app 里真打了标签的活动）仍属【待真机验证】。
"""
from types import SimpleNamespace

from runtrainer.domain import activity_edit as ae
from runtrainer.garmin.garminconnect_adapter import GarminConnectAdapter


def _adapter(summary: dict, detail: dict) -> GarminConnectAdapter:
    a = object.__new__(GarminConnectAdapter)     # 绕开 __init__ 里的真客户端
    a._client = SimpleNamespace(
        get_activity=lambda _id: summary,
        get_activity_details=lambda _id: detail,
    )
    return a


CN_DESCRIPTORS = [
    {"metricsIndex": 0, "key": "directTimestamp"},
    {"metricsIndex": 1, "key": "directWorkoutFeel"},
    {"metricsIndex": 2, "key": "directWorkoutRpe"},
]


def _cn_detail(rows: list[list]) -> dict:
    return {"metricDescriptors": CN_DESCRIPTORS,
            "activityDetailMetrics": [{"metrics": r} for r in rows]}


_SUMMARY = {"activityName": "晨跑", "summaryDTO": {"duration": 1800.0, "distance": 5000.0}}


# ------------------------------------------------------- 1. CN 区描述符列

def test_cn_layout_picks_first_non_empty_label_row():
    """标签不是逐采样变化的量，只有少数行非空；要扫到第一个非空值。"""
    detail = _cn_detail([[1000.0, 0, 0], [1002.0, 0, 0], [1004.0, 75, 70]])
    raw = _adapter(_SUMMARY, detail).fetch_activity_detail("1")
    assert raw.feel == 75 and raw.rpe == 70
    assert ae.norm_feel(raw.feel) == 4
    assert ae.norm_rpe(raw.rpe) == 7


def test_cn_layout_reads_each_key_independently():
    """感受和用力程度可能出现在不同行（用户分两次填），不能要求同一行。"""
    detail = _cn_detail([[1000.0, 50, 0], [1002.0, 0, 80]])
    raw = _adapter(_SUMMARY, detail).fetch_activity_detail("1")
    assert ae.norm_feel(raw.feel) == 3
    assert ae.norm_rpe(raw.rpe) == 8


def test_cn_layout_all_zero_is_unlabelled():
    detail = _cn_detail([[1000.0, 0, 0], [1002.0, 0, 0]])
    raw = _adapter(_SUMMARY, detail).fetch_activity_detail("1")
    assert raw.feel is None and raw.rpe is None


# --------------------------------------------------- 2. 国际区自描述列

def test_international_layout_reads_columns():
    detail = {"activityDetailMetrics": [
        {"key": "directWorkoutFeel", "metrics": [0, 0, 25]},
        {"key": "directWorkoutRpe", "metrics": [0, 30]},
    ]}
    raw = _adapter(_SUMMARY, detail).fetch_activity_detail("1")
    assert raw.feel == 25 and raw.rpe == 30
    assert ae.norm_feel(raw.feel) == 1
    assert ae.norm_rpe(raw.rpe) == 3


def test_international_layout_without_labels_is_unlabelled():
    detail = {"activityDetailMetrics": [
        {"key": "directHeartRate", "metrics": [140, 150]},
    ]}
    raw = _adapter(_SUMMARY, detail).fetch_activity_detail("1")
    assert raw.feel is None and raw.rpe is None


# ---------------------------------------------------- 3. 概要字段兜底

def test_summary_fields_used_when_detail_has_no_labels():
    summary = dict(_SUMMARY, workoutFeel=100, workoutRpe=90)
    raw = _adapter(summary, {}).fetch_activity_detail("1")
    assert raw.feel == 100 and raw.rpe == 90
    assert ae.norm_feel(raw.feel) == 5
    assert ae.norm_rpe(raw.rpe) == 9


def test_summary_zero_means_unlabelled():
    """佳明用 0 表示「没打标签」，不能当成最低档存进去。"""
    summary = dict(_SUMMARY, workoutFeel=0, workoutRpe=0)
    raw = _adapter(summary, {}).fetch_activity_detail("1")
    assert raw.feel is None and raw.rpe is None


def test_detail_labels_win_over_summary_fields():
    """detail 里读到了就用 detail 的；概要字段只是兜底。"""
    summary = dict(_SUMMARY, workoutFeel=100, workoutRpe=100)
    detail = _cn_detail([[1000.0, 25, 40]])
    raw = _adapter(summary, detail).fetch_activity_detail("1")
    assert raw.feel == 25 and raw.rpe == 40
    assert ae.norm_feel(raw.feel) == 1
    assert ae.norm_rpe(raw.rpe) == 4
