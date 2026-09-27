"""Automatic volume assumptions and initial-inclusive graphs for real cost estimates.

The offline illustrative renderer deliberately does not use this module.
"""

from copy import deepcopy
from decimal import Decimal
import html
import json
import math
from pathlib import Path

from .costs import VARIANTS, build_report


STYLES = (("teacher", "教師 (teacher)", "#163b65", ""),
          ("base", "学習前 (base)", "#a14b00", "2 5"),
          ("fine_tuned", "学習済み (fine_tuned)", "#146e4a", "10 5"))
SAMPLES = 80
MAX_MONTHS = 1200
PANEL_HEIGHT = 610


def _finite(value):
    if not math.isfinite(value):
        raise ValueError("Derived graph cost or range exceeds finite numeric range")
    return value


def _multiply(left, right):
    result = _finite(left * right)
    if left and right and result == 0:
        raise ValueError("Derived graph value underflows numeric range")
    return result


def _divide(left, right):
    result = _finite(left / right)
    if left and result == 0:
        raise ValueError("Derived graph value underflows numeric range")
    return result


def _tree_finite(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _finite(value)
    elif isinstance(value, dict):
        for child in value.values():
            _tree_finite(child)
    elif isinstance(value, list):
        for child in value:
            _tree_finite(child)


def _nice(value):
    """Round up to 1, 2 or 5 times a power of ten without clamping small values."""
    if value <= 0:
        raise ValueError("Automatic range must be positive")
    scale = Decimal(10) ** Decimal(str(value)).adjusted()
    fraction = Decimal(str(value)) / scale
    factor = next(item for item in (1, 2, 5, 10) if fraction <= item)
    result = _finite(float(factor * scale))
    if result == 0:
        raise ValueError("Automatic range underflows numeric range")
    return result


def _points(maximum, intersections=()):
    return sorted({*(_multiply(maximum, i / SAMPLES) for i in range(SAMPLES + 1)),
                   *(x for x in intersections if x is not None and 0 <= x <= maximum)})


def _format(value):
    if value is None:
        return "不明"
    if value == 0:
        return "0"
    if 0.0001 <= abs(value) < 1_000_000_000:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.5g}"


def _cost_axis(values):
    peak = max((value for value in values if value is not None), default=0)
    if not peak:
        return {"minimum": 0, "maximum": 1, "ticks": [0, 0.2, 0.4, 0.6, 0.8, 1]}
    desired = _divide(peak, 5)
    scale = Decimal(10) ** Decimal(str(desired)).adjusted()
    fraction = Decimal(str(desired)) / scale
    factor = max(item for item in (1, 2, 2.5, 5, 10) if Decimal(str(item)) <= fraction)
    step = _finite(float(Decimal(str(factor)) * scale))
    if step == 0:
        raise ValueError("Cost axis step underflows numeric range")
    intervals = math.ceil(_divide(peak, step))
    maximum = _multiply(step, intervals)
    return {"minimum": 0, "maximum": maximum,
            "ticks": [_multiply(step, i) for i in range(intervals + 1)]}


def _report(data, volume, months=12):
    prepared = {**data, "monthly_requests": volume, "horizon_months": months}
    try:
        report = build_report(prepared)
        _tree_finite(report)
    except OverflowError as exc:
        raise ValueError("Derived costs exceed finite numeric range") from exc
    # Do not let the legacy floating-point calculation turn a tiny paid rate into free use.
    for name, raw in data["variants"].items():
        if raw["inference_mode"] != "tokens":
            continue
        usage, rates = raw["usage_per_request"], raw["prices"]
        values = [usage.get(k) for k in ("input_tokens", "cached_input_tokens", "output_tokens")]
        values += [rates.get(k) for k in ("input_per_million", "cached_input_per_million",
                                         "output_per_million")]
        if None not in values:
            inp, cached, out, ir, cr, outr = (Decimal(str(v)) for v in values)
            expected = ((inp - cached) * ir + cached * cr + out * outr) / 1_000_000
            if expected and report["variants"][name]["inference_per_request"] == 0:
                raise ValueError("Inference cost underflows numeric range")
    return report


def _line(variant, point, cumulative=False, include_initial=True):
    slope = variant["monthly_cost"] if cumulative else variant["per_request"]
    initial = ((variant["initial_total"] if include_initial else 0) if cumulative
               else variant["monthly_fixed"])
    return (None if slope is None or initial is None
            else _finite(initial + _multiply(slope, point)))


