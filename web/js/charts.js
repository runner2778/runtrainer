/** ECharts 工具：读取 CSS 设计令牌（跟随主题）、初始化与尺寸管理 */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function chartColors() {
  return {
    text: cssVar('--text'),
    muted: cssVar('--text-muted'),
    gridline: cssVar('--gridline'),
    bg: cssVar('--bg-card'),
    accent: cssVar('--accent'),
    good: cssVar('--st-good'),
    warning: cssVar('--st-warning'),
    serious: cssVar('--st-serious'),
    critical: cssVar('--st-critical'),
    kindE: cssVar('--kind-e'),
    kindM: cssVar('--kind-m'),
    kindT: cssVar('--kind-t'),
    kindI: cssVar('--kind-i'),
    kindR: cssVar('--kind-r'),
    // 时期对比线用独立色相序列（与训练类型色无关的语义）
    period: [cssVar('--kind-e'), cssVar('--kind-t'), cssVar('--kind-i'), cssVar('--kind-r'), cssVar('--kind-m')],
    ramp: [cssVar('--ramp-0'), cssVar('--ramp-1'), cssVar('--ramp-2'), cssVar('--ramp-3')],
  };
}

export function baseAxis(colors) {
  return {
    axisLine: { lineStyle: { color: colors.gridline } },
    // hideOverlap + auto interval：120 天的日期标签不再互相重叠压字
    axisLabel: { color: colors.muted, fontSize: 11, interval: 'auto', hideOverlap: true },
    splitLine: { lineStyle: { color: colors.gridline } },
    axisTick: { show: false },
  };
}

export function initChart(el, option) {
  const chart = echarts.init(el);
  chart.setOption(option);
  // 压缩变小 bug 修复：图表可能在页面隐藏时初始化（容器 0 尺寸），
  // 或窗口/网格重排时容器尺寸变化而 ECharts 不自知。ResizeObserver 在
  // 容器尺寸变化（含 display:none → 可见）时触发 resize 重新测量。
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => {
      if (!chart.isDisposed()) chart.resize();
    });
    ro.observe(el);
    el._chartResizeObserver = ro;
  }
  return chart;
}

export function disposeChart(el) {
  const c = echarts.getInstanceByDom(el);
  if (c) c.dispose();
  if (el._chartResizeObserver) {
    el._chartResizeObserver.disconnect();
    el._chartResizeObserver = null;
  }
}

export function resizeIn(container) {
  container.querySelectorAll('.chart, .chart-sm').forEach((el) => {
    const c = echarts.getInstanceByDom(el);
    if (c) c.resize();
  });
}

export function disposeIn(container) {
  container.querySelectorAll('.chart, .chart-sm').forEach((el) => {
    disposeChart(el);
  });
}

/** 通用 tooltip：浅色卡片，数值制表对齐 */
export function tooltip(colors, extra = {}) {
  return {
    trigger: 'axis',
    backgroundColor: colors.bg,
    borderColor: colors.gridline,
    textStyle: { color: colors.text, fontSize: 12 },
    extraCssText: 'box-shadow: 0 2px 8px rgba(0,0,0,.15); border-radius: 6px;',
    ...extra,
  };
}
