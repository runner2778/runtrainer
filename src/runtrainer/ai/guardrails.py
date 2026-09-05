"""AI 输出护栏（纯函数，不碰 DB/网络）：逐条校验，能钳制的钳制，不能修复的丢弃。

规则：
1. schema 校验（pydantic，由调用方先做）
2. 日期范围 [今天, +6]；调整必须指向存在的课；add_easy 必须落在空档日
3. 调整后相邻日不得同时为强度课
4. 已有课扩容累计不超过 +10%（减量不设下限；加练由规则 5 单独约束）
5. 加练仅 E/RECOVERY/CROSS、≤45 分钟、每周 ≤2 次、赛前 3 天禁止
6. modify 距离变化 ≤30%、配速区必须来自配速表
7. 赛前 14 天只能 keep/rest/decrease/skip 或轻松课
8. 强度课容量上限（I ≤8% 周量且 ≤10km；R ≤5% 且 ≤8km；T ≤50min；M ≤32km）
9. 加练请求但缺 add_extra_advice → 由 coach_service 重试一次
10. 全部被丢弃 → 回退原计划（由 coach_service 保证）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .contracts import CoachOutput

HARD_KINDS = {"T", "I", "R", "M", "TUNEUP", "RACE"}
VALID_KINDS = {"E", "M", "T", "I", "R", "LR", "RECOVERY"}
VALID_ZONES = {"E", "M", "T", "I", "R"}
EXTRA_KINDS = {"E", "RECOVERY", "CROSS"}

# 容量上限（与 plan_engine 保持一致）
CAP_I_WEEK_PCT = 0.08
CAP_I_KM = 10.0
CAP_R_WEEK_PCT = 0.05
CAP_R_KM = 8.0
CAP_T_MIN = 50.0
CAP_M_KM = 32.0


@dataclass
class GuardContext:
    today: date
    race_date: date
    week_start: date
    week_end: date
    week_km: float                       # 本周计划跑量（调整前）
    workouts: list[dict]                 # 本周一至今天+6 的课：{id,date,kind,pace_zone,distance_km,duration_min,status}
    paces: dict                          # vd.pace_table 输出
    add_extra_count_this_week: int = 0   # 本周已生效的加练次数
    extra_requested: bool = False
    force: bool = False                  # 训练者明确强制要求调整：不打回，降级落地（不拒绝）
    # force 豁免：赛前窗口非轻松改动→降 E 落地；相邻强度日冲突→降 E 落地；挪课进强度夹缝→降 E 挪入；
    # 周量 +10% 上限豁免；赛前加练→改 20–30 分钟恢复跑。
    # force 不豁免：距离 ±30%/容量上限钳制与数据合法性（日期范围/课存在/已完成课/占档日）


def is_hard(kind: str, pace_zone: str | None) -> bool:
    return kind in HARD_KINDS or (kind == "LR" and pace_zone == "M")


def _in_taper_window(ctx: GuardContext, d: date) -> bool:
    return d >= ctx.race_date - timedelta(days=13)


def _in_week(ctx: GuardContext, d: date) -> bool:
    return ctx.week_start <= d <= ctx.week_end


def _wkey(w: dict) -> str:
    """一天两练：同日期按 slot 区分占用（slot 缺省视为 1）。"""
    return f"{w['date']}#{w.get('slot') or 1}"


def _day_hard(by_date: dict, d: date) -> bool:
    """某日任意时段存在强度课即视为强度日（二练日也只看一天一档）。"""
    return any(w["date"] == d.isoformat() and is_hard(w["kind"], w.get("pace_zone"))
               for w in by_date.values())


def _day_occupied(by_date: dict, d: date) -> bool:
    return any(w["date"] == d.isoformat() for w in by_date.values())


def _adjacent_ok(by_date: dict, d: date) -> bool:
    if not _day_hard(by_date, d):
        return True
    for off in (-1, 1):
        if _day_hard(by_date, d + timedelta(days=off)):
            return False
    return True


class _State:
    def __init__(self, ctx: GuardContext):
        self.by_date: dict[str, dict] = {_wkey(w): dict(w) for w in ctx.workouts}
        self.week_km = float(ctx.week_km)
        self.extras = int(ctx.add_extra_count_this_week)

    def km_delta(self, ctx: GuardContext, delta: float) -> bool:
        """周跑量检查：增幅累计不超过 +10%（减量是安全方向，不设下限）。"""
        new = self.week_km + delta
        if delta > 0 and new > 1.10 * ctx.week_km:
            return False
        self.week_km = new
        return True


def validate(output: CoachOutput, ctx: GuardContext) -> tuple[list[dict], list[str]]:
    """返回 (可落库的调整项列表, 护栏日志)。逐条顺序应用，违规即丢弃。"""
    state = _State(ctx)
    log: list[str] = []
    accepted: list[dict] = []
    suggestion = output.add_extra_advice.suggestion if output.add_extra_advice else None
    for i, item in enumerate(output.adjustments):
        try:
            got = _apply(item, suggestion, state, ctx, log, i)
            if got is not None:
                accepted.append(got)
        except ValueError as e:
            log.append(f"调整#{i} 数据非法被丢弃: {e}")
    return accepted, log


def _apply(item, suggestion, state: _State, ctx: GuardContext, log: list[str], idx: int) -> dict | None:
    """单条调整：钳制 → 状态模拟 → 成功返回 dict，失败返回 None 并记日志。"""
    action = item.action
    try:
        d = date.fromisoformat(item.date)
    except ValueError:
        log.append(f"调整#{idx} 日期格式错误被丢弃: {item.date}")
        return None
    if not (ctx.today <= d <= ctx.today + timedelta(days=6)):
        log.append(f"调整#{idx} 日期 {item.date} 超出 [今天, +6] 范围被丢弃")
        return None

    # 定位目标课
    workout = None
    if item.planned_workout_id is not None:
        for w in state.by_date.values():
            if w.get("id") == item.planned_workout_id:
                workout = w
                break
        if workout is None:
            log.append(f"调整#{idx} 指向不存在的课 id={item.planned_workout_id} 被丢弃")
            return None
        if workout["date"] != item.date:
            item = item.model_copy(update={"date": workout["date"]})
            d = date.fromisoformat(workout["date"])
            log.append(f"调整#{idx} 日期与课表不一致，已改为 {workout['date']}")
    else:
        if action != "add_easy":
            log.append(f"调整#{idx} 缺少 planned_workout_id 被丢弃")
            return None

    taper = _in_taper_window(ctx, d)
    changes = item.changes
    out = {"date": d.isoformat(), "planned_workout_id": item.planned_workout_id,
           "action": action, "changes": changes.model_dump(exclude_none=True) if changes else None,
           "reason": item.reason}

    if action == "keep":
        if workout["status"] == "completed":
            log.append(f"调整#{idx} 已完成课 keep，忽略")
        return out

    if workout and workout.get("status") == "completed":
        log.append(f"调整#{idx} 目标课已完成，不可再调整")
        return None

    if action in ("rest", "skip"):
        state.by_date.pop(_wkey(workout), None)
        if _in_week(ctx, d) and not state.km_delta(ctx, -float(workout.get("distance_km") or 0)):
            state.by_date[_wkey(workout)] = workout
            log.append(f"调整#{idx} 休息导致周量降幅超 10% 被丢弃")
            return None
        return out

    if action == "decrease":
        old = float(workout.get("distance_km") or 0)
        new = old * 0.8
        if changes and changes.distance_km is not None:
            new = min(float(changes.distance_km), old)
        new = max(0.0, new)
        key = _wkey(workout)
        orig = state.by_date[key]
        state.by_date[key] = dict(workout, distance_km=new)
        if _in_week(ctx, d) and not state.km_delta(ctx, new - old):
            state.by_date[key] = orig
            log.append(f"调整#{idx} 降量导致周量变化超限被丢弃")
            return None
        if changes is None:
            out["changes"] = {"distance_km": round(new, 1)}
        else:
            out["changes"] = dict(changes.model_dump(exclude_none=True), distance_km=round(new, 1))
        return out

    if action == "modify":
        old = float(workout.get("distance_km") or 0)
        old_dur = float(workout.get("duration_min") or 0)
        kind = workout["kind"]
        zone = workout.get("pace_zone")
        dist = old
        dur = old_dur
        if changes:
            if changes.kind and changes.kind in VALID_KINDS:
                kind = changes.kind
            elif changes.kind:
                log.append(f"调整#{idx} 非法 kind={changes.kind}，保留原类型 {kind}")
            if changes.pace_zone:
                if changes.pace_zone in VALID_ZONES:
                    zone = changes.pace_zone
                else:
                    log.append(f"调整#{idx} 非法 pace_zone={changes.pace_zone}，保留 {zone}")
            if changes.distance_km is not None:
                dist = float(changes.distance_km)
                if dist > 1.3 * old:
                    dist = 1.3 * old
                    log.append(f"调整#{idx} 距离增幅超 30%，钳制为 {dist:.1f}km")
                elif dist < 0.7 * old:
                    dist = 0.7 * old
                    log.append(f"调整#{idx} 距离降幅超 30%，钳制为 {dist:.1f}km")
            if changes.duration_min is not None:
                dur = float(changes.duration_min)
        if taper and not (kind in ("E", "RECOVERY") or (kind == "LR" and zone == "E")):
            if not ctx.force:
                log.append(f"调整#{idx} 赛前 14 天不可改为 {kind}，被丢弃")
                return None
            log.append(f"调整#{idx} 强制模式：赛前 14 天内不能上 {kind} 强度，按用户要求降为轻松跑 E 落地（跑量保留）")
            kind, zone = "E", "E"
        # 容量上限
        if kind == "I":
            dist = min(dist, min(CAP_I_WEEK_PCT * ctx.week_km, CAP_I_KM))
        elif kind == "R":
            dist = min(dist, min(CAP_R_WEEK_PCT * ctx.week_km, CAP_R_KM))
        elif kind == "T":
            dur = min(dur, CAP_T_MIN)
        if zone == "M":
            dist = min(dist, CAP_M_KM)
        new_w = dict(workout, kind=kind, pace_zone=zone, distance_km=dist, duration_min=dur)
        key = _wkey(workout)
        state.by_date[key] = new_w
        if not _adjacent_ok(state.by_date, d):
            state.by_date[key] = workout
            if not ctx.force:
                log.append(f"调整#{idx} 造成相邻日强度课冲突，被丢弃")
                return None
            log.append(f"调整#{idx} 强制模式：相邻日已有强度课，{kind} 无法插入，按用户要求降为 E 轻松跑落地")
            kind, zone = "E", "E"
            new_w = dict(workout, kind=kind, pace_zone=zone, distance_km=dist, duration_min=dur)
            state.by_date[key] = new_w
        if _in_week(ctx, d) and not state.km_delta(ctx, dist - old):
            if not ctx.force:
                state.by_date[key] = workout
                log.append(f"调整#{idx} 导致周量变化超 ±10% 被丢弃")
                return None
            state.week_km += dist - old
            log.append(f"调整#{idx} 强制模式：周量增幅超 +10% 上限被豁免，按用户要求执行")
        out["changes"] = {"kind": kind, "pace_zone": zone,
                          "distance_km": round(dist, 1), "duration_min": round(dur, 1)}
        if changes and changes.title:
            out["changes"]["title"] = changes.title
        if changes and changes.description:
            out["changes"]["description"] = changes.description
        return out

    if action == "shift":
        if taper:
            if not ctx.force:
                log.append(f"调整#{idx} 赛前 14 天不可挪课，被丢弃")
                return None
            log.append(f"调整#{idx} 强制模式：赛前 14 天内按用户要求挪课")
        if not changes or not changes.date:
            log.append(f"调整#{idx} 缺少目标日期，被丢弃")
            return None
        try:
            nd = date.fromisoformat(changes.date)
        except ValueError:
            log.append(f"调整#{idx} 目标日期格式错误，被丢弃")
            return None
        if not (ctx.today <= nd <= ctx.today + timedelta(days=6)):
            log.append(f"调整#{idx} 目标日期 {changes.date} 超出范围，被丢弃")
            return None
        if _day_occupied(state.by_date, nd):
            log.append(f"调整#{idx} 目标日期 {changes.date} 已有课，被丢弃")
            return None
        key = _wkey(workout)
        w = state.by_date.pop(key)
        w = dict(w, date=nd.isoformat())
        state.by_date[_wkey(w)] = w
        if not _adjacent_ok(state.by_date, nd):
            state.by_date.pop(_wkey(w), None)
            if not ctx.force:
                state.by_date[key] = dict(w, date=workout["date"])
                log.append(f"调整#{idx} 挪课后与相邻日强度课冲突，被丢弃")
                return None
            log.append(f"调整#{idx} 强制模式：目标日 {nd} 两侧已是强度日，{workout['kind']} 降为 E 轻松跑挪入")
            w = dict(w, date=nd.isoformat(), kind="E", pace_zone="E")
            state.by_date[_wkey(w)] = w
        return out

    if action == "add_easy":
        # 一天两练：changes.slot=2 表示落在当日已有课的第二时段（第一时段已存在）；
        # slot=1/缺省只允许落在空档日。
        slot = int(changes.slot) if changes and changes.slot in (1, 2) else 1
        day_w = [w for w in state.by_date.values() if w["date"] == d.isoformat()]
        if slot == 1:
            if day_w:
                log.append(f"调整#{idx} 加练日期 {d.isoformat()} 已有课，被丢弃")
                return None
        else:
            if not day_w:
                log.append(f"调整#{idx} 第二练需当日已有第一练，{d.isoformat()} 空档日被丢弃")
                return None
            if any((w.get("slot") or 1) == 2 for w in day_w):
                log.append(f"调整#{idx} 当日已有第二练，被丢弃")
                return None
            if len(day_w) >= 2:
                log.append(f"调整#{idx} 当日已有两练，被丢弃")
                return None
        if state.extras >= 2:
            log.append(f"调整#{idx} 本周加练已达 2 次上限，被丢弃")
            return None
        kind = (suggestion.kind if suggestion and suggestion.kind in EXTRA_KINDS else "E")
        dur = min(float(suggestion.duration_min) if suggestion else 30.0, 45.0)
        if d >= ctx.race_date - timedelta(days=2):
            if not ctx.force:
                log.append(f"调整#{idx} 赛前 3 天禁止加练，被丢弃")
                return None
            kind, dur = "RECOVERY", min(dur, 20.0)
            log.append(f"调整#{idx} 强制模式：赛前 3 天内加练按用户要求落地为 {dur:.0f} 分钟恢复跑")
        elif taper and kind != "RECOVERY":
            if not ctx.force:
                log.append(f"调整#{idx} 赛前 14 天只允许恢复跑加练，被丢弃")
                return None
            kind, dur = "RECOVERY", min(dur, 30.0)
            log.append(f"调整#{idx} 强制模式：赛前 14 天内加练按用户要求落地为 ≤{dur:.0f} 分钟恢复跑")
        e_pace = ctx.paces.get("E", {}).get("slow_s_km") or 360.0
        dist = round(dur * 60 / e_pace, 1)
        new_w = {
            "id": None, "date": d.isoformat(), "slot": slot, "kind": kind,
            "pace_zone": "E" if kind != "CROSS" else None,
            "distance_km": dist, "duration_min": dur, "status": "planned",
        }
        state.by_date[_wkey(new_w)] = new_w
        # 加练不计入周量 ±10% 检查（时长 ≤45min、每周 ≤2 次已单独约束）
        state.extras += 1
        out["changes"] = {"kind": kind, "duration_min": dur, "slot": slot,
                          "distance_km": dist, "pace_zone": "E" if kind != "CROSS" else None}
        return out

    log.append(f"调整#{idx} 未知动作 {action}，被丢弃")
    return None