def _coincident(variants, cumulative=False, include_initial=True):
    keys = (("initial_total", "monthly_cost") if include_initial else ("monthly_cost",)) if cumulative else (
        "monthly_fixed", "per_request")
    pairs = []
    for index, first in enumerate(VARIANTS):
        for second in VARIANTS[index + 1:]:
            if all(variants[first][key] is not None
                   and variants[first][key] == variants[second][key] for key in keys):
                pairs.append(f"{first} / {second}")
    return ["同一線が重なっています: " + ", ".join(pairs)] if pairs else []


def adaptive_graph_plan(cost_input):
    """Select bounded sampling, three volumes and independent payback horizons."""
    baseline = _report(cost_input, 0)
    variants = baseline["variants"]
    comparable = cost_input.get("evaluation_bridge", {}).get("comparable", True)
    crossovers = {}
    volume_comparisons = {}
    for name in ("base", "fine_tuned"):
        teacher, student = variants["teacher"], variants[name]
        cross = None
        if comparable and all(v[key] is not None for v in (teacher, student)
                              for key in ("per_request", "monthly_fixed")):
            marginal = teacher["per_request"] - student["per_request"]
            fixed = student["monthly_fixed"] - teacher["monthly_fixed"]
            if marginal:
                intersection = _divide(fixed, marginal)
                cross = intersection if intersection > 0 else None
        crossovers[name] = cross
        if not comparable:
            status, note = "not_comparable", "評価条件を比較できないため月額交点の判断不可"
        elif any(v[key] is None for v in (teacher, student)
                 for key in ("per_request", "monthly_fixed")):
            status, note = "unknown_costs", "費用・単価が不明のため月額交点の判断不可"
        elif cross is not None:
            status = "positive_crossover"
            note = f"教師との月額交点 {_format(cross)} 件/月（丸印・縦破線）"
        elif teacher["per_request"] == student["per_request"] and teacher["monthly_fixed"] == student["monthly_fixed"]:
            status, note = "coincident", "教師と月額費用が同一（全件数で線が重なる）"
        else:
            cheaper = (student["monthly_fixed"] < teacher["monthly_fixed"]
                       or (student["monthly_fixed"] == teacher["monthly_fixed"]
                           and student["per_request"] < teacher["per_request"]))
            status = ("student_cheaper_at_all_positive_volumes" if cheaper
                      else "student_more_expensive_at_all_positive_volumes")
            note = f"正の月額交点なし（正の件数では常に教師より{'安い' if cheaper else '高い'}）"
        volume_comparisons[name] = {"status": status, "note": f"{name}: {note}"}
    positive = [value for value in crossovers.values() if value is not None]
    if positive:
        largest = max(positive)
        volumes = [_nice(_multiply(largest, factor)) for factor in (0.25, 1.25, 4)]
        reason = "Positive operating-cost intersection(s); rounded volumes below and above the largest."
        volume_basis = "Automatically selected volume assumptions from estimated operating-cost intersections"
    else:
        volumes = [100, 1000, 10000]
        reason = ("No known positive comparable operating-cost intersection. "
                  "100 / 1000 / 10000 are illustrative VOLUME ASSUMPTIONS, not fictional costs.")
        volume_basis = "Illustrative volume assumptions; all costs still come from the current estimate"
    maximum = _nice(_multiply(max(volumes), 1.25))
    scenarios = []
    for label, volume in zip(("low", "middle", "high"), volumes):
        report = _report(cost_input, volume)
        comparisons = deepcopy(report["comparisons"])
        positive_paybacks = [c["payback_months"] for c in comparisons.values()
                             if c["payback_months"] is not None and c["payback_months"] > 0]
        longest = max(positive_paybacks, default=0)
        horizon = (MAX_MONTHS if longest > MAX_MONTHS / 1.5
                   else min(MAX_MONTHS, max(1, math.ceil(_nice(longest * 1.5)))) if longest
                   else 12)
        notes, intersections = [], []
        for name, comparison in comparisons.items():
            savings, extra = comparison["monthly_savings"], comparison["incremental_initial"]
            crossing = None
            if savings is not None and extra is not None and savings:
                candidate = _divide(extra, savings)
                if candidate >= 0:
                    crossing = candidate
                    intersections.append(candidate)
            payback = comparison["payback_months"]
            if not comparable:
                status = "not_comparable"
                text = "評価条件を比較できないため回収判断不可"
            elif savings is None or extra is None:
                status = "unknown_costs"
                text = "費用不明のため回収判断不可"
            elif savings <= 0:
                status = "no_positive_monthly_savings"
                text = "月額節約なし（回収を示さない）"
            elif payback > horizon:
                status = "outside_horizon"
                text = f"回収 {_format(payback)} か月 — 表示範囲外（上限1200か月）"
            else:
                status = "within_horizon"
                text = f"回収 {_format(payback)} か月"
            if crossing is not None:
                text += f" / 累積交点 {_format(crossing)} か月"
            comparison.update(operating_crossover_requests=crossovers[name],
                              cumulative_intersection_months=crossing,
                              payback_status=status,
                              payback_within_horizon=None if payback is None else payback <= horizon)
            notes.append(f"{name}: {text}")
        notes += _coincident(report["variants"], cumulative=True)
        scenarios.append({
            "id": label, "monthly_requests": volume, "horizon_months": horizon,
            "horizon_reason": "Positive payback times 1.5, rounded up to nice whole months (minimum 1); "
                              "default 12 without useful positive payback; cap 1200",
            "points": _points(horizon, intersections),
            "cost_axis": _cost_axis(_line(v, horizon, cumulative=True)
                                    for v in report["variants"].values()),
            "operating_cost_axis": _cost_axis(_line(v, horizon, cumulative=True, include_initial=False)
                                              for v in report["variants"].values()),
            "variants": report["variants"], "comparisons": comparisons, "notes": notes,
        })
    plan = {
        "schema": "auto-cost-graphs-v1", "currency": cost_input["currency"],
        "assumptions": {"volume_basis": volume_basis, "not_actual_user_usage": True,
                        "cost_basis": "Current actual evaluation cohort and public-rate estimate; not invoices"},
        "selection_reason": reason, "selected_scenario": "middle",
        "summary_basis": "The legacy report summary uses the automatically selected middle scenario",
        "sample_intervals": SAMPLES, "maximum_horizon_months": MAX_MONTHS,
        "comparable": comparable,
        "volume_axis": {"minimum": 0, "maximum": maximum,
                        "points": _points(maximum, positive), "crossovers": crossovers,
                        "comparisons": volume_comparisons,
                        "cost_axis": _cost_axis(_line(v, maximum) for v in variants.values())},
        "scenarios": scenarios,
    }
    _tree_finite(plan)
    return plan


