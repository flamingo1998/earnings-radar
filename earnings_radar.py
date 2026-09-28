#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财报雷达 · Earnings Radar  v0.1.0

财报事件 + 期权隐含波动 分析工具（美股为主）。
数据源：Longbridge CLI（需已安装并登录，https://open.longbridge.com）

输入自选股清单，输出：
  1. 下个财报日（含盘前/盘后）
  2. 期权隐含波动幅度（市场给这次财报定价了多少波动）
  3. 近 N 季财报日实际波动分布
  4. IV vs 历史波动率对比
  5. 暗底 HTML 报告 + CSV

用法：
  python3 earnings_radar.py --tickers NKE,MU,COST
  python3 earnings_radar.py --tickers-file watchlist.txt --days 60
  python3 earnings_radar.py --tickers AAPL --history-years 3

仅供学习研究，不构成投资建议。
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import time

VERSION = "0.1.0"
DEFAULT_DAYS = 45          # 未来多少天内视为"即将到来的财报"
DEFAULT_HIST_YEARS = 3     # 取多少年历史财报做统计
CLI_CALL_GAP = 0.35        # Longbridge CLI 调用间隔（避免触发限频）


class RadarError(Exception):
    pass


# ------------------------------------------------------------------ CLI 封装
def call_cli(cli, *args, retries=3):
    """调用 Longbridge CLI 并解析 JSON，带限频重试。"""
    cmd = [cli] + [str(a) for a in args] + ["--format", "json"]
    last = "unknown"
    for attempt in range(1, retries + 1):
        time.sleep(CLI_CALL_GAP)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except Exception as exc:
            last = "invoke failed: %s" % exc
        else:
            out = (p.stdout or "").strip()
            err = (p.stderr or "").strip()
            if out:
                try:
                    return json.loads(out)
                except json.JSONDecodeError:
                    last = "JSON parse failed: %s" % out[:150]
            else:
                last = (err or "empty output")[:220]
        if attempt < retries:
            time.sleep(0.6 * attempt)
    raise RadarError(last)


def norm_symbol(ticker):
    t = ticker.strip().upper()
    if not re.match(r"^[A-Z0-9\.\-]+\.(US|HK|SG|CN)$", t):
        t = t + ".US"
    return t


def display_symbol(sym):
    return sym.split(".")[0]


# ------------------------------------------------------------------ 数据获取
def get_quote(cli, sym):
    d = call_cli(cli, "quote", sym)
    if not isinstance(d, list) or not d:
        raise RadarError("quote empty")
    q = d[0]
    return float(q["last"])


def get_klines(cli, sym, count=560):
    d = call_cli(cli, "kline", sym, "--count", count, "--adjust", "forward")
    rows = []
    if isinstance(d, list):
        for x in d:
            try:
                rows.append({"date": x["time"][:10], "close": float(x["close"])})
            except Exception:
                continue
    rows.sort(key=lambda r: r["date"])
    return rows


