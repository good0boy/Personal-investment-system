/**
 * 「策略注释」子页的数据源 —— 策略设计逻辑 + 参数词典 + 运行 tag 解读。
 *
 * ⚠️ 这里是**面向阅读的转译**，不是规则依据：
 *   规则的唯一依据始终是两份规格书 ——
 *   · `策略规格说明书.md`（momentum v1.6）
 *   · `docs/策略规格说明书-v2.0-中频质量反转.md`（quality_reversal，文档内版本 v2.2）
 *   两者冲突时以规格书为准，并回来改这里。
 *
 * 参数含义的取值来源：规格书 §4（quality_reversal）/ §2（momentum），
 * 加上对 `config.json` 快照里实际出现过的键的核对。
 */

export interface ParamDoc {
  /** 中文名 */
  label: string
  /** 含义（含判定口径与关闭方式） */
  desc: string
}

export interface TagToken {
  token: string
  meaning: string
}

export interface StrategyNote {
  /** 一句话定位 */
  oneLiner: string
  /** 性质 */
  nature: string
  /** 为什么这么设计（依据规格书与回测研究） */
  designWhy: string[]
  /** 开仓结构（逐条） */
  entries: string[]
  /** 退出规则（逐条） */
  exits: string[]
  /** 打分/排序口径（可选） */
  scoring?: string[]
  /** 必须知道的结论与警告 */
  caveats: string[]
  /** 规则依据文档 */
  specDoc: string
}

// ======================================================================
// 策略级说明
// ======================================================================

