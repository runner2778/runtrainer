"""截图识别：调用 Windows 自带 OCR（Windows.Media.Ocr）把成绩截图读成字段。

不引入任何第三方依赖——Windows 10/11 自带 OCR 引擎，通过 PowerShell 的
WinRT 投影调用（脚本内嵌在本模块，运行时落盘，打包后同样可用）。
识别结果是「预填」，最终以用户确认为准：界面必须回显原始识别文本。
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import unicodedata
from pathlib import Path

from .. import config
from ..domain import activity_edit as ae

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")
OCR_TIMEOUT_S = 45

# 「认出了成绩」的判据：只有 name 不算（OCR 总能把标题行读出来，若拿它当成功
# 判据，一张风景照配个字也能「识别成功」并预填一个空表单）。
_MEASURE_FIELDS = frozenset({
    "distance_m", "duration_s", "avg_pace_s_km", "avg_hr", "avg_cadence",
    "stride_length_m", "calories", "feel", "rpe",
})

# PowerShell 5.1 + WinRT 投影（纯 ASCII）。WinRT 的 IAsyncOperation 不能直接
# 等待，用 System.WindowsRuntimeSystemExtensions.AsTask 反射出一个 Await。
_PS_SCRIPT = r"""
param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $type) {
    $m = $asTaskGeneric.MakeGenericMethod($type)
    $t = $m.Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { [Console]::Error.WriteLine('NO_OCR_ENGINE'); exit 2 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) { Write-Output $line.Text }
"""

_PUNCT_MAP = {
    "。": ".", "．": ".", "·": ".", "•": ".", "、": ",",
    "：": ":", "；": ";", "，": ",", "！": "!", "？": "?",
    "“": '"', "”": '"', "‘": "'", "’": "'", "′": "'", "″": '"',
    "（": "(", "）": ")", "－": "-", "–": "-", "—": "-", "～": "~",
    "／": "/", "＇": "'", "％": "%",
}

# 「一个数 + 单位」，单位允许 OCR 抖动（公里/千米/km/km/h 都算）
_KM = r"(?:km|KM|Km|公里|千米|㎞)"
_BPM = r"(?:bpm|BPM|次/分|次分|心率)"
_SPM = r"(?:spm|SPM|步/分|步频|步分)"
_KCAL = r"(?:kcal|Kcal|千卡|大卡|卡路里|卡)"
_M = r"(?:m|M|米)"
_MIN = r"(?:分钟|min|MIN|分)"

_TIME_HMS = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})\b")
_TIME_MS = re.compile(r"\b(\d{1,3}):(\d{2})\b")
# 配速的分钟/秒分隔符：真机实测 Windows OCR 会把 4'59" 读成 4.59 / 4。59 / 4·59，
# 所以 ' : . " · 都当分隔符。范围守卫（120–900 s/km）会挡掉 10.52 / 1.14 这类小数。
_PACE = re.compile(r"\b(\d{1,2})[:'\".,·](\d{2})\b")
_PACE_LABEL = re.compile(r"/\s*km|每公里|配速|pace", re.IGNORECASE)
_UNIT_ONLY = re.compile(rf"^\s*(?:{_KM}|{_M})\s*$", re.IGNORECASE)
_BARE_NUM = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})?)\s*$")


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _labeled_value(lines: list[str], label_re: re.Pattern, value_re: re.Pattern,
                   lookahead: int = 2) -> re.Match | None:
    """在「标签行」及其后 N 行里找值。

    截图排版常把标签与数值分成两行（`Avg Pace` / `km` / `4.59" /km`），
    纯全局正则会串行误配，所以先按标签定位再看邻近行。
    """
    for i, ln in enumerate(lines):
        if not label_re.search(ln):
            continue
        m = value_re.search(ln)
        if m:
            return m
        for j in range(i + 1, min(i + 1 + lookahead, len(lines))):
            m = value_re.search(lines[j])
            if m:
                return m
    return None


def _distance_from_unit_line(lines: list[str]) -> tuple[float, str] | None:
    """单位单独成行时（真机 OCR 实测：「10 · 52 / Time / 52:31 / Avg Pace / km」），
    从「km」那行往回找最近的一个纯数字行当距离。"""
    for i, ln in enumerate(lines):
        if not _UNIT_ONLY.match(ln):
            continue
        for j in range(i - 1, max(-1, i - 5), -1):
            m = _BARE_NUM.match(lines[j])
            if m:
                v = float(m.group(1))
                if 0.5 <= v <= 250:
                    return v, lines[j] + " " + ln
    return None


def normalize_text(text: str) -> str:
    """OCR 原文 → 可正则的规范文本。

    处理三类噪声：全角字符（NFKC）、中英文标点混读、数字与小数点之间被 OCR
    拆出的空格（实测「10.52」→「10 · 52」、「52:31」→「52 ： 31」）。
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = "".join(_PUNCT_MAP.get(ch, ch) for ch in s)
    s = re.sub(r"(?<=\d)[ \t]+(?=[\d:.'\",])", "", s)
    s = re.sub(r"(?<=[\d:.'\",])[ \t]+(?=\d)", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# 数值行的特征：带时间 `52:36`，或「数字+计量单位」。这类行是数据不是标题，
# 不能被当成活动名（否则「用时 50:00」会变成活动名，看着像识别错了）。
_DATA_LINE = re.compile(
    rf"\d{{1,3}}:\d{{2}}|\d[ \t]*(?:{_KM}|{_BPM}|{_SPM}|{_KCAL}|{_MIN}|{_M})\b",
    re.IGNORECASE)


def _looks_like_data_line(ln: str) -> bool:
    return bool(_DATA_LINE.search(ln))


def parse_activity_text(raw: str) -> dict:
    """从识别文本里抽出活动字段。返回 {fields, matched, warnings}。

    fields 直接可喂给 activity_edit.build_row；matched 记录每个字段命中的
    原文片段（界面回显「识别到：…」）；warnings 列出存疑之处。
    """
    text = normalize_text(raw)
    lines = _lines(text)
    fields: dict = {}
    matched: dict[str, str] = {}
    warnings: list[str] = []

    def find(pat: str) -> re.Match | None:
        return re.search(pat, text, re.IGNORECASE)

    # --- 距离 ---
    # 数字与单位之间只允许同行空白（[ \t]* 而非 \s*）：用 \s* 会跨行走，
    # 真机实测「用时 50:00\n心率 300」里 "00" 会被当成心率值、单位取到下一行的
    # 「心率」→ 读出 avg_hr=0。
    m = find(rf"(\d{{1,3}}(?:\.\d{{1,3}})?)[ \t]*{_KM}")
    if m:
        fields["distance_m"] = round(float(m.group(1)) * 1000, 1)
        matched["distance_m"] = " ".join(m.group(0).split())
    else:
        unit_line = _distance_from_unit_line(lines)
        if unit_line:
            fields["distance_m"] = round(unit_line[0] * 1000, 1)
            matched["distance_m"] = unit_line[1]
        else:
            m = find(rf"(\d{{1,3}}[,.]?\d{{0,3}})[ \t]*{_M}\b")
            if m and float(m.group(1).replace(",", "")) >= 500:
                fields["distance_m"] = round(float(m.group(1).replace(",", "")), 1)
                matched["distance_m"] = m.group(0).strip()

    # --- 用时：先 h:mm:ss，再 mm:ss ---
    m = _TIME_HMS.search(text)
    if m:
        fields["duration_s"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                + int(m.group(3)))
        matched["duration_s"] = m.group(0)
    else:
        m = _TIME_MS.search(text)
        if m:
            fields["duration_s"] = int(m.group(1)) * 60 + int(m.group(2))
            matched["duration_s"] = m.group(0)
        else:
            m = find(rf"(\d{{1,3}})[ \t]*{_MIN}")
            if m:
                fields["duration_s"] = int(m.group(1)) * 60
                matched["duration_s"] = m.group(0).strip()

    # --- 配速：优先带 /km 或 配速 标签的行，避免把用时/距离当配速 ---
    def _pace_candidates() -> list[re.Match]:
        out: list[re.Match] = []
        for ln in lines:
            if not _PACE_LABEL.search(ln):
                continue
            for mm in _PACE.finditer(ln):
                out.append(mm)
        if not out:
            # 无标签时的兜底：跳过长得像「距离」「用时」的行，否则「10.00 km」
            # 里的 10.00 会被读成 10:00/km（600 s/km 恰好在合法域内，守卫拦不住）
            for ln in lines:
                if re.search(rf"{_KM}|{_BPM}|{_SPM}", ln, re.IGNORECASE):
                    continue
                for mm in _PACE.finditer(ln):
                    out.append(mm)
        return out

    pace_m = None
    for cand in _pace_candidates():
        secs = int(cand.group(1)) * 60 + int(cand.group(2))
        if cand.group(0) == matched.get("duration_s"):
            continue
        if 120 <= secs <= 900:
            pace_m = cand
            break
    if pace_m:
        secs = int(pace_m.group(1)) * 60 + int(pace_m.group(2))
        fields["avg_pace_s_km"] = secs
        matched["avg_pace_s_km"] = pace_m.group(0)

    # --- 心率 ---
    m = find(rf"(\d{{2,3}})[ \t]*{_BPM}")
    if m:
        fields["avg_hr"] = float(m.group(1))
        matched["avg_hr"] = m.group(0).strip()

    # --- 步频 ---
    m = find(rf"(\d{{2,3}})[ \t]*{_SPM}")
    if m:
        fields["avg_cadence"] = float(m.group(1))
        matched["avg_cadence"] = m.group(0).strip()

    # --- 步幅 ---
    m = find(rf"(\d\.\d{{1,2}})[ \t]*{_M}\b")
    if m:
        v = _num(m.group(1))
        if v and 0.5 <= v <= 2.5:
            fields["stride_length_m"] = v
            matched["stride_length_m"] = m.group(0).strip()

    # --- 热量 ---
    m = find(rf"(\d{{2,4}})[ \t]*{_KCAL}")
    if m:
        fields["calories"] = int(m.group(1))
        matched["calories"] = m.group(0).strip()

    # --- 主观感受 / 用力程度 ---
    m2 = find(r"(?:感受|感觉|体感|feel)[^\d\n]{0,6}(\d{1,3})")
    if m2:
        fields["feel"] = ae.norm_feel(m2.group(1))
        matched["feel"] = m2.group(0).strip()
    m3 = find(r"(?:RPE|用力|强度|费力)[^\d\n]{0,6}(\d{1,3})")
    if m3:
        fields["rpe"] = ae.norm_rpe(m3.group(1))
        matched["rpe"] = m3.group(0).strip()

    # --- 名称：第一行若有中文/字母且不像数值行，当标题 ---
    for ln in lines[:3]:
        if _looks_like_data_line(ln):
            continue
        if not re.fullmatch(r"[\d:.'\",]+", ln):
            fields["name"] = ln[:40]
            matched["name"] = ln[:40]
            break

    if "distance_m" not in fields and "duration_s" not in fields:
        warnings.append("没识别到距离或用时，请手动补全")
    if fields.get("avg_hr") and not (60 <= fields["avg_hr"] <= 230):
        warnings.append(f"心率 {fields['avg_hr']:.0f} 不合常理，请核对")
        fields.pop("avg_hr", None)
    if fields.get("distance_m") and fields.get("duration_s"):
        pace = ae.derive_pace(fields["distance_m"], fields["duration_s"])
        if fields.get("avg_pace_s_km") and pace \
                and abs(pace - fields["avg_pace_s_km"]) > 90:
            warnings.append(
                f"配速与「距离/用时」不一致（算得 {int(pace) // 60}:{int(pace) % 60:02d}），"
                "已按距离与用时为准")
        if pace:
            fields["avg_pace_s_km"] = pace
    return {"fields": fields, "matched": matched, "warnings": warnings,
            "raw_text": raw, "normalized": text}


def _script_path() -> Path:
    """把内嵌脚本落到数据目录（内容变了才重写），打包后同样可用。"""
    p = config.DATA_DIR / "ocr_win.ps1"
    body = _PS_SCRIPT.lstrip("\n")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    stamp = p.with_suffix(".ps1.sha")
    try:
        if p.exists() and stamp.exists() and stamp.read_text().strip() == digest:
            return p
    except OSError:
        pass
    config.ensure_dirs()
    p.write_text(body, encoding="ascii")
    try:
        stamp.write_text(digest)
    except OSError:
        pass
    return p


def recognize_text(image_path: str | Path) -> str:
    """调用 Windows OCR 返回识别文本。失败抛 RuntimeError（调用方转成人话）。"""
    path = Path(image_path)
    if not path.exists():
        raise RuntimeError(f"图片不存在：{path}")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise RuntimeError(f"不支持的图片格式：{path.suffix}")
    script = _script_path()
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(script), str(path)],
            capture_output=True, timeout=OCR_TIMEOUT_S,
        )
    except FileNotFoundError as e:
        raise RuntimeError("本机找不到 powershell.exe，无法识别截图") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("识别超时，请换一张更小/更清晰的截图") from e
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        if "NO_OCR_ENGINE" in err:
            raise RuntimeError("系统没有可用的 OCR 语言包（设置 → 时间和语言 → 语言）")
        log.warning("OCR 退出码 %s：%s", proc.returncode, err[:400])
        raise RuntimeError(f"识别失败：{err[:200] or '未知错误'}")
    return out


def recognize_activity(image_path: str | Path) -> dict:
    """截图 → 字段。识别不出任何字段时抛 RuntimeError，避免静默写垃圾数据。"""
    raw = recognize_text(image_path)
    if not raw.strip():
        raise RuntimeError("这张图里没读到文字（尽量截跑步 App 的成绩页，别截太糊）")
    parsed = parse_activity_text(raw)
    parsed["image"] = str(image_path)
    if not (_MEASURE_FIELDS & set(parsed["fields"])):
        raise RuntimeError("读到了文字但认不出成绩字段，请手动填写")
    return parsed