def _rows(variants, points, cumulative=False, include_initial=True):
    key = "month" if cumulative else "monthly_requests"
    return [{key: point, **{name: _line(variants[name], point, cumulative, include_initial)
                           for name in VARIANTS}} for point in points]


def _panel(rows, title, xlabel, ylabel, notes, cost_axis, crossings=None, offset=0):
    esc = html.escape
    xkey = next(iter(rows[0]))
    xmax = max(row[xkey] for row in rows)
    known = [row[name] for row in rows for name in VARIANTS if row[name] is not None]
    peak = max(known, default=0)
    ymax = cost_axis["maximum"]
    parts = [f'<g transform="translate(0 {offset})">',
             f'<text x="110" y="30" font-size="19">{esc(title)}</text>',
             '<text x="110" y="54" font-size="12">PUBLIC RETAIL ESTIMATE; NOT INVOICE / 評価コホートの概算・件数は仮定</text>']
    for tick in cost_axis["ticks"]:
        y = 365 - tick / ymax * 280
        parts.extend([
            f'<path d="M110 {y} H760" stroke="#ddd"/>',
            f'<text x="101" y="{y+4}" text-anchor="end" font-size="12">{_format(tick)}</text>',
        ])
    for i in range(6):
        parts.append(f'<text x="{110+i*130}" y="391" text-anchor="middle" '
                     f'font-size="12">{_format(xmax*(i/5))}</text>')
    parts.append('<path d="M110 85 V365 H760" fill="none" stroke="#333"/>')
    for index, (name, label, color, dash) in enumerate(STYLES):
        coords = " ".join(f'{110 + row[xkey]/xmax*650:.5f},{365-row[name]/ymax*280:.5f}'
                          for row in rows if row[name] is not None)
        if coords:
            parts.append(f'<polyline data-variant="{name}" points="{coords}" fill="none" '
                         f'stroke="{color}" stroke-width="3" stroke-dasharray="{dash}"/>')
        y = 106 + index * 35
        parts.append(f'<path d="M790 {y} h32" stroke="{color}" stroke-width="3" stroke-dasharray="{dash}"/>')
        suffix = " — 不明" if not coords else ""
        parts.append(f'<text x="831" y="{y+4}" font-size="12">{esc(label + suffix)}</text>')
    for index, (name, _, color, _) in enumerate(STYLES[1:]):
        crossing = (crossings or {}).get(name)
        if crossing is None or not 0 < crossing <= xmax:
            continue
        row = next(row for row in rows if row[xkey] == crossing)
        if row["teacher"] is None or row[name] is None:
            continue
        x, y = 110 + crossing / xmax * 650, 365 - row["teacher"] / ymax * 280
        parts.append(f'<path data-crossing-guide="{name}" d="M{x:.5f} 365 V{y:.5f}" '
                     f'fill="none" stroke="{color}" stroke-dasharray="4 4" opacity="0.7"/>')
        parts.append(f'<circle data-crossing="{name}" cx="{x:.5f}" cy="{y:.5f}" r="{5+index*2}" '
                     f'fill="white" fill-opacity="0.7" stroke="{color}" stroke-width="2"/>')
    parts.extend([
        f'<text x="435" y="426" text-anchor="middle" font-size="14">{esc(xlabel)}</text>',
        f'<text transform="translate(24 225) rotate(-90)" text-anchor="middle" font-size="14">{esc(ylabel)}</text>',
    ])
    if not peak:
        notes = [("既知の費用はすべて0です。" if known else "全構成の費用が不明です。"), *notes]
    for index, note in enumerate(notes):
        parts.append(f'<text x="110" y="{454+index*21}" font-size="12">{esc(note)}</text>')
    parts.append("</g>")
    return "\n".join(parts)