export const STRATEGY_NOTES: Record<string, StrategyNote> = {
  quality_reversal: {
    oneLiner:
      '在上证指数近 20 个交易日未明显下跌时，买入沪深主板/创业板中' +
      '「近期温和偏弱（反转）+ 基本面成长为正 + 低波动 + 低换手 + 成交额达标」的股票，' +
      '每 5 个交易日评估一次建仓、最多持 10 只、每只 10% 仓位，持有满 30 个交易日即卖出' +
      '（另有 5 条提前退出）。',
    nature: '中频 · 逆向 + 质量 + 低风险（不是追涨）。',
    designWhy: [
      '信号方向反着做：v1.x 动量族多轮实验的根因是 alpha 在「隔夜」漏光 —— 买入强势股次日平均低开 1.8%（A 股短期反转），所以本策略买「没人要的」，方向与追涨完全相反。',
      'ret_50（近 50 日累计涨跌幅）分层单调性 −1.00，是全项目形状最干净的反向信号；volatility_20 单调性 −0.70、turnover_20 −0.60，方向一致。',
      'revenue_yoy / profit_yoy 是全部因子里唯二 IC 为正且单调 +1.00 的因子，但 ICIR 量级不够（当收益引擎太弱）→ 只当「质量门」过滤用。',
      '持有成本随频率急剧下降：持有 30 个交易日对应年化成本约 1.9%（实测 1.65%），是 v1.6 动量策略（7.8%）的约 1/4.7 —— 频率本身就是 alpha 杠杆。',
    ],
    entries: [
      '可交易性过滤（非 ST/停牌/新股不足 90 交易日/一字板/60 日禁买）由引擎股票池负责，策略侧不重复实现。',
      '门1 · 大盘非急跌（市场级）：上证近 20 个交易日累计涨跌幅 > index_ret20_min（默认 −10%）；不通过则当日不建仓（已持仓照常按退出规则走）。',
      '门2 · 反转门：ret_50 的截面分位 ≤ reversal_pct（默认 0.50，即只买近 250 日偏弱的一半）。',
      '门2b · 排除脉冲股：近 20 日最大单日涨幅 max_ret_20 ≤ 0.15（排除刚被爆拉过的票；无独立开关，只能调上限）。',
      '门3 · 质量门：最新一期 profit_yoy > 0 且 revenue_yoy > 0（按公告日对齐，防未来函数）。',
      '门4a · 低波动：volatility_20 ≤ 截面中位数。门4b · 低换手：turnover_20 ≤ 截面中位数。',
      '门0 · 流动性下限：近 20 日日均成交额 amount_mean_20 ≥ min_amount_mean_20（默认 5000 万元）。注意它与门4b 是两个口径：换手率按股本归一，成交额刻画「能承接多少钱」。',
      '个股因子缺值 → 该股不通过（不猜）；因子文件整体缺失 → 该门降级不裁剪并告警（在 run 的日志里可见）。',
    ],
    exits: [
      '① 到期换仓：持有满 hold_days_max 个交易日（含首尾计数）→ T 日尾盘按收盘价卖出。',
      '② 质量恶化：当日已公告的最新一期 profit_yoy < 0 或 revenue_yoy < 0 → 尾盘卖出。',
      '③ 风险事件：isST=1 → 尾盘卖出；收盘跌停 → 顺延 T+1 开盘（红线：不按理想价撮合）。',
      '④ 过度兑现：近 20 个交易日累计涨跌幅 ≥ realized_ret20_max（默认 +30%）→ 尾盘卖出。',
      '⑤ 止损（可配）：毛收益率 ≤ stop_loss_p_mult × p → 尾盘卖出；倍数 0 = 关闭（默认关闭）。',
      '⑥ 止盈（可配）：毛收益率 ≥ take_profit_p_mult × p，或 ≥ take_profit_abs 绝对阈值 → 尾盘卖出；两者 0 = 关闭（默认关闭）。',
      '买入仍是 T+1 开盘价成交（数据红线）；卖出为 T 日尾盘收盘价。多个条件同日触发时，卖出原因用「|」连接。',
    ],
    scoring: [
      '通过全部门的候选，按 5 个维度做逐日截面百分位秩后等权平均打分：ret_50（−1，越弱越好）、profit_yoy（+1）、revenue_yoy（+1）、volatility_20（−1）、turnover_20（−1）。',
      '必须截面标准化再合成 —— 5 个维度量纲不同，直接相加等于让量纲大的独占权重。',
      '降序取前 N = max_positions − 当前持仓数 只；空位才补，不强制换手。每 rebalance_every 个交易日才评估一次建仓。',
    ],
    caveats: [
      '🔴 截至 v2.2 的八轮实验（逐门消融 / 450 组寻优 ×2 / 样本外 / 非牛市窗口 / 风格归因）结论：该策略未跑出正 alpha。M6 风格归因显示 α 在三个窗口全不显著，日收益六到七成由风格解释 —— 本质是「低波动+低换手+低动量+高成长+略偏大市值、β≈0.8」的风格组合，赚的是 beta 不是 alpha。',
      '止损/止盈的「改进」已被样本外证伪（效应符号跨域、跨样本翻转）→ 默认参数一律保持 0（关闭），不要因为某次样本内结果好看就改默认值。',
      '反转门是策略的命根：关掉它年化 +1.88% → −2.43%（−4.31pp）。',
      '样本外区间已被多轮实验使用，不是干净 OOS；对比不同 run 时注意区间与口径（tag 前缀 oos-/nb-/nb30- 区分了窗口）。',
    ],
    specDoc: 'docs/策略规格说明书-v2.0-中频质量反转.md（文档内版本 v2.2）',
  },

  momentum: {
    oneLiner:
      '在上证指数近 3 个交易日逐日均未明显下跌时，买入沪深主板/创业板中' +
      '「近期持续上涨、放量、且近 N 个交易日没出现过涨停/触板」的强势股，' +
      '最多持 5 只、每只 20% 仓位，毛盈利达止盈线即止盈、毛亏达止损线即止损，' +
      '或持有满 hold_days_max 个交易日卖出。',
    nature: '短期动量 + 波动率偏好策略 —— 赌的是「近期强势股在短期内继续强势」。',
    designWhy: [
      '经典短期动量逻辑：放量上涨的强势股短期内有延续惯性；排序键用「近 5 日平均振幅」降序，偏好高波动股 —— 与「赚 6% 就走」的止盈设计一致，代价是回撤更大。',
      '大盘择时（闸门 1）用逐日口径：近 3 个交易日中任一天跌破 −1% 即关门 —— 趋势市满仓、震荡市频繁空仓，收益高度依赖市场环境。',
      '闸门 5「近期无涨停」想排除被资金爆炒过、情绪已透支的标的；⚠️ 但它与「猎强势股」的取向直接对冲，v1.5 的消融显示这条门开着更差（寻优时把 0=关闭放进网格做了对照）。',
      '持有期短（默认 ≤10 个交易日）→ 交易成本是主要敌人（实测年化成本 4.7%~7.8%）。',
    ],
    entries: [
      '闸门1 · 大盘择时：上证近 3 个交易日逐日涨跌幅均 > timing_daily_min（−1%），且当日涨跌幅 > timing_today_min（−0.5%）；不通过则本轮不开仓（已持仓不动）。',
      '闸门2 · 持仓数：当前持仓 < max_positions。',
      '闸门3 · 个股筛选（板块差异化）：今日涨跌幅 ∈ (entry_today_pct_min, 上限)；近 10 日累计涨跌幅 ∈ (下限, 上限)；近 3 日累计 > entry_ret3_min；近 20 日累计 > entry_ret20_min。主板与创业板的上限/区间不同（键名带 _main/_gem 后缀）。',
      '闸门4 · 量比：volume_ratio_1 与 volume_ratio_5 须同时 > volume_ratio_min（放量上涨，过滤缩量上涨）。',
      '闸门5 · 近期无涨停：近 no_limit_up_days 个交易日（含信号日）内不得触及涨停价；0 = 关闭。',
      '闸门3.5 · 极端动量剔除：v1.4 起停用（exclude_top_pct = 0），仅供对照回测。',
    ],
    exits: [
      '① 时间止损：持有满 hold_days_max 个交易日（含首尾）→ T 日尾盘按收盘价卖出。',
      '② 止盈：毛收益率 ≥ 止盈线（take_profit_gross_main / _gem，v1.5 起两板块统一 6%）→ 尾盘卖出。',
      '③ 大盘急跌：上证当日收盘涨跌幅 < index_panic_pct（−1.5%）→ 全部持仓尾盘卖出。',
      '④ 跌停：持仓股 T 日跌停 → 尾盘挂单，卖不掉顺延 T+1 开盘重试（不按理想价撮合）。',
      '⑤ 风险警示：isST = 1 → 尾盘卖出。',
      '⑥ 个股止损：毛亏损 ≥ stop_loss_amp_mult × 近 stop_loss_amp_window 日平均振幅（主板/创业板同值）→ 尾盘卖出。',
      '买入是 T+1 开盘价成交；卖出（清仓）v1.6 起改为 T 日尾盘按收盘价成交。当日买入的持仓当日不得卖出（A 股 T+1 守卫）。',
    ],
    caveats: [
      '🔴 四个版本、两条成交口径、600+ 组寻优的全部结论：momentum 没有跑出正 alpha。根因是 alpha 在「隔夜」漏光（买入强势股 → 次日平均低开 1.8%，A 股短期反转）。',
      'v1.5 的两条新规则已被自己的消融否掉：闸门 5「近 10 日无涨停」开着更差；止损越松越好（1.2× 是收紧方向，实测方向相反）。',
      'v1.6 把清仓改到 T 日尾盘成交后口径偏乐观（既判定又成交），新旧数字不可比。',
      '出场时点是二阶变量（|Δ|≤4pp）：换成交时点救不回买入侧的问题。',
    ],
    specDoc: '策略规格说明书.md（现役 v1.6）',
  },
}

