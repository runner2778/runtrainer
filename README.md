# SuperTrainer 跑训助手（RunTrainer）

Windows 桌面跑步训练助手：接入佳明（Garmin）手表数据，基于杰克·丹尼尔斯 VDOT 体系自动生成周期化课表，融合挪威双乳酸阈值等前沿方法，由 AI（DeepSeek）担任每日教练。

## 功能

- **课表引擎**：确定性规则生成（E/M/T/I/R/LR + 阈值/间歇/重复跑轮换），VDOT 配速表驱动，周期化（基础→早期→过渡→最终→减量），周跑量渐进 + 每 4 周 down week，容量上限护栏
- **一天两练**：可选每周 N 天二练（T 日挪威双乳酸阈值拆分 3×8′+5×5′，或强度课 + 傍晚放松跑）；**职业双练模式**效仿职业运动员——休息日轻松跑单练、其余每天两练，减量/比赛周自动恢复常规
- **力量课穿插**：每周 0–2 次力量课安排在轻松填充日（不占跑量、不进减量周）
- **AI 教练**（DeepSeek）：每日按睡眠/HRV/静息心率/压力给出建议（keep/modify/decrease/rest/add_easy），护栏校验 + 用户批准双保险；聊天式问答
- **能力预估**：综合近期比赛/手表 VO2max/间歇能力/配速-心率趋势（同配速心率下降 = 有氧进步），可自动微调课表 VDOT
- **Garmin 同步**：活动（含分段结构）+ 睡眠/HRV/压力/身体电量，365 天回填分批断点续传
- **手动导入**：FIT 文件（fitparse）/ CSV 模板，去重归档
- 日历按训练类型着色 + 完成状态；配速-心率对照趋势图；健康数据图表；黑红暗色主题

## 技术栈

Python 3.12+ · pywebview(Edge WebView2) · 原生 ES Modules + Alpine.js + ECharts 5（全本地无 CDN）· SQLite(WAL) · garminconnect（非官方 API，可替换 adapter）· DeepSeek（OpenAI 兼容）

## 目录结构

```
src/runtrainer/
├─ app.py / config.py        # webview 窗口、静态服务、%APPDATA%\RunTrainer 数据目录
├─ db/                       # 短连接 + migrations/000N_*.sql + repos/
├─ domain/                   # 纯函数层：vdot / plan_engine / workout_catalog / ability / load_metrics / guardrails
├─ garmin/                   # adapter 隔离：garminconnect / mock / fit / csv / sync_service
├─ ai/                       # deepseek_client / prompt_builder / contracts / coach_service
├─ services/                 # plan_service / settings_service（凭据仅存 Windows 凭据管理器）
├─ api/bridge.py             # JS 唯一入口，统一 {ok,data,error} 信封
└─ utils/
web/                         # index.html + css + js/pages + vendor
tests/                       # pytest（VDOT 锚点、引擎不变量、护栏、FIT 解析、导入去重、教练流）
tools/                       # 开发/验证脚本（重建计划、UI 验证、数据回填等）
```

## 开发

```powershell
.venv\Scripts\python.exe -m pytest -q      # 测试（数据目录自动隔离）
.venv\Scripts\pythonw.exe -m runtrainer    # 运行
```

## 安全

- Garmin 密码与 DeepSeek API key **只存 Windows 凭据管理器（keyring）**，绝不落库/落日志/入仓库
- 真实数据位于 `%APPDATA%\RunTrainer`（仓库不包含任何个人数据）
- AI 输出经护栏（容量上限/相邻强度/周量 ±10%/赛前规则）校验后方可生效