def _svg(title, panels):
    height = PANEL_HEIGHT * len(panels)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="{height}" '
            f'viewBox="0 0 1080 {height}" role="img" aria-labelledby="title description">\n'
            f'<title id="title">{html.escape(title)}</title>\n'
            '<desc id="description">実測コホートの使用量と公開単価による概算。'
            '教師は青の実線、学習前は橙の点線、学習済みは緑の破線。'
            '不明な費用は描画しない。件数・期間は自動選択した仮定で、請求額でも採用判断でもない。'
            '正確な交点と費用は対応するCSVおよびgraph-plan.jsonを参照。</desc>\n'
            f'<rect width="1080" height="{height}" fill="white"/>\n'
            '<g font-family="sans-serif" fill="#222">\n' + "\n".join(panels) + "\n</g>\n</svg>\n")


def graph_charts(report):
    """Return bounded CSV series and SVGs; no change to illustrative chart output."""
    plan = report["graph_plan"]
    axis = plan["volume_axis"]
    volume_rows = _rows(report["variants"], axis["points"])
    notes = ["3構成を比較。線がない構成は費用不明です。件数は利用実績ではありません。"]
    notes.extend(comparison["note"] for comparison in axis["comparisons"].values())
    notes += _coincident(report["variants"])
    charts = {"volume": (volume_rows, _svg("月間件数と運用費用", [
        _panel(volume_rows, "月間件数による運用費用の比較（初期費用を除く）",
               "月間件数（自動選択した仮定）", f'{report["currency"]} / 月', notes,
               axis["cost_axis"], axis["crossovers"])]))}
    panels, all_rows = [], []
    for index, scenario in enumerate(plan["scenarios"]):
        rows = _rows(scenario["variants"], scenario["points"], cumulative=True)
        title = f'初期費用を含む累積費用 — 月間 {_format(scenario["monthly_requests"])} 件'
        notes = [*scenario["notes"], "各線は初期費用から開始し、月額運用費を毎月加算します。",
                 "品質は別途確認が必要です。費用差のみで採用を判断しません。"]
        crossings = {name: comparison["cumulative_intersection_months"]
                     for name, comparison in scenario["comparisons"].items()}
        panels.append(_panel(rows, title, "経過月数", report["currency"], notes,
                             scenario["cost_axis"], crossings,
                             offset=index * PANEL_HEIGHT))
        all_rows.extend({"scenario": scenario["id"], "monthly_requests": scenario["monthly_requests"],
                         **row} for row in rows)
        if scenario["id"] == plan["selected_scenario"]:
            operating_rows = _rows(scenario["variants"], scenario["points"], cumulative=True,
                                   include_initial=False)
            operating_title = (f'運用費用のみの累積 — 月間 {_format(scenario["monthly_requests"])} 件'
                               "（自動選択・中央）")
            operating_notes = [
                "初期費用を含みません。各線は0から開始し、月額運用費のみを毎月加算します。",
                "初期費用の回収判断は、初期費用を含む payback.svg を参照してください。",
                "品質は別途確認が必要です。費用差のみで採用を判断しません。",
                *_coincident(scenario["variants"], cumulative=True, include_initial=False),
            ]
            charts["cumulative"] = (operating_rows, _svg("中央シナリオの累積運用費用（初期費用を除く）", [
                _panel(operating_rows, operating_title, "経過月数", report["currency"], operating_notes,
                       scenario["operating_cost_axis"])]))
    charts["payback"] = (all_rows, _svg("3つの月間件数での費用回収比較（初期費用を含む）", panels))
    return charts