// ======================================================================
// 参数词典（两条策略合并一张表，键名全局唯一）
// ======================================================================

export const PARAM_DOCS: Record<string, ParamDoc> = {
  // ---------- 通用（两策略共用语义） ----------
  max_positions: {
    label: '最大持仓数',
    desc: '同时持有的股票数上限。quality_reversal 默认 10 只×10%仓位；momentum 默认 5 只×20%仓位。',
  },
  max_weight_per_stock: {
    label: '单票仓位',
    desc: '每只买入时投入「此比例 × 当前总资产」（不是初始资金，随净值复利）。',
  },
  hold_days_max: {
    label: '目标持有期',
    desc: '持有满 N 个交易日（含首尾计数）即时间止损卖出。quality_reversal 默认 30；momentum 默认 10。',
  },
  benchmark: { label: '基准指数', desc: '净值对照与大盘信号的来源（quality_reversal 兼作门1 的信号源）。' },

  // ---------- quality_reversal 专有 ----------
  rebalance_every: {
    label: '建仓评估间隔',
    desc: '每 N 个交易日评估一次建仓（默认 5）—— 控换手的关键参数，不是每天换仓。',
  },
  index_ret20_min: {
    label: '门1 · 大盘非急跌下限',
    desc: '上证近 20 个交易日累计涨跌幅低于此值 → 当日不建仓（默认 −10%）。设为 −1.0 等效关闭。',
  },
  reversal_pct: {
    label: '门2 · 反转分位上限',
    desc: 'ret_50（近 50 日累计涨跌幅）截面分位 ≤ 此值才候选（默认 0.50，只买偏弱的一半）。设为 1.0 等效关闭。反转门是策略的命根（消融 −4.31pp）。',
  },
  max_ret_20_max: {
    label: '门2b · 脉冲上限',
    desc: '近 20 日最大单日涨幅超过此值的「脉冲股」被排除（默认 0.15）。⚠️ 无独立开关，只能把上限调大来放宽。',
  },
  use_quality_gate: {
    label: '门3 · 质量门开关',
    desc: '开启时要求最新一期 profit_yoy > 0 且 revenue_yoy > 0（按公告日对齐，防未来函数）。',
  },
  use_lowvol_gate: {
    label: '门4a · 低波动开关',
    desc: '开启时要求 volatility_20 ≤ 截面中位数。消融显示贡献 ≈0（中性）。',
  },
  use_lowturn_gate: {
    label: '门4b · 低换手开关',
    desc: '开启时要求 turnover_20 ≤ 截面中位数。消融显示贡献 ≈0（中性）。',
  },
  use_liquidity_gate: {
    label: '门0 · 流动性门开关',
    desc: '开启时要求日均成交额达标。消融显示这条门为负贡献（关掉 +1.30pp），但默认保持开启（主人拍板不放宽）。',
  },
  min_amount_mean_20: {
    label: '门0 · 成交额下限',
    desc: '近 20 日日均成交额（元）下限，默认 5000 万。与门4b 的换手率是两个口径：换手率按股本归一，成交额刻画「能承接多少钱」。',
  },
  realized_ret20_max: {
    label: '过度兑现阈值',
    desc: '退出条件④：近 20 日累计涨跌幅 ≥ 此值（默认 +30%）即卖出 —— 唯一设计上的「落袋」出口。设为极大值（如 10）等效关闭。',
  },
  stop_loss_p_mult: {
    label: '止损倍数（×p）',
    desc: '退出条件⑤：毛收益率 ≤ 此倍数 × p 时卖出。0 = 关闭（默认）。⚠️ 多轮实验已证伪「加止损」：符号跨域/跨样本翻转，停用是最终结论。',
  },
  take_profit_p_mult: {
    label: '止盈倍数（×p）',
    desc: '退出条件⑥之一：毛收益率 ≥ 此倍数 × p 时卖出。0 = 关闭（默认）。450 组寻优里任何止盈档都更差。',
  },
  take_profit_abs: {
    label: '止盈绝对阈值',
    desc: '退出条件⑥之一：毛收益率 ≥ 此值（如 0.06 = 6%）时卖出。0 = 关闭（默认）。6% 绝对档比 ×p 档好，但仍远差于不加。',
  },

  // ---------- momentum 专有 ----------
  index_code: { label: '择时/清仓信号源', desc: '大盘择时与急跌清仓都读这个指数（默认上证 sh.000001）。' },
  timing_daily_min: {
    label: '闸门1 · 逐日涨跌幅下限',
    desc: '上证近 3 个交易日「逐日」涨跌幅都必须 > 此值（默认 −1%），任一天跌破即关门。',
  },
  timing_today_min: {
    label: '闸门1 · 当日涨跌幅下限',
    desc: '上证当日涨跌幅必须 > 此值（默认 −0.5%）才开仓。',
  },
  entry_today_pct_min: { label: '今日涨跌幅下限', desc: '闸门3①：个股当日涨跌幅必须 > 此值（默认 1%）—— 只买今天确实在涨的。' },
  entry_today_pct_max_main: { label: '今日涨跌幅上限 · 主板', desc: '闸门3①：主板个股当日涨跌幅上限（默认 6%）—— 排除已经板上的票。' },
  entry_today_pct_max_gem: { label: '今日涨跌幅上限 · 创业板', desc: '闸门3①：创业板当日涨跌幅上限（默认 11%，注册制 ±20% 故放宽）。' },
  entry_ret3_min: { label: '近 3 日累计涨幅下限', desc: '闸门3③：近 3 个交易日累计涨跌幅 > 此值（默认 5%），通用不分板块。' },
  entry_ret20_min: { label: '近 20 日累计涨幅下限', desc: '闸门3④：近 20 个交易日累计涨跌幅 > 此值（默认 5%）。与③高度相关，主要用来排除前期有过大跌的票。' },
  entry_ret10_min_main: { label: '近 10 日累计区间下限 · 主板', desc: '闸门3②：主板近 10 日累计涨跌幅 > 此值（默认 3%）。' },
  entry_ret10_max_main: { label: '近 10 日累计区间上限 · 主板', desc: '闸门3②：主板近 10 日累计涨跌幅 < 此值（默认 15%）—— 排除前期已暴涨的，与下限配合形成「温和上涨」。' },
  entry_ret10_min_gem: { label: '近 10 日累计区间下限 · 创业板', desc: '闸门3②：创业板近 10 日累计涨跌幅 > 此值（默认 5%）。' },
  entry_ret10_max_gem: { label: '近 10 日累计区间上限 · 创业板', desc: '闸门3②：创业板近 10 日累计涨跌幅 < 此值（默认 20%）。' },
  amplitude_window: {
    label: '排序键窗口（振幅）',
    desc: '候选按「近 N 日平均振幅」降序排列取前 N 只（默认 5）。振幅 = (最高−最低)/前收盘，后复权口径。这个排序键偏好高波动股。',
  },
  volume_ratio_min: {
    label: '闸门4 · 量比下限',
    desc: '1 日量比与 5 日量比须同时 > 此值（默认 1.5）。量比 = volume[t] / mean(volume[t-n..t-1])（分母不含今日），过滤缩量上涨。',
  },
  no_limit_up_days: {
    label: '闸门5 · 近期无涨停窗口',
    desc: '近 N 个交易日（含信号日）内不得出现涨停/触板（按最高价触及涨停价判定，比例容差 0.005）；N = 0 时本闸门完全关闭。⚠️ v1.5 消融显示开着更差。',
  },
  use_stop_loss: { label: '个股止损开关', desc: '是否启用退出条件⑥（v1.4 新增，v1.5 收紧并取消板块区分）。' },
  stop_loss_amp_window: { label: '止损基准窗口（振幅）', desc: '止损线 = 倍数 × 近 N 日平均振幅（默认 5）。' },
  stop_loss_amp_mult_main: { label: '止损倍数 · 主板', desc: '毛亏损 ≥ 此倍数 × 近 N 日均振幅即止损。v1.5 定为 2.0（此前 1.2 收紧方向已被消融否掉：越松越好）。' },
  stop_loss_amp_mult_gem: { label: '止损倍数 · 创业板', desc: '同上，v1.5 起与主板同值。' },
  exclude_top_pct: {
    label: '极端动量剔除分位',
    desc: '闸门3.5（v1.4 起停用）：剔除 momentum_20 处于全池最高「1−此值」分位的股票。0 = 停用，仅供对照回测。',
  },
  take_profit_gross_main: { label: '止盈线 · 主板', desc: '毛收益率 ≥ 此值即止盈（毛口径 = 不扣费用）。v1.5 起两板块统一 6%。' },
  take_profit_gross_gem: { label: '止盈线 · 创业板', desc: '同上。' },
  index_panic_pct: { label: '大盘急跌清仓阈值', desc: '上证当日收盘涨跌幅 < 此值（默认 −1.5%）→ 全部持仓尾盘卖出。' },

  // ---------- 引擎/回测设置 ----------
  initial_cash: { label: '初始资金', desc: '回测起始总资产（默认 100 万元）。' },
}