def get_calendar(cli, sym, start, end):
    """返回标准化财报事件列表。"""
    try:
        d = call_cli(cli, "finance-calendar", "report", "--symbol", sym,
                     "--start", start, "--end", end)
    except RadarError:
        return []
    events = []
    if not isinstance(d, dict):
        return events
    for item in d.get("list", []) or []:
        for info in item.get("infos", []) or []:
            content = str(info.get("content") or "")
            if "业绩" not in content:
                continue
            etd = None
            m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", str(info.get("date") or ""))
            if m:
                etd = "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
            if not etd:
                m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(item.get("date") or ""))
                if m2:
                    etd = m2.group(0)
            if not etd:
                continue
            dtxt = str(info.get("date_type") or "")
            timing = "pm" if "盘后" in dtxt else ("am" if "盘前" in dtxt else "?")
            kv = {}
            for k in info.get("data_kv") or []:
                kv[str(k.get("type"))] = k.get("value")
            events.append({
                "date": etd, "timing": timing, "period": content,
                "eps_est": kv.get("estimate_eps"), "eps_act": kv.get("actual_eps"),
                "rev_est": kv.get("estimate_revenue"), "rev_act": kv.get("actual_revenue"),
            })
    seen = set()
    out = []
    for e in events:
        key = (e["date"], e["period"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda e: e["date"])
    return out


def get_expiries(cli, sym):
    try:
        d = call_cli(cli, "option", "chain", sym)
    except RadarError:
        return []
    if not isinstance(d, list):
        return []
    return sorted(str(x.get("expiry_date")) for x in d if x.get("expiry_date"))


def get_chain(cli, sym, date):
    d = call_cli(cli, "option", "chain", sym, "--date", date)
    rows = []
    if isinstance(d, list):
        for x in d:
            if str(x.get("standard", "true")).lower() not in ("true", "1"):
                continue
            rows.append(x)
    return rows


# ------------------------------------------------------------------ 指标计算
def hv_pct(closes, n=30):
    cl = closes[-(n + 1):]
    if len(cl) < 10:
        return None
    rets = [math.log(cl[i] / cl[i - 1]) for i in range(1, len(cl)) if cl[i - 1] > 0]
    if len(rets) < 5:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100.0


def implied_move(cli, sym, spot, earnings_iso, max_probe=4):
    """财报后首个可用到期日的 ATM 跨式 / 现价 = 隐含波动幅度。

    首选到期日 = 财报日之后最近的到期日；若其无报价数据（部分新挂合约仅有代码），
    自动尝试随后到期日（最多 max_probe 个），取第一个有可用报价的。
    """
    exps = [e for e in get_expiries(cli, sym) if e > earnings_iso][:max_probe]
    for exp in exps:
        chain = get_chain(cli, sym, exp)
        cand = []
        for x in chain:
            try:
                c = float(x.get("call_last") or 0)
                p = float(x.get("put_last") or 0)
                k = float(x["strike"])
            except Exception:
                continue
            if c > 0 and p > 0:
                cand.append((abs(k - spot), k, c, p, x))
        if not cand:
            continue
        cand.sort(key=lambda z: z[0])
        _, k, c, p, x = cand[0]
        iv_c = float(x.get("call_iv") or 0) * 100.0
        iv_p = float(x.get("put_iv") or 0) * 100.0
        iv = (iv_c + iv_p) / 2.0 if (iv_c or iv_p) else None
        straddle = c + p
        return {"exp": exp, "strike": k, "straddle": straddle,
                "move_pct": straddle / spot * 100.0, "iv": iv}
    return None


def realized_moves(klines, past_events, lookback=8):
    idx_by_date = {r["date"]: i for i, r in enumerate(klines)}
    dates = [r["date"] for r in klines]
    out = []
    for e in past_events:
        d = e["date"]
        idx = idx_by_date.get(d)
        if idx is None:
            prev = [i for i, dd in enumerate(dates) if dd <= d]
            if not prev:
                continue
            idx = prev[-1]
        if e["timing"] == "pm":
            if idx + 1 >= len(klines):
                continue
            mv = klines[idx + 1]["close"] / klines[idx]["close"] - 1.0
        elif e["timing"] == "am":
            if idx - 1 < 0:
                continue
            mv = klines[idx]["close"] / klines[idx - 1]["close"] - 1.0
        else:
            continue
        out.append({"date": d, "move": mv * 100.0})
    out.sort(key=lambda z: z["date"])
    return out[-lookback:]


def ratio_label(ratio):
    if ratio is None:
        return ("-", "na")
    if ratio >= 1.5:
        return ("历史高位区", "hot")
    if ratio >= 1.15:
        return ("高于常态", "warm")
    if ratio >= 0.85:
        return ("接近常态", "norm")
    return ("低于常态", "cool")


# ------------------------------------------------------------------ 单票分析
def analyze(cli, ticker_raw, days_ahead, hist_years):
    sym = norm_symbol(ticker_raw)
    row = {"symbol": sym, "name": display_symbol(sym), "ok": False, "err": None}
    try:
        spot = get_quote(cli, sym)
        kcount = min(1000, max(320, int(hist_years * 262) + 60))
        kl = get_klines(cli, sym, count=kcount)
        today = dt.date.today()
        start = (today - dt.timedelta(days=int(hist_years * 365.25))).isoformat()
        end = (today + dt.timedelta(days=days_ahead)).isoformat()
        cal = get_calendar(cli, sym, start, end)
        today_iso = today.isoformat()
        upcoming = [e for e in cal
                    if e["date"] >= today_iso and str(e.get("eps_act") or "") in ("", "None", "待公布")]
        past = [e for e in cal if e["date"] < today_iso]
        nxt = upcoming[0] if upcoming else None
        hist = realized_moves(kl, past, lookback=8)
        absm = sorted(abs(h["move"]) for h in hist)
        med = absm[len(absm) // 2] if absm else None
        imp = None
        if nxt:
            try:
                imp = implied_move(cli, sym, spot, nxt["date"])
            except RadarError as exc:
                row["err"] = str(exc)[:160]
        ratio = None
        if imp and med and med > 0:
            ratio = imp["move_pct"] / med
        row.update({
            "ok": True, "spot": spot,
            "next_date": nxt["date"] if nxt else None,
            "next_timing": ("盘后" if nxt["timing"] == "pm" else "盘前") if nxt else None,
            "next_period": nxt["period"] if nxt else None,
            "eps_est": nxt.get("eps_est") if nxt else None,
            "rev_est": nxt.get("rev_est") if nxt else None,
            "hist": hist, "hist_med": med,
            "hist_mean": (sum(absm) / len(absm)) if absm else None,
            "implied": imp, "ratio": ratio,
            "hv30": hv_pct([r["close"] for r in kl]),
            "klines_n": len(kl),
        })
    except RadarError as exc:
        row["err"] = str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        row["err"] = "unexpected: %r" % exc
    return row


# ------------------------------------------------------------------ 控制台输出
def fmt_pct(v, digits=1, sign=False):
    if v is None:
        return "-"
    s = ("+" if sign and v > 0 else "") + ("%.*f" % (digits, v)) + "%"
    return s


def print_console(rows):
    head = ("%-7s %-9s %-16s %-9s %-9s %-7s %-13s %s"
            % ("SYMBOL", "SPOT", "NEXT EARNINGS", "IMPL±%", "HISTmed%", "RATIO", "IV/HV30", "NOTE"))
    print(head)
    print("-" * len(head))
    for r in rows:
        if not r.get("ok"):
            print("%-7s  ERROR: %s" % (r["name"], r.get("err")))
            continue
        nxt = "-"
        if r.get("next_date"):
            nxt = "%s %s" % (r["next_date"], r["next_timing"])
        imp_s = fmt_pct(r["implied"]["move_pct"]) if r.get("implied") else "-"
        med_s = fmt_pct(r.get("hist_med"))
        ratio_s = ("%.2f" % r["ratio"]) if r.get("ratio") else "-"
        iv = r["implied"]["iv"] if r.get("implied") else None
        hv = r.get("hv30")
        ivhv = "%s/%s" % (fmt_pct(iv, 0), fmt_pct(hv, 0)) if (iv or hv) else "-"
        label, _ = ratio_label(r.get("ratio"))
        note = label if r.get("ratio") else (r.get("err") or "")
        if r.get("ratio") is None and r.get("implied"):
            note = "样本不足"
        if not r.get("next_date"):
            note = "窗口内无已确认财报"
        elif not r.get("implied") and not r.get("err"):
            note = "无期权数据"
        print("%-7s %-9.2f %-16s %-9s %-9s %-7s %-13s %s"
              % (r["name"], r["spot"], nxt, imp_s, med_s, ratio_s, ivhv, note))


# ------------------------------------------------------------------ HTML 报告
CSS = """
:root{
  --bg:#0B0C0E; --panel:#131416; --panel2:#17181B; --line:#26282C;
  --text:#E9E7E2; --muted:#8A8F98; --amber:#F5A623; --red:#E15241; --green:#149E61;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,Roboto,sans-serif;padding:28px 4vw 60px}
.wrap{max-width:1180px;margin:0 auto}
.hd{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px;border-bottom:2px solid var(--amber);padding-bottom:14px;margin-bottom:22px}
.hd h1{font-size:24px;letter-spacing:1px}
.hd h1 .en{color:var(--amber);font-size:14px;letter-spacing:3px;margin-left:10px;vertical-align:middle}
.hd .meta{color:var(--muted);font-size:12px;text-align:right}
table.sum{width:100%;border-collapse:collapse;margin-bottom:26px;background:var(--panel);border:1px solid var(--line)}
table.sum th,table.sum td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
table.sum th{color:var(--muted);font-weight:600;font-size:12px;letter-spacing:.5px}
table.sum td:first-child,table.sum th:first-child{text-align:left}
table.sum tr:last-child td{border-bottom:none}
.mono{font-family:var(--mono)}
.pos{color:var(--green)} .neg{color:var(--red)} .amb{color:var(--amber)}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid var(--line)}
.tag.hot{color:var(--red);border-color:var(--red)}
.tag.warm{color:var(--amber);border-color:var(--amber)}
.tag.norm{color:var(--text)}
.tag.cool{color:var(--green);border-color:var(--green)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(540px,1fr));gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.card .ct{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px}
.card .ct .sym{font-size:19px;font-weight:700}
.card .ct .spot{font-family:var(--mono);color:var(--muted)}
.kv{display:grid;grid-template-columns:repeat(3,1fr);gap:8px 14px;margin-bottom:12px}
.kv .k{color:var(--muted);font-size:12px}
.kv .v{font-family:var(--mono);font-size:15px}
.bars{margin-top:6px}
.bars .t{color:var(--muted);font-size:12px;margin-bottom:6px}
.brow{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
.brow .d{width:82px;color:var(--muted);font-family:var(--mono)}
.brow .barwrap{position:relative;flex:1;height:14px;background:var(--panel2);border-radius:3px;overflow:hidden}
.brow .bar{position:absolute;top:0;bottom:0}
.brow .val{width:64px;text-align:right;font-family:var(--mono)}
.impl{margin-top:8px;font-size:12px;color:var(--muted)}
.impl b{color:var(--amber);font-family:var(--mono)}
.foot{margin-top:34px;border-top:1px solid var(--line);padding-top:14px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px}
.err{color:var(--red);font-size:12px}
"""


def bar_html(hist, implied_pct):
    """最后一列历史波动条（居中零轴）。"""
    if not hist:
        return '<div class="bars"><div class="t">历史财报日波动：无样本</div></div>'
    mx = max([abs(h["move"]) for h in hist] + ([implied_pct] if implied_pct else []) + [1.0])
    parts = ['<div class="bars"><div class="t">近 %d 季财报日实际波动（红=跌 · 绿=涨 · 橙=跨式隐含）</div>' % len(hist)]
    for h in hist:
        w = abs(h["move"]) / mx * 50.0
        cls = "pos" if h["move"] >= 0 else "neg"
        if h["move"] >= 0:
            bar = '<div class="bar pos" style="left:50%%;width:%.1f%%;background:var(--green)"></div>' % w
        else:
            bar = '<div class="bar neg" style="left:%.1f%%;width:%.1f%%;background:var(--red)"></div>' % (50 - w, w)
        parts.append('<div class="brow"><span class="d">%s</span><div class="barwrap">%s</div><span class="val %s">%s</span></div>'
                     % (h["date"][2:], bar, cls, ("%+.1f%%" % h["move"])))
    if implied_pct:
        w = implied_pct / mx * 50.0
        parts.append('<div class="brow"><span class="d">隐含</span><div class="barwrap">'
                     '<div class="bar" style="left:50%%;width:%.1f%%;background:var(--amber);opacity:.85"></div></div>'
                     '<span class="val amb">±%.1f%%</span></div>' % (min(w, 50), implied_pct))
    parts.append("</div>")
    return "".join(parts)


def build_html(rows, days_ahead, generated):
    ok_rows = [r for r in rows if r.get("ok")]
    ok_rows.sort(key=lambda r: (r.get("ratio") is None, -(r.get("ratio") or 0)))

    s = []
    s.append("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">")
    s.append("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    s.append("<title>财报雷达 · Earnings Radar</title><style>%s</style></head><body><div class=\"wrap\">" % CSS)
    s.append('<div class="hd"><h1>财报雷达<span class="en">EARNINGS RADAR</span></h1>'
             '<div class="meta">生成时间：%s<br>数据源：Longbridge · 未来 %d 天窗口 · 仅供研究参考</div></div>' % (generated, days_ahead))

    # ---- 汇总表
    s.append('<table class="sum"><tr><th>标的</th><th>现价</th><th>下个财报</th><th>隐含波动</th>'
             '<th>历史中位</th><th>隐含/历史</th><th>ATM IV</th><th>HV30</th><th>判读</th></tr>')
    for r in ok_rows:
        nxt = "-"
        if r.get("next_date"):
            nxt = "%s %s" % (r["next_date"], r["next_timing"])
        imp_pct = r["implied"]["move_pct"] if r.get("implied") else None
        iv = r["implied"]["iv"] if r.get("implied") else None
        label, tag = ratio_label(r.get("ratio"))
        s.append("<tr><td><b>%s</b></td><td class=\"mono\">%.2f</td><td class=\"mono\">%s</td>"
                 "<td class=\"mono amb\">%s</td><td class=\"mono\">%s</td><td class=\"mono\">%s</td>"
                 "<td class=\"mono\">%s</td><td class=\"mono\">%s</td><td><span class=\"tag %s\">%s</span></td></tr>"
                 % (r["name"], r["spot"], nxt,
                    ("±%.1f%%" % imp_pct) if imp_pct else "-",
                    fmt_pct(r.get("hist_med")),
                    ("%.2fx" % r["ratio"]) if r.get("ratio") else "-",
                    fmt_pct(iv, 0),
                    fmt_pct(r.get("hv30"), 0),
                    tag, label))
    s.append("</table>")

    # ---- 卡片
    s.append('<div class="cards">')
    for r in ok_rows:
        s.append('<div class="card"><div class="ct"><span class="sym">%s</span>'
                 '<span class="spot">$%.2f</span></div>' % (r["name"], r["spot"]))
        nxt_txt = "近期无财报（未来 %d 天内）" % days_ahead
        if r.get("next_date"):
            nxt_txt = "%s（%s）· %s" % (r["next_date"], r["next_timing"], r.get("next_period") or "")
        s.append('<div class="kv"><div><div class="k">下个财报</div><div class="v" style="font-size:13px">%s</div></div>' % nxt_txt)
        if r.get("next_date") and (r.get("eps_est") or r.get("rev_est")):
            s.append('<div><div class="k">预期 EPS / 营收</div><div class="v" style="font-size:13px">%s / %s</div></div>'
                     % (r.get("eps_est") or "-", r.get("rev_est") or "-"))
        imp = r.get("implied")
        s.append('<div><div class="k">隐含波动（跨式定价）</div><div class="v amb">%s</div></div>'
                 % (("±%.1f%%" % imp["move_pct"]) if imp else "-"))
        s.append("</div>")
        s.append('<div class="kv"><div><div class="k">历史中位 |波动|</div><div class="v">%s</div></div>'
                 % fmt_pct(r.get("hist_med")))
        s.append('<div><div class="k">隐含 / 历史</div><div class="v">%s</div></div>'
                 % (("%.2fx · " % r["ratio"]) + ratio_label(r["ratio"])[0] if r.get("ratio") else "-"))
        ivhv = "%s / %s" % (fmt_pct(imp["iv"], 0) if imp else "-", fmt_pct(r.get("hv30"), 0))
        s.append('<div><div class="k">ATM IV / HV30</div><div class="v">%s</div></div></div>' % ivhv)
        s.append(bar_html(r.get("hist") or [], imp["move_pct"] if imp else None))
        if imp:
            s.append('<div class="impl">期权定价：<b>%s</b> 到期 · 行权价 <b>%.1f</b> · 跨式成本 <b>$%.2f</b></div>'
                     % (imp["exp"], imp["strike"], imp["straddle"]))
        if r.get("err"):
            s.append('<div class="err">期权数据缺失：%s</div>' % r["err"])
        s.append("</div>")
    s.append("</div>")

    # ---- 错误行
    bad = [r for r in rows if not r.get("ok")]
    if bad:
        s.append('<div style="margin-top:18px">')
        for r in bad:
            s.append('<div class="err">%s：%s</div>' % (r["name"], r.get("err")))
        s.append("</div>")

    s.append('<div class="foot"><div>财报雷达 Earnings Radar v%s · 基于 Longbridge CLI 数据<br>'
             '隐含波动 = 财报后首个到期日 ATM 跨式成本 / 现价；仅作事件定价参考，不构成投资建议。</div>'
             '<div>Le Refuge 金融情报</div></div>' % VERSION)
    s.append("</div></body></html>")
    return "".join(s)


# ------------------------------------------------------------------ 主流程
def main(argv=None):
    ap = argparse.ArgumentParser(description="财报雷达 · Earnings Radar v%s — 财报事件 + 期权隐含波动分析" % VERSION)
    ap.add_argument("--tickers", help="逗号分隔的标的清单，如 NKE,MU,COST")
    ap.add_argument("--tickers-file", help="每行一个标的的文件")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="未来窗口天数（默认 %d）" % DEFAULT_DAYS)
    ap.add_argument("--history-years", type=int, default=DEFAULT_HIST_YEARS, help="历史统计年数（默认 %d）" % DEFAULT_HIST_YEARS)
    ap.add_argument("--out", help="HTML 输出路径（默认 earnings_radar_YYYYMMDD.html）")
    ap.add_argument("--csv", help="CSV 输出路径（默认与 HTML 同名）")
    ap.add_argument("--cli", default=os.environ.get("LONGBRIDGE_CLI", "longbridge"), help="Longbridge CLI 可执行文件路径")
    args = ap.parse_args(argv)

    tickers = []
    if args.tickers:
        tickers += [t.strip() for t in args.tickers.split(",") if t.strip()]
    if args.tickers_file:
        try:
            with open(args.tickers_file, encoding="utf-8") as f:
                tickers += [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
        except OSError as exc:
            print("无法读取 %s：%s" % (args.tickers_file, exc))
            return 2
    if not tickers:
        ap.print_help()
        return 2
    seen = set()
    tickers = [t for t in tickers if not (t.upper() in seen or seen.add(t.upper()))]

    today = dt.date.today().isoformat()
    print("财报雷达 v%s · %d 只标的 · 数据源 Longbridge CLI\n" % (VERSION, len(tickers)))
    rows = []
    for t in tickers:
        print("分析 %s ..." % t, end="", flush=True)
        r = analyze(args.cli, t, args.days, args.history_years)
        print(" done" if r.get("ok") else " FAILED")
        rows.append(r)

    print()
    print_console(rows)

    out_html = args.out or ("earnings_radar_%s.html" % today.replace("-", ""))
    out_csv = args.csv or os.path.splitext(out_html)[0] + ".csv"
    for path in (out_html, out_csv):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    html = build_html(rows, args.days, dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "spot", "next_earnings", "timing", "implied_move_pct", "straddle",
                    "atm_iv_pct", "hv30_pct", "hist_median_abs_pct", "ratio", "read", "expiry"])
        for r in rows:
            if not r.get("ok"):
                w.writerow([r["name"], "", "", "", "", "", "", "", "", "", "ERROR:" + str(r.get("err")), ""])
                continue
            imp = r.get("implied") or {}
            label, _ = ratio_label(r.get("ratio"))
            w.writerow([r["name"], "%.2f" % r["spot"], r.get("next_date") or "", r.get("next_timing") or "",
                        "%.2f" % imp["move_pct"] if imp else "", "%.2f" % imp["straddle"] if imp else "",
                        "%.1f" % imp["iv"] if imp.get("iv") else "", "%.1f" % r["hv30"] if r.get("hv30") else "",
                        "%.2f" % r["hist_med"] if r.get("hist_med") else "",
                        "%.2f" % r["ratio"] if r.get("ratio") else "", label, imp.get("exp") or ""])
    print("\nHTML: %s\nCSV : %s" % (out_html, out_csv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
