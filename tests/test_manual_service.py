"""手动补录活动：入库链路、校验、编辑、删除，以及下游一致性。"""
import pytest

from runtrainer.db.repos import activity_repo
from runtrainer.services import manual_service


def _base(**kw):
    p = {"name": "夜跑", "distance_m": 10000, "duration_s": 3000,
         "avg_hr": 150, "max_hr": 170, "avg_cadence": 180, "rpe": 6, "feel": 4,
         "note": "风大"}
    p.update(kw)
    return p


def test_add_activity_lands_with_all_fields():
    res = manual_service.add_activity(_base())
    assert res["is_new"] is True
    a = res["activity"]
    assert a["source"] == "manual"
    assert a["manual"] == 1
    assert a["distance_m"] == 10000
    assert a["avg_pace_s_km"] == 300.0
    assert a["stride_length_m"] == 1.111
    assert a["rpe"] == 6 and a["feel"] == 4
    assert a["note"] == "风大"
    assert a["external_id"].startswith("manual-")


def test_add_activity_external_id_is_unique():
    ids = {manual_service.add_activity(_base())["activity"]["external_id"]
           for _ in range(5)}
    assert len(ids) == 5


def test_add_activity_rejects_nonsense():
    with pytest.raises(manual_service.ManualError, match="至少要填一个"):
        manual_service.add_activity({"name": "空"})
    with pytest.raises(manual_service.ManualError, match="距离必须大于 0"):
        manual_service.add_activity(_base(distance_m=0))
    with pytest.raises(manual_service.ManualError, match="用时必须大于 0"):
        manual_service.add_activity(_base(duration_s=-1))
    with pytest.raises(manual_service.ManualError, match="平均心率"):
        manual_service.add_activity(_base(avg_hr=300))
    with pytest.raises(manual_service.ManualError, match="最大心率不能低于"):
        manual_service.add_activity(_base(avg_hr=170, max_hr=150))
    with pytest.raises(manual_service.ManualError, match="步频"):
        manual_service.add_activity(_base(avg_cadence=5))


def test_add_activity_rejects_impossible_pace():
    """距离/用时算出 70 s/km（1:10/km）这种超人类配速 → 挡住（多半单位填错）。"""
    with pytest.raises(manual_service.ManualError, match="配速不合常理"):
        manual_service.add_activity(_base(distance_m=10000, duration_s=700))


def test_manual_activity_is_visible_to_prediction_pipeline():
    """补录的活动要和手表活动同权：能进活动列表、能被课程分类器看到。"""
    manual_service.add_activity(_base())
    rows = activity_repo.list_activities()
    assert len(rows) == 1
    assert rows[0]["manual"] == 1


def test_edit_activity_updates_fields_and_recomputes_pace():
    aid = manual_service.add_activity(_base())["id"]
    out = manual_service.edit_activity(aid, {"duration_s": 2700, "rpe": 9})
    assert out["duration_s"] == 2700
    assert out["avg_pace_s_km"] == 270.0
    assert out["rpe"] == 9
    assert out["distance_m"] == 10000        # 未改的字段保持


def test_edit_activity_rejects_missing():
    with pytest.raises(manual_service.ManualError, match="活动不存在"):
        manual_service.edit_activity(999999, {"rpe": 5})


def test_edit_activity_validates_merged_result():
    """单看新值合法、合并后非法的情况也要挡住（最大心率 < 平均心率）。"""
    aid = manual_service.add_activity(_base(avg_hr=150, max_hr=170))["id"]
    with pytest.raises(manual_service.ManualError, match="最大心率不能低于"):
        manual_service.edit_activity(aid, {"max_hr": 120})


def test_delete_activity_removes_row_and_samples():
    aid = manual_service.add_activity(_base())["id"]
    activity_repo.save_samples(aid, [(0, 140, 3.0, 180, 10.0)])
    assert activity_repo.get_samples(aid)
    assert manual_service.delete_activity(aid) is True
    assert activity_repo.get_activity(aid) is None
    assert activity_repo.get_samples(aid) == []


def test_delete_activity_rejects_missing():
    with pytest.raises(manual_service.ManualError, match="活动不存在"):
        manual_service.delete_activity(999999)


def test_refresh_after_entry_is_noop_without_plan():
    assert manual_service.refresh_plan_after_entry() is None