// ======================================================================
// 运行 tag 解读
// ======================================================================

/** 已知的「窗口前缀」→ 含义 */
const TAG_WINDOW_PREFIX: Record<string, string> = {
  nb30: '非牛市窗口（2025-12-01 ~ 2026-09-30，行情补数修复后的全区间）',
  nb: '非牛市窗口（2025-12-01 ~ 2026-09-17，修复前最后一个干净交易日）',
  oos: '样本外窗口（2025-01-01 ~ 2026-09-16，一轮牛市）',
  v20: '样本内窗口（2020-06-01 ~ 2024-12-31）',
  v21: '样本内窗口（2020-06-01 ~ 2024-12-31）',
  v22: '样本内窗口（2020-06-01 ~ 2024-12-31）',
}

/** 已知的「配置域」词元 → 含义 */
const TAG_DOMAIN: Record<string, string> = {
  baseline: '门全开基线（所有闸门/退出按默认值）',
  base: '门全开基线（所有闸门/退出按默认值）',
  merge: '合并档：关流动性门（use_liquidity_gate=false）+ 关过度兑现退出（realized_ret20_max=10）',
  tp6: '止盈绝对档 6%（take_profit_abs=0.06）',
  pos2: '最大持仓数 2（max_positions=2）',
  pos5: '最大持仓数 5（max_positions=5）',
  pos10: '最大持仓数 10（max_positions=10）',
}

