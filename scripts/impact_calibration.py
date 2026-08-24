"""影响分校准 v2：识别「次日有大动静」的新闻（分组洞察 + 逻辑回归）。

v1 的两个教训：
1. Y 错配：个股公告对市场指数几乎无影响，应用「该股次日 |收益|」衡量；
2. 时间偏斜：backfill 公告集中在近月（2026-07/08 占 49%），网格搜索权重易过拟合。

v2 方法：
- Y = 带个股标签的新闻用该股次日 |收益|，无标签的用上证指数次日 |收益|；
- 分组统计：事件类型 / 来源大类 / 多源爆发 / 情绪强度 -> 次日平均 |ret| 与
  P(|ret|>1.5%)、P(|ret|>3%)，直接产出可解释的告警规则；
- 手写逻辑回归（numpy，无 sklearn 依赖）预测 P(|ret|>1.5%)，样本内训练、
  样本外 AUC 验证，输出特征系数解释「什么特征真正预测大波动」。

输出：results/impact_calibration.md / impact_calibration.csv
"""

import bisect
import json
import pathlib
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.event_filter import classify  # noqa: E402
from news_aggregator.impact import match_themes, compute_impact  # noqa: E402

NEWS_DIR = ROOT / "news"
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
SPLIT_DATE = "2025-08-01"
THR_TAIL = 0.015   # 次日 |ret| > 1.5% 视为「大动静」
THR_BIG = 0.03     # 次日 |ret| > 3% 视为「特大动静」


def load_all_items() -> list:
    items, seen = [], set()
    for path in sorted((NEWS_DIR / "raw").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            if it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            items.append(it)
    return items


def load_index() -> tuple:
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol="sh000001")
    dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
    closes = pd.to_numeric(df["close"], errors="coerce").tolist()
    ret = {}
    for i in range(len(dates) - 1):
        if closes[i] > 0:
            ret[dates[i]] = abs(closes[i + 1] / closes[i] - 1)
    return ret, dates


def load_stock_rets(pool: list) -> dict:
    out = {}
    for it in pool:
        p = DATA_DIR / f"bars_{it['symbol']}.csv"
        if not p.exists():
            continue
        bars = pd.read_csv(p, encoding="utf-8-sig")
        dates = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d").tolist()
        closes = bars["close"].to_numpy(float)
        r = {}
        for i in range(len(dates) - 1):
            if closes[i] > 0:
                r[dates[i]] = abs(closes[i + 1] / closes[i] - 1)
        out[it["symbol"]] = r
    return out


def source_group(src: str) -> str:
    if src in ("东方财富公告", "SEC EDGAR", "政策公告"):
        return "公告"
    if src in ("东方财富个股新闻", "个股新闻"):
        return "个股新闻"
    if src in ("AP", "Reuters", "AFP", "彭博", "白宫", "中国外交部", "美联储Fed",
               "欧洲央行ECB", "日本央行BOJ", "英国央行BOE", "中国人民银行", "FRED宏观数据",
               "美国非农/CPI", "美国GDP/PCE", "ISM PMI", "EIA原油库存", "OPEC/IEA",
               "中国宏观数据", "IMF/世界银行", "美国国务院", "国会听证会", "SEC EDGAR"):
        return "宏观/一手"
    return "快讯/社媒"


def build_sample(items, idx_ret, idx_dates, stock_rets) -> pd.DataFrame:
    # backfill 归档的公告条目没有 impact 字段，先全量补齐 impact/impact_parts/sentiment
    items = compute_impact(items, themes_list)
    rows = []
    for it in items:
        if it.get("kind") != "news":
            continue
        d = it.get("date")
        if not d:
            continue
        i = bisect.bisect_right(idx_dates, d) - 1
        if i < 0 or i >= len(idx_dates) - 1:
            continue
        ref = idx_dates[i]
        syms = [s for s in (it.get("symbols") or []) if s in stock_rets]
        y_stk = None
        for s in syms:
            r = stock_rets[s].get(ref)
            if r is not None:
                y_stk = r
                break
        y_mkt = idx_ret.get(ref)
        if y_stk is not None:
            y = y_stk
            y_kind = "stock"
        elif y_mkt is not None:
            y = y_mkt
            y_kind = "market"
        else:
            continue
        text = f"{it.get('title', '')} {it.get('content', '')}"
        cls = classify(text)
        ev_type = cls[0] if cls else "其他"
        ev_whitelist = int(bool(cls and cls[2]))
        matched = match_themes(text, themes_list)
        parts = it.get("impact_parts") or {}
        burst = float(parts.get("burst", 0.0))
        intensity = float(parts.get("intensity", 0.0))
        rows.append({
            "id": it.get("id"), "date": d, "ref": ref,
            "source": it.get("source") or "", "group": source_group(it.get("source") or ""),
            "event_type": ev_type, "ev_whitelist": ev_whitelist,
            "n_themes": len(matched), "has_symbols": 1 if syms else 0,
            "burst": burst, "intensity": intensity,
            "y": y, "y_kind": y_kind,
            "tail": int(y > THR_TAIL), "big": int(y > THR_BIG),
        })
    return pd.DataFrame(rows)


