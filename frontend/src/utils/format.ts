/**
 * 展示格式化工具（模块 3 列表页与详情页共用）。
 *
 * ★ 一条纪律：**所有涨跌/盈亏着色都走 `pnlClass()`**。
 *   中国市场惯例是「涨红跌绿」，与欧美相反。散在各处写三元表达式，
 *   迟早有一处写反 —— 而写反的颜色比不写着色更糟（会把亏损看成盈利）。
 */

/** 空值统一显示为长破折号（区分"没有这个指标"与"指标为 0"） */
export const DASH = '—'

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  return `${(v * 100).toFixed(digits)}%`
}

/** 百分比但带正负号（用于收益/盈亏，一眼看出方向） */
export function fmtPctSigned(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  const s = (v * 100).toFixed(digits)
  return v > 0 ? `+${s}%` : `${s}%`
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  return v.toFixed(digits)
}

export function fmtMoney(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  return `¥${v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
}

/** 带正负号的金额 */
export function fmtMoneySigned(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH
  const body = Math.abs(v).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
  return v < 0 ? `-¥${body}` : `+¥${body}`
}

/** `2026-09-18 10:39:50` → `2026-09-18 10:39` */
export function fmtDateTime(s: string | null | undefined): string {
  if (!s) return DASH
  return s.replace('T', ' ').slice(0, 16)
}

/** `20260918-101429-27dfa23f` → 保持原样（它是 ID，不是给人读的） */
export function fmtRunId(runId: string): string {
  return runId || DASH
}

/**
 * 涨跌/盈亏着色类名。
 *
 * 0 视为「不变」用中性色 —— 把 0 涂成红色会让人以为赚了。
 */
export function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v) || v === 0) return ''
  return v > 0 ? 'text-up' : 'text-down'
}

/** 状态 → el-tag 类型 */
export function statusTagType(
  status: string,
): 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
    success: 'success',
    running: 'warning',
    failed: 'danger',
    incomplete: 'info',
  }
  return map[status] ?? 'info'
}

export function statusLabel(status: string): string {
  const map: Record<string, string> = {
    success: '已完成',
    running: '运行中',
    failed: '失败',
    incomplete: '产物不全',
  }
  return map[status] ?? status
}

export function fmtBytes(n: number | null | undefined): string {
  if (!n || n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let size = n
  let i = 0
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024
    i += 1
  }
  return `${size.toFixed(1)} ${units[i]}`
}

/** 导出 CSV（带 BOM，否则 Excel 打开中文是乱码） */
export function downloadCsv(filename: string, rows: Array<Array<string | number | null>>): void {
  const body = rows
    .map((row) =>
      row
        .map((cell) => {
          const s = cell === null || cell === undefined ? '' : String(cell)
          // 含逗号/引号/换行时必须加引号并转义
          return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
        })
        .join(','),
    )
    .join('\r\n')

  const blob = new Blob(['\uFEFF' + body], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