/**
 * 解析一个运行 tag（如 `nb30-base-sl15`、`nb30-merge-h40-sl15`）。
 *
 * tag 是跑回测时手工敲的标记，这里按项目的命名惯例逐词转译；
 * 识别不了的词元原样保留 —— **最终解释以配置快照（config.json）为准**。
 */
export function decodeTag(tag: string | null | undefined): TagToken[] {
  if (!tag) return []
  const out: TagToken[] = []
  for (const raw of tag.split('-')) {
    const t = raw.trim()
    if (!t) continue
    const low = t.toLowerCase()

    // 窗口前缀
    if (TAG_WINDOW_PREFIX[low]) {
      out.push({ token: t, meaning: TAG_WINDOW_PREFIX[low] })
      continue
    }
    // 版本批次前缀（v1.3/v1.5/v2.0…）
    if (/^v\d+(\.\d+)?$/.test(low)) {
      out.push({ token: t, meaning: '策略版本批次标记（对应规格书的版本沿革）' })
      continue
    }
    // 成交口径词元
    if (low === 'close') {
      out.push({ token: t, meaning: '成交口径对照：清仓改为 T 日尾盘按收盘价成交（v1.6 口径）' })
      continue
    }
    if (low === 'recon') {
      out.push({ token: t, meaning: '参数重建（代码已丢失，按已知参数重建的版本）' })
      continue
    }
    // 实验批次前缀（m3c/m3d/m3e/m4/m5…）
    if (/^m\d+[a-z]?$/.test(low)) {
      out.push({ token: t, meaning: '样本内实验批次标记（M 系列实验的批次号，见规格书 §7 各节）' })
      continue
    }
    // 配置域词元
    if (TAG_DOMAIN[low]) {
      out.push({ token: t, meaning: TAG_DOMAIN[low] })
      continue
    }
    // 止损：sl15 → 1.5×p；sl125 → 1.25×p；sl10 → 1.0×p；sl1.75 → 1.75×p
    const sl = low.match(/^sl(\d+\.?\d*)$/)
    if (sl) {
      const n = sl[1]
      const mult = n.includes('.') ? parseFloat(n) : n.length <= 2 ? parseInt(n, 10) / 10 : parseInt(n, 10) / 100
      out.push({ token: t, meaning: `止损线 = ${mult}×p（stop_loss_p_mult=${mult}；p = 近 5 日「每日涨跌幅绝对值」均值）` })
      continue
    }
    // 持有期：h40 → hold_days_max=40
    const h = low.match(/^h(\d+)$/)
    if (h) {
      out.push({ token: t, meaning: `目标持有期 ${h[1]} 个交易日（hold_days_max=${h[1]}）` })
      continue
    }
    // 止盈倍数：tp15 → 1.5×p
    const tp = low.match(/^tp(\d+\.?\d*)$/)
    if (tp) {
      const n = tp[1]
      const mult = n.includes('.') ? parseFloat(n) : n.length <= 2 ? parseInt(n, 10) / 10 : parseInt(n, 10) / 100
      out.push({ token: t, meaning: `止盈线 = ${mult}×p（take_profit_p_mult=${mult}）` })
      continue
    }
    // 其它：原样保留
    out.push({ token: t, meaning: '（命名惯例之外的词元，含义以配置快照为准）' })
  }
  return out
}