def write_graph_summary(report, output):
    output = Path(output)
    plan = report["graph_plan"]
    (output / "graph-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n")
    estimate = report.get("estimate", {})
    selection = (
        "使用量と公開単価から求めた月額費用の交点を基準に、交点より少ない件数と多い件数を含む"
        "3つの月間件数を自動選択しました。"
        if any(cross is not None for cross in plan["volume_axis"]["crossovers"].values())
        else "比較可能な正の月額交点が得られなかったため、月間100件・1,000件・10,000件を"
             "比較用の仮定として選びました。費用の単価を仮定で補ったものではありません。")
    lines = [
        "# 評価結果に基づく費用比較グラフ", "",
        "保存済みの実際の評価コホート使用量と現在の公開単価による概算です。"
        "本番の利用実績・請求書・人間確認済み成功を示すものではありません。", "",
        "## この比較に使った条件", "",
        f"- 将来の配置時間: 1日 {_format(estimate.get('hours_per_day'))} 時間、"
        f"平均月 {_format(estimate.get('hours_per_month'))} 時間（365日 / 12か月）。",
        f"- 学習済み構成だけに加算する追加初期費用: {_format(estimate.get('additional_initial_cost'))} USD。",
        f"- 学習済み構成だけに加算する追加月額費用: {_format(estimate.get('additional_monthly_cost'))} USD / 月。",
        "- 配置時間は将来の概算条件です。配置の起動・停止は行わず、実験中の配置費は実際の経過時間で計上します。",
        "- 追加費用の既定値0は人件費などが未計上という意味で、無料という意味ではありません。", "",
        "## 月間件数による運用費用", "",
        "![月間件数と3構成の月額運用費](volume.svg)", "",
        selection, "",
        "月間件数は自動選択した仮定です。追加費用は学習済み構成の採用に伴う増分USD費用のみです。", "",
        "## 初期費用を含む3つの累積費用シナリオ", "",
        "![3つの月間件数とそれぞれの期間での累積費用・回収](payback.svg)", "",
        "各線の切片は初期費用、傾きは月額運用費です。節約なし・不明は回収として表示しません。"
        "回収が1200か月の上限を超える場合は「表示範囲外」であり「回収なし」ではありません。", "",
    ]
    for scenario in plan["scenarios"]:
        lines += [f'### {scenario["id"]}: 月間 {_format(scenario["monthly_requests"])} 件 / '
                  f'{scenario["horizon_months"]} か月', ""]
        lines += [f"- {note.replace('fine_tuned:', '学習済み:').replace('base:', '学習前:')}"
                  for note in scenario["notes"]]
        lines.append("")
    lines += [
        "## 品質との比較（採用判断ではありません）", "",
        "費用が低くても品質が同等とは限りません。以下は保存された評価記録の確認状況です。"
        "自動採点を人間確認済み成功と読み替えないでください。", "",
    ]
    for name, variant in report["variants"].items():
        label = {"teacher": "教師", "base": "学習前", "fine_tuned": "学習済み"}[name]
        quality = {"pass": "品質基準を満たす（記録上）", "fail": "品質基準を満たさない",
                   "unreviewed": "品質基準は未確認"}[variant["quality_gate"]]
        review = "完了（記録上）" if variant["review_complete"] else "未完了"
        lines.append(f"- {label}: {quality}。人間によるレビューは{review}です。")
    lines += ["", "採用判断は保留です。品質・費用・比較条件を人間が確認してから判断してください。",
              "詳しい保留理由は [判断記録](report.json)、"
              "単価・使用量の不足情報は [概算の詳細](estimate.md) を参照してください。", "",
              "## 計算根拠", "",
              "- [graph-plan.json](graph-plan.json): 自動範囲・件数・期間・理由・各構成の費用と交点",
              "- [report.json](report.json) / [costs.csv](costs.csv): 自動選択した中央シナリオの要約",
              "- [cumulative.svg](cumulative.svg): 中央シナリオの累積運用費用（初期費用を除く）",
              "- [volume.csv](volume.csv) / [payback.csv](payback.csv): 交点を含む描画値",
              "- [estimate.md](estimate.md) / [prices.json](prices.json) / "
              "[inventory.json](inventory.json) / [cost-ledger.json](cost-ledger.json): 使用量・単価・配賦"]
    (output / "graphs.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
