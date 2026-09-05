"""garminconnect 库适配器：个人账号凭据访问 Garmin Connect 数据。

注意：garminconnect 是非官方库，接口与字段均可能随 Garmin 服务端变化而失效。
所有解析均为防御式（字段缺失 → None），失败统一抛 AdapterError 由上层降级处理。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from ..utils import dates
from .adapter import AdapterError, GarminAdapter, RawActivity, RawDailyHealth

log = logging.getLogger(__name__)


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_local_dt(s: str) -> datetime | None:
    """'2026-09-01 06:30:00'（设备本地时间，无时区）→ 本地 datetime。"""
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _parse_laps(raw: list) -> list[dict]:
    """splitSummaries → 规范圈数据 [{distance_m, elapsed_s, avg_hr, pace_s_km}]。

    字段名随 Garmin 服务端变化，全部防御式解析；无效圈丢弃。
    splitSummaries 常缺 duration 但带 averageSpeed：用距离/速度反推时长，
    否则间歇课的休息/快跑时长比无从计算。
    """
    laps = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        dist = _num(_first(it, "distance", "totalDistance"))
        dur = _num(_first(it, "duration", "movingDuration", "totalDuration"))
        pace = None
        if dist and dur:
            pace = dur / (dist / 1000.0)
        elif _num(_first(it, "averageSpeed", "avgSpeed")):
            pace = 1000.0 / _num(_first(it, "averageSpeed", "avgSpeed"))
            if dist and not dur:
                dur = dist * pace / 1000.0
        if not dist and not dur:
            continue
        laps.append({
            "distance_m": dist,
            "elapsed_s": dur,
            "avg_hr": _num(_first(it, "averageHR", "avgHR", "averageHeartRate")),
            "pace_s_km": round(pace, 1) if pace else None,
            "split_type": str(_first(it, "splitType")) if _first(it, "splitType") else None,
        })
    return laps


class GarminConnectAdapter(GarminAdapter):
    name = "garmin"

    def __init__(self, username: str, password: str, is_cn: bool = True):
        from garminconnect import Garmin
        self._client = Garmin(username, password, is_cn=is_cn)
        self._username = username

    def login(self, username: str | None = None, password: str | None = None) -> None:
        try:
            self._client.login()
        except Exception as e:
            # Cloudflare 防护/认证失败/网络问题统一归类
            raise AdapterError(f"Garmin 登录失败：{e}") from e

    def fetch_profile(self) -> dict:
        try:
            p = self._client.get_user_profile()
        except Exception as e:
            raise AdapterError(f"获取 Garmin 档案失败：{e}") from e
        # 中国区 API 结构：昵称在活动的 ownerFullName 上，档案在 userData 子对象里
        ud = _first(p, "userData") or {}
        birth = _first(ud, "birthDate")
        year = None
        if isinstance(birth, str) and len(birth) >= 4:
            year = int(birth[:4])
        nickname = _first(ud, "displayName", "userName", "fullName") or self._fetch_owner_name()
        return {
            "nickname": nickname,
            "sex": "male" if str(_first(ud, "gender", default="")).upper() == "MALE" else
                   ("female" if str(_first(ud, "gender", default="")).upper() == "FEMALE" else None),
            "birth_year": year,
            "height_cm": _num(_first(ud, "height")),
            "weight_kg": _num(_first(ud, "weight")) / 1000 if _num(_first(ud, "weight")) else None,
            "vo2max": _num(_first(ud, "vo2MaxRunning")),
            "max_hr": None,  # 后续可经 get_max_metrics 或用户手动补充
            "rest_hr": None,
        }

    def _fetch_owner_name(self) -> str | None:
        """昵称不在档案里时，从最近一条活动取 ownerFullName（Garmin 显示名）。"""
        try:
            acts = self._client.get_activities(0, 1) or []
            return acts[0].get("ownerFullName") if acts else None
        except Exception as e:  # 昵称是软字段，拿不到也不影响同步
            log.debug("取 ownerFullName 失败: %s", e)
            return None

    def fetch_activities(self, since: datetime, limit: int = 100) -> list[RawActivity]:
        """拉取 since 以来的全部跑步活动（Garmin 列表按时间倒序分页）。

        get_activities(start, limit) 的 start 是页偏移——必须逐页翻到
        时间早于 since 为止，否则只会拿到最近一页（历史被静默截断）。
        """
        result = []
        since_ts = int(since.timestamp())
        offset = 0
        pages = 0
        while True:
            try:
                items = self._client.get_activities(offset, limit) or []
            except Exception as e:
                raise AdapterError(f"获取活动列表失败：{e}") from e
            if not items:
                break
            pages += 1
            oldest_ts = None
            for it in items:
                # 中国区 API 的字段名是 activityType（type 为 null）
                t = _first(it, "type", "activityType")
                type_key = _first(t, "typeKey") if isinstance(t, dict) else None
                if type_key not in ("running", "treadmill_running", "track_running", "trail_running"):
                    continue  # 本应用只关注跑步
                start_local = _parse_local_dt(_first(it, "startTimeLocal"))
                if start_local is None:
                    continue
                start_ts = int(start_local.astimezone(dates.local_tz()).timestamp())
                if oldest_ts is None or start_ts < oldest_ts:
                    oldest_ts = start_ts
                if start_ts < since_ts:
                    continue
                dur = _num(_first(it, "duration"))
                dist = _num(_first(it, "distance"))
                result.append(RawActivity(
                    external_id=str(_first(it, "activityId")),
                    name=str(_first(it, "activityName") or f"{type_key} 活动"),
                    sport="running",
                    start_ts=start_ts,
                    tz_offset_min=int(dates.local_tz().utcoffset(start_local).total_seconds() // 60),
                    duration_s=dur,
                    distance_m=dist,
                    avg_pace_s_km=(dur / (dist / 1000.0)) if dur and dist else None,
                    avg_hr=_num(_first(it, "averageHR")),
                    max_hr=_num(_first(it, "maxHR")),
                    avg_cadence=_num(_first(it, "averageRunningCadenceInStepsPerMinute")),
                    max_cadence=_num(_first(it, "maxRunningCadenceInStepsPerMinute")),
                    stride_length_m=_num(_first(it, "avgStrideLength", "averageStrideLength")),
                    aerobic_te=_num(_first(it, "aerobicTrainingEffect")),
                    anaerobic_te=_num(_first(it, "anaerobicTrainingEffect")),
                    exercise_load=_num(_first(it, "exerciseLoad")),
                    elevation_gain_m=_num(_first(it, "elevationGain")),
                    elevation_loss_m=_num(_first(it, "elevationLoss")),
                    calories=int(_first(it, "calories")) if _first(it, "calories") else None,
                    laps=_parse_laps(_first(it, "splitSummaries", default=[])),
                ))
            if len(items) < limit:
                break  # 最后一页
            if oldest_ts is not None and oldest_ts < since_ts:
                break  # 已翻过 since 时间点，更早的页面不再需要
            offset += limit
            if pages > 100:
                log.warning("活动翻页超过 100 页，停止（异常数据保护）")
                break
        return result

    def fetch_activity_detail(self, external_id: str) -> RawActivity:
        try:
            summary = self._client.get_activity(int(external_id))
            detail = self._client.get_activity_details(int(external_id))
        except Exception as e:
            raise AdapterError(f"获取活动详情失败：{e}") from e
        # 中国区单活动响应把概要字段放在 summaryDTO 子对象里，顶层没有
        # duration/distance——之前按顶层解析得到 None 并把已存数据覆盖清空
        s = _first(summary, "summaryDTO") or {}
        if not isinstance(s, dict):
            s = {}
        def _sf(*keys):
            return _first(s, *keys) if s else _first(summary, *keys)
        dur = _num(_sf("duration", "movingDuration"))
        dist = _num(_sf("distance"))
        avg_hr = _num(_sf("averageHR"))
        if avg_hr is None:
            # summaryDTO 缺 averageHR 时用各 split 均值兜底
            hrs = [x["avg_hr"] for x in _parse_laps(_first(summary, "splitSummaries", default=[]))
                   if x.get("avg_hr")]
            if hrs:
                avg_hr = round(sum(hrs) / len(hrs), 1)
        samples = self._normalize_samples(self._parse_detail_metrics(detail))
        return RawActivity(
            external_id=external_id,
            name=str(_first(summary, "activityName") or "活动"),
            sport="running",
            start_ts=int(dates.date_to_ts(date.today())),  # 占位，上层以已存活动为准
            tz_offset_min=480,
            duration_s=dur,
            distance_m=dist,
            avg_pace_s_km=(dur / (dist / 1000.0)) if dur and dist else None,
            avg_hr=avg_hr,
            max_hr=_num(_sf("maxHR")),
            avg_cadence=_num(_sf("averageRunningCadenceInStepsPerMinute")),
            max_cadence=_num(_sf("maxRunningCadenceInStepsPerMinute")),
            stride_length_m=_num(_sf("avgStrideLength", "averageStrideLength")),
            aerobic_te=_num(_sf("aerobicTrainingEffect")),
            anaerobic_te=_num(_sf("anaerobicTrainingEffect")),
            exercise_load=_num(_sf("exerciseLoad")),
            elevation_gain_m=_num(_sf("elevationGain")),
            calories=int(_sf("calories")) if _sf("calories") else None,
            laps=_parse_laps(_first(summary, "splitSummaries", default=[])),
            samples=samples,
        )

    def _parse_detail_metrics(self, detail: dict) -> list[dict]:
        """activityDetailMetrics 采样行解析（兼容 CN 区与国际区形态）。

        CN 区（实测）：顶层 metricDescriptors=[{metricsIndex, key, unit}]，
        activityDetailMetrics 为行对象列表 [{"metrics": [v,...]}, ...]，
        每行按 metricsIndex 对齐（约 2s 一行）。
        国际区新格式：adm.metrics 为 [{key, metrics:[...]}]（每列自带数据）。
        时间优先取 sumElapsedDuration（相对秒），缺列回退 directTimestamp。
        """
        d = detail or {}
        adm = _first(d, "activityDetailMetrics") or {}
        # CN 区：activityDetailMetrics 本身就是行对象列表
        # [{"metrics": [v,...]}, ...]；国际区是 {"metrics": [...]} 字典
        if isinstance(adm, list):
            rows = adm
        elif isinstance(adm, dict):
            rows = _first(adm, "metrics", default=[]) or []
        else:
            return []
        if not isinstance(rows, (list, tuple)) or not rows:
            return []
        # 国际区新格式：每列是 {key, metrics:[...]}
        if all(isinstance(m, dict) and m.get("key") and m.get("metrics") is not None
               for m in rows):
            cols = {str(m["key"]): m.get("metrics") for m in rows}
            n = min((len(v) for v in cols.values()
                     if isinstance(v, (list, tuple))), default=0)
            return [self._sample_from_cols(cols, i) for i in range(n)]
        # CN/老格式：描述符定义列，行是值列表（CN 行为 {"metrics": [...]}）
        descriptors = (_first(d, "metricDescriptors", default=[]) or
                       _first(adm, "metricDescriptors", default=[]) or [])
        idx: dict[str, int] = {}
        for md in descriptors:
            if not isinstance(md, dict) or not md.get("key"):
                continue
            pos = md.get("metricsIndex", md.get("index"))
            if pos is None:
                continue
            idx[str(md["key"])] = int(pos)
        if not idx and descriptors:
            idx = {str(md.get("key")): i for i, md in enumerate(descriptors)
                   if isinstance(md, dict) and md.get("key")}
        ts_key = "sumElapsedDuration" if "sumElapsedDuration" in idx else "directTimestamp"
        samples = []
        for row in rows:
            values = row.get("metrics") if isinstance(row, dict) else row
            if not isinstance(values, (list, tuple)):
                continue
            def val(key):
                i = idx.get(key)
                return _num(values[i]) if i is not None and i < len(values) else None
            samples.append({
                "t_offset_s": val(ts_key),
                "hr": val("directHeartRate"),
                "speed_mps": val("directSpeed"),
                "cadence": val("directRunCadence"),
                "altitude_m": val("directElevation"),
            })
        return samples

    @staticmethod
    def _sample_from_cols(cols: dict, i: int) -> dict:
        def val(key):
            v = cols.get(key)
            return _num(v[i]) if isinstance(v, (list, tuple)) and i < len(v) else None
        return {"t_offset_s": val("directTimestamp"), "hr": val("directHeartRate"),
                "speed_mps": val("directSpeed"), "cadence": val("directCadence"),
                "altitude_m": val("directElevation")}

    @staticmethod
    def _normalize_samples(samples: list[dict]) -> list[dict]:
        """时间戳归一：毫秒→秒、相对起点归零、按时间排序；无时间戳则按序编号。"""
        ts = [s.get("t_offset_s") for s in samples if s.get("t_offset_s") is not None]
        if ts and min(ts) > 1e11:  # epoch 毫秒（1.7e12）而非秒（1.7e9）
            for s in samples:
                if s.get("t_offset_s") is not None:
                    s["t_offset_s"] /= 1000.0
            ts = [s.get("t_offset_s") for s in samples if s.get("t_offset_s") is not None]
        if ts:
            t0 = min(ts)
            for s in samples:
                if s.get("t_offset_s") is not None:
                    s["t_offset_s"] = round(s["t_offset_s"] - t0, 1)
        else:
            for i, s in enumerate(samples):
                s["t_offset_s"] = float(i)
        samples.sort(key=lambda s: s["t_offset_s"])
        return samples

    def fetch_daily_health(self, start: date, end: date) -> list[RawDailyHealth]:
        result = []
        d = start
        while d <= end:
            result.append(self._fetch_day_health(d))
            d += timedelta(days=1)
        return result

    def _fetch_day_health(self, d: date) -> RawDailyHealth:
        iso = d.isoformat()
        sleep_data = hrv_data = stress_data = summary = None
        try:
            sleep_data = self._client.get_sleep_data(iso)
        except Exception as e:
            log.debug("sleep 拉取失败 %s: %s", iso, e)
        try:
            hrv_data = self._client.get_hrv_data(iso)
        except Exception as e:
            log.debug("hrv 拉取失败 %s: %s", iso, e)
        try:
            stress_data = self._client.get_stress_data(iso)
        except Exception as e:
            log.debug("stress 拉取失败 %s: %s", iso, e)
        try:
            summary = self._client.get_user_summary(iso)
        except Exception as e:
            log.debug("summary 拉取失败 %s: %s", iso, e)
        if all(x is None for x in (sleep_data, hrv_data, stress_data, summary)):
            raise AdapterError(f"{iso} 健康数据全部拉取失败（可能被限流）")

        dto = _first(sleep_data, "dailySleepDTO") or {}
        score = _first(dto, "sleepScore")
        score_val = None
        if isinstance(score, dict):
            score_val = _num(_first(score, "value", "optionalSleepScoreValue"))
        elif score is not None:
            score_val = _num(score)
        hrv_sum = _first(hrv_data, "hrvSummary") or {}
        return RawDailyHealth(
            date=d,
            sleep_start_ts=_num(_first(sleep_data, "sleepStartTimestampGMT")),
            sleep_end_ts=_num(_first(sleep_data, "sleepEndTimestampGMT")),
            sleep_duration_s=_num(_first(dto, "sleepTimeSeconds")),
            deep_s=_num(_first(dto, "deepSleepSeconds")),
            light_s=_num(_first(dto, "lightSleepSeconds")),
            rem_s=_num(_first(dto, "remSleepSeconds")),
            awake_s=_num(_first(dto, "awakeSleepSeconds")),
            sleep_score=score_val,
            resting_hr=_num(_first(summary, "restingHeartRate")),
            avg_hr=_num(_first(summary, "averageHR")),
            max_hr=_num(_first(summary, "maxHeartRate")),
            hrv_avg_ms=_num(_first(hrv_sum, "lastNightAvg", "weeklyAvg")),
            hrv_status=str(_first(hrv_sum, "status")).lower() if _first(hrv_sum, "status") else None,
            stress_avg=_num(_first(stress_data, "avgStressLevel")),
            body_battery_min=_num(_first(summary, "bodyBatteryLowestValue")),
            body_battery_max=_num(_first(summary, "bodyBatteryHighestValue")),
            steps=int(_first(summary, "totalSteps")) if _first(summary, "totalSteps") else None,
            raw={"sleep": bool(sleep_data), "hrv": bool(hrv_data),
                 "stress": bool(stress_data), "summary": bool(summary)},
        )