// ======================================================================
// 参数行组装
// ======================================================================

export interface ParamRow {
  name: string
  value: string
  label: string
  desc: string
  /** 词典里有解释 = true；没有 = 仅展示键值 */
  documented: boolean
}

/** 偏好的展示顺序（词典顺序），未收录的键按字典序跟在后面 */
const PARAM_ORDER = [
  'max_positions',
  'max_weight_per_stock',
  'hold_days_max',
  'rebalance_every',
  'index_ret20_min',
  'reversal_pct',
  'max_ret_20_max',
  'use_quality_gate',
  'use_lowvol_gate',
  'use_lowturn_gate',
  'use_liquidity_gate',
  'min_amount_mean_20',
  'realized_ret20_max',
  'stop_loss_p_mult',
  'take_profit_p_mult',
  'take_profit_abs',
  'benchmark',
  'index_code',
  'timing_daily_min',
  'timing_today_min',
  'entry_today_pct_min',
  'entry_today_pct_max_main',
  'entry_today_pct_max_gem',
  'entry_ret3_min',
  'entry_ret20_min',
  'entry_ret10_min_main',
  'entry_ret10_max_main',
  'entry_ret10_min_gem',
  'entry_ret10_max_gem',
  'amplitude_window',
  'volume_ratio_min',
  'no_limit_up_days',
  'exclude_top_pct',
  'use_stop_loss',
  'stop_loss_amp_window',
  'stop_loss_amp_mult_main',
  'stop_loss_amp_mult_gem',
  'index_panic_pct',
  'take_profit_gross_main',
  'take_profit_gross_gem',
  'initial_cash',
]

