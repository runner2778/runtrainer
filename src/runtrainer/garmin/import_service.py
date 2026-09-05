"""手动导入编排：FIT/CSV → RawActivity → 归档原文件 → DB upsert（去重）。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .. import config
from ..db.repos import activity_repo
from ..utils import filehash
from .adapter import RawActivity
from .csv_importer import parse_csv
from .fit_importer import parse_fit

log = logging.getLogger(__name__)


def _raw_to_row(a: RawActivity, source: str, external_id: str, file_path: str | None) -> dict:
    return {
        "source": source,
        "external_id": external_id,
        "file_path": file_path,
        "name": a.name,
        "sport": a.sport,
        "start_ts": a.start_ts,
        "tz_offset_min": a.tz_offset_min,
        "duration_s": a.duration_s,
        "distance_m": a.distance_m,
        "avg_pace_s_km": a.avg_pace_s_km,
        "avg_hr": a.avg_hr,
        "max_hr": a.max_hr,
        "avg_cadence": a.avg_cadence,
        "max_cadence": a.max_cadence,
        "elevation_gain_m": a.elevation_gain_m,
        "elevation_loss_m": a.elevation_loss_m,
        "calories": a.calories,
        "laps_json": __import__("json").dumps(a.laps, ensure_ascii=False) if a.laps else None,
        "has_samples": 1 if a.samples else 0,
    }


def _persist(activity: RawActivity, source: str, external_id: str,
             file_path: str | None) -> tuple[int, bool]:
    row = _raw_to_row(activity, source, external_id, file_path)
    aid, created = activity_repo.upsert_activity(row)
    if activity.samples:
        activity_repo.save_samples(aid, [
            (s.get("t_offset_s"), s.get("hr"), s.get("speed_mps"), s.get("cadence"), s.get("altitude_m"))
            for s in activity.samples
        ])
    return aid, created


def _archive(path: Path) -> Path:
    """原文件归档到 %APPDATA%\\RunTrainer\\raw\\<sha256 前 16 位>_<原名>（数据目录沿用原名）。"""
    config.ensure_dirs()
    digest = filehash.sha256_file(path)[:16]
    dest = config.RAW_DIR / f"{digest}_{path.name}"
    if not dest.exists():
        shutil.copy2(path, dest)
    return dest


def import_fit_file(path: Path | str) -> dict:
    path = Path(path)
    activity = parse_fit(path)
    dest = _archive(path)
    digest = filehash.sha256_file(path)
    external_id = f"{digest[:16]}_{activity.start_ts}"
    aid, created = _persist(activity, "fit", external_id, str(dest))
    log.info("FIT 导入 %s (id=%s, 新建=%s)", path.name, aid, created)
    return {"activity_id": aid, "created": created, "name": activity.name}


def import_csv_file(path: Path | str) -> list[dict]:
    path = Path(path)
    activities = parse_csv(path)
    dest = _archive(path)
    results = []
    for a in activities:
        digest = filehash.sha256_text(f"{a.start_ts}|{a.distance_m}|{a.duration_s}|{a.name}")
        external_id = f"{digest[:16]}_{a.start_ts}"
        aid, created = _persist(a, "csv", external_id, str(dest))
        results.append({"activity_id": aid, "created": created, "name": a.name})
    log.info("CSV 导入 %s 共 %s 条", path.name, len(results))
    return results


def import_files(paths: list[str]) -> dict:
    """批量导入，按扩展名路由。返回汇总。"""
    imported, skipped, errors = 0, 0, []
    for p in paths:
        path = Path(p)
        try:
            if path.suffix.lower() == ".fit":
                r = import_fit_file(path)
                imported += 1 if r["created"] else 0
                skipped += 0 if r["created"] else 1
            elif path.suffix.lower() == ".csv":
                for r in import_csv_file(path):
                    imported += 1 if r["created"] else 0
                    skipped += 0 if r["created"] else 1
            else:
                errors.append(f"{path.name}: 不支持的文件类型")
        except Exception as e:
            log.exception("导入失败 %s", p)
            errors.append(f"{path.name}: {e}")
    return {"imported": imported, "skipped": skipped, "errors": errors}