themes_list = None  # 模块级，main 中赋值


def group_stats(df: pd.DataFrame, col: str) -> pd.DataFrame:
    base_y = float(df["y"].mean())
    base_tail = float(df["tail"].mean())
    base_big = float(df["big"].mean())
    rows = []
    for val, sub in df.groupby(col, dropna=False):
        if len(sub) < 30:
            continue
        rows.append({
            "value": str(val), "n": len(sub),
            "mean_abs": float(sub["y"].mean()),
            "tail_rate": float(sub["tail"].mean()),
            "big_rate": float(sub["big"].mean()),
            "lift_tail": float(sub["tail"].mean() / base_tail) if base_tail else None,
            "lift_big": float(sub["big"].mean() / base_big) if base_big else None,
        })
    out = pd.DataFrame(rows).sort_values("big_rate", ascending=False)
    return out


def logreg_fit(X: np.ndarray, y: np.ndarray, epochs: int = 600,
               lr: float = 0.5, l2: float = 1e-3, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n, p = X.shape
    w = rng.normal(0, 0.05, p)
    b = 0.0
    Xb = X @ w + b
    for _ in range(epochs):
        p_ = 1.0 / (1.0 + np.exp(-np.clip(Xb, -30, 30)))
        grad = (X.T @ (p_ - y)) / n + l2 * w
        w -= lr * grad
        b -= lr * float(np.mean(p_ - y))
        Xb = X @ w + b
    return np.concatenate([[b], w])


def logreg_prob(X: np.ndarray, theta: np.ndarray) -> np.ndarray:
    z = np.clip(X @ theta[1:] + theta[0], -30, 30)
    return 1.0 / (1.0 + np.exp(-z))


def auc(y: np.ndarray, score: np.ndarray) -> float:
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    ranks = pd.Series(score).rank().to_numpy()
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def build_features(df: pd.DataFrame) -> tuple:
    feats = pd.get_dummies(df[["group", "event_type"]], columns=["group", "event_type"], dtype=float)
    feats["burst"] = df["burst"].to_numpy()
    feats["intensity"] = df["intensity"].to_numpy()
    feats["n_themes"] = df["n_themes"].to_numpy() / 5.0
    feats["has_symbols"] = df["has_symbols"].to_numpy()
    cols = list(feats.columns)
    X = feats.to_numpy(float)
    mean = X.mean(axis=0, keepdims=True)
    std = X.std(axis=0, keepdims=True)
    std[std == 0] = 1.0
    X = (X - mean) / std
    return X, cols


def main() -> int:
    global themes_list
    themes_list = yaml.safe_load((ROOT / "news_aggregator/themes.yaml").read_text(encoding="utf-8"))["themes"]
    items = load_all_items()
    idx_ret, idx_dates = load_index()
    pool = yaml.safe_load((DATA_DIR / "universe_expanded.yaml").read_text(encoding="utf-8"))["symbols"]
    stock_rets = load_stock_rets(pool)
    print(f"[load] 新闻 {len(items)} 条")

    df = build_sample(items, idx_ret, idx_dates, stock_rets)
    print(f"[sample] {len(df)} 条（个股Y {int((df['y_kind'] == 'stock').sum())} / 市场Y {int((df['y_kind'] == 'market').sum())}）")
    base_y, base_tail, base_big = df["y"].mean(), df["tail"].mean(), df["big"].mean()
    print(f"[base] 次日|ret|均值 {base_y*100:.2f}% | P(>1.5%) {base_tail*100:.1f}% | P(>3%) {base_big*100:.1f}%")

    is_df = df[df["date"] < SPLIT_DATE]
    oos_df = df[df["date"] >= SPLIT_DATE]

    # 分组统计（全样本）
    g_evt = group_stats(df, "event_type")
    g_src = group_stats(df, "group")
    df["burst_flag"] = (df["burst"] >= 0.5).astype(int)
    g_burst = group_stats(df, "burst_flag")
    print("[groups] 事件类型分组完成")

    # 逻辑回归（样本内训练、样本外 AUC）——特征列在全量 df 上统一构造，避免 dummy 列集不一致
    X_all, cols = build_features(df)
    is_mask = (df["date"] < SPLIT_DATE).to_numpy()
    X_is, y_is = X_all[is_mask], df.loc[is_mask, "tail"].to_numpy()
    X_oos, y_oos = X_all[~is_mask], df.loc[~is_mask, "tail"].to_numpy()
    theta = logreg_fit(X_is, y_is)
    auc_is = auc(y_is, logreg_prob(X_is, theta))
    auc_oos = auc(y_oos, logreg_prob(X_oos, theta))
    # 基线：原 impact 总分（近似：authority*0.2 + burst*0.2 + intensity*0.25 与原文权重一致）
    # 注意：必须在切片之前赋值，避免 SettingWithCopy
    df["impact_raw"] = (0.2 * df["burst"] + 0.25 * df["intensity"])
    base_score_is = df.loc[is_mask, "impact_raw"].to_numpy()
    base_score_oos = df.loc[~is_mask, "impact_raw"].to_numpy()
    auc_base_is = auc(y_is, base_score_is)
    auc_base_oos = auc(y_oos, base_score_oos)
    print(f"[logreg] AUC: IS {auc_is:.3f} / OOS {auc_oos:.3f}（基线 impact 分数 IS {auc_base_is:.3f} / OOS {auc_base_oos:.3f}）")

    df.to_csv(RESULTS_DIR / "impact_calibration.csv", index=False, encoding="utf-8-sig")

    # 报告
    def pct(v):
        return "-" if v is None else f"{v * 100:.1f}%"

    lines = []
    lines.append("# 影响分校准报告 v2（什么新闻第二天有大动静）")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"> 样本 {len(df)} 条：个股标签新闻用该股次日 |收益|，无标签用上证指数次日 |收益|。")
    lines.append(f"> 基线：次日 |收益| 均值 {pct(base_y)}，P(>1.5%) = {pct(base_tail)}，P(>3%) = {pct(base_big)}。")
    lines.append("> **这是注意力过滤器，不是交易信号：只预测「波动大小」，不预测方向。**")
    lines.append("")
    lines.append("## 一、什么新闻最值得告警（分组统计，按 P(>3%) 降序）")
    lines.append("")
    for title, g in (("事件类型", g_evt), ("来源大类", g_src), ("多源爆发", g_burst)):
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| 组 | 样本 | 次日|ret|均值 | P(>1.5%) | P(>3%) | 大动静提升 |")
        lines.append("|---|---|---|---|---|---|")
        for _, r in g.iterrows():
            lift = f"{r['lift_big']:.1f}x" if r["lift_big"] else "-"
            lines.append(f"| {r['value']} | {int(r['n'])} | {pct(r['mean_abs'])} | "
                         f"{pct(r['tail_rate'])} | {pct(r['big_rate'])} | {lift} |")
        lines.append("")
    lines.append("## 二、逻辑回归（预测 P(次日|ret|>1.5%)，样本内训练 / 样本外验证）")
    lines.append("")
    lines.append(f"- 模型 AUC：样本内 **{auc_is:.3f}** / 样本外 **{auc_oos:.3f}**")
    lines.append(f"- 对照（原 impact 总分近似）：样本内 {auc_base_is:.3f} / 样本外 {auc_base_oos:.3f}")
    lines.append("")
    coef = pd.Series(theta[1:], index=cols).sort_values(ascending=False)
    lines.append("特征系数（正 = 增大波动概率）：")
    lines.append("")
    for name, c in coef.head(8).items():
        lines.append(f"- `{name}`: {c:+.3f}")
    lines.append("")
    lines.append("## 三、告警规则建议（可直接配进 monitor）")
    lines.append("")
    lines.append("- 大动静集中度最高的新闻类型优先告警（见第一节表顶部的组）。")
    lines.append("- 建议把 monitor 的告警过滤从「单一影响分阈值」改为「事件类型白名单 + 多源爆发」组合规则，")
    lines.append("  并保留 impact_min 作为兜底（防止低影响刷屏）。")
    lines.append("- 告警文案可附带本报告的同类事件历史波动参考（如「同类事件次日平均波动 X%，大动静概率 Y%」）。")
    lines.append("")
    (RESULTS_DIR / "impact_calibration.md").write_text("\n".join(lines), encoding="utf-8")
    print("[report] -> results/impact_calibration.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