/** 把参数值转成可读文案（金额用万/亿，布尔用开/关） */
export function formatParamValue(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'boolean') return v ? '开启' : '关闭'
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return '—'
    if (Math.abs(v) >= 1e8) return `${Number((v / 1e8).toPrecision(6))} 亿`
    if (Math.abs(v) >= 1e4) return `${Number((v / 1e4).toPrecision(6))} 万`
    return String(v)
  }
  return String(v)
}

/**
 * 从一次 run 的 config 快照里取参数 dict。
 * 兼容两种结构：`{strategy:{params:{...}}}`（现行）与 `{params:{...}}`（旧）。
 */
export function extractParams(config: Record<string, unknown> | null | undefined): Record<string, unknown> {
  if (!config) return {}
  const s = config.strategy
  if (s && typeof s === 'object' && !Array.isArray(s)) {
    const p = (s as Record<string, unknown>).params
    if (p && typeof p === 'object' && !Array.isArray(p)) return p as Record<string, unknown>
  }
  if (config.params && typeof config.params === 'object' && !Array.isArray(config.params)) {
    return config.params as Record<string, unknown>
  }
  return {}
}

/** 组装参数表的行：已收录的按固定顺序，未收录的键按字典序追加 */
export function buildParamRows(params: Record<string, unknown>): ParamRow[] {
  const keys = Object.keys(params)
  const known = PARAM_ORDER.filter((k) => keys.includes(k))
  const unknown = keys.filter((k) => !PARAM_ORDER.includes(k)).sort()
  const rows: ParamRow[] = []
  for (const k of [...known, ...unknown]) {
    const doc = PARAM_DOCS[k]
    rows.push({
      name: k,
      value: formatParamValue(params[k]),
      label: doc?.label ?? '',
      desc: doc?.desc ?? '',
      documented: Boolean(doc),
    })
  }
  return rows
}
