"""Aggregated scenario cost accounting with unknown-value propagation."""

import csv
import html
import json
import math
from pathlib import Path


VARIANTS = ("teacher", "base", "fine_tuned")
FIXED = ("model_hosting", "agent", "tools_logs_storage")
INITIAL = ("generation", "training", "evaluation", "other")


def number(value, label):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: expected a number or null")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label}: must be finite and nonnegative")
    return value


def total(values):
    return None if any(v is None for v in values) else sum(values)


def mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected an object")
    return value


def fields(obj, keys, label):
    mapping(obj, label)
    return {key: number(obj.get(key), f"{label}.{key}") for key in keys}


def _reject_nonfinite(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value < 0:
            raise ValueError("All numeric inputs must be finite and nonnegative")
    if isinstance(value, dict):
        for child in value.values():
            _reject_nonfinite(child)
    elif isinstance(value, list):
        for child in value:
            _reject_nonfinite(child)


def variant_cost(raw, volume):
    mapping(raw, "variant")
    mode = raw.get("inference_mode")
    prices = mapping(raw.get("prices", {}), "prices")
    if mode == "per_request":
        inference = number(prices.get("per_request"), "prices.per_request")
    elif mode == "tokens":
        usage = fields(raw.get("usage_per_request", {}),
                       ("input_tokens", "cached_input_tokens", "output_tokens"), "usage")
        rates = fields(prices, ("input_per_million", "cached_input_per_million",
                                "output_per_million"), "prices")
        inp, cached, out = usage.values()
        if inp is not None and cached is not None and cached > inp:
            raise ValueError("cached_input_tokens is a subset of input_tokens")
        if None in (*usage.values(), *rates.values()):
            inference = None
        else:
            inference = ((inp - cached) * rates["input_per_million"]
                         + cached * rates["cached_input_per_million"]
                         + out * rates["output_per_million"]) / 1_000_000
    else:
        raise ValueError("inference_mode must be per_request or tokens")
    variable = fields(raw.get("variable_fees_per_request", {}),
                      ("agent", "tools_logs_storage"), "variable_fees_per_request")
    fixed = fields(raw.get("monthly_fixed", {}), FIXED, "monthly_fixed")
    initial = fields(raw.get("initial", {}), INITIAL, "initial")
    per_request = total([inference, *variable.values()])
    monthly_fixed = total(list(fixed.values()))
    initial_total = total(list(initial.values()))
    monthly = (None if per_request is None or monthly_fixed is None
               else per_request * volume + monthly_fixed)
    quality = mapping(raw.get("quality", {}), "quality")
    complete = quality.get("review_complete", False)
    if not isinstance(complete, bool):
        raise ValueError("review_complete must be boolean")
    counts = fields(quality, ("reviewed_requests", "successful_requests"), "quality")
    reviewed, successes = counts.values()
    for key, value in counts.items():
        if value is not None and int(value) != value:
            raise ValueError(f"{key} must be an integer")
    if reviewed is not None and successes is not None and successes > reviewed:
        raise ValueError("successful_requests cannot exceed reviewed_requests")
    gate = quality.get("quality_gate", "unreviewed")
    if gate not in ("pass", "fail", "unreviewed"):
        raise ValueError("quality_gate must be pass, fail, or unreviewed")
    success_rate = (successes / reviewed if complete and reviewed and successes is not None
                    else None)
    success_cost = (monthly / (volume * success_rate)
                    if monthly is not None and volume and success_rate else None)
    return {
        "inference_per_request": inference,
        "variable_fees_per_request": variable, "monthly_fixed_components": fixed,
        "initial_components": initial, "per_request": per_request,
        "monthly_fixed": monthly_fixed, "initial_total": initial_total,
        "monthly_cost": monthly, "reviewed_success_rate": success_rate,
        "projected_cost_per_reviewed_success": success_cost,
        "quality_gate": gate, "review_complete": complete,
        "source": raw.get("source"),
    }


def compare(teacher, student):
    names = ("per_request", "monthly_fixed", "monthly_cost")
    if any(teacher[k] is None or student[k] is None for k in names):
        return {"status": "unknown_costs", "crossover_requests": None,
                "monthly_savings": None, "incremental_initial": None,
                "payback_months": None}
    marginal_savings = teacher["per_request"] - student["per_request"]
    fixed_extra = student["monthly_fixed"] - teacher["monthly_fixed"]
    savings = teacher["monthly_cost"] - student["monthly_cost"]
    extra = (student["initial_total"] - teacher["initial_total"]
             if teacher["initial_total"] is not None and student["initial_total"] is not None
             else None)
    crossover = fixed_extra / marginal_savings if marginal_savings > 0 and fixed_extra >= 0 else None
    if marginal_savings > 0:
        status = "volume_crossover" if fixed_extra >= 0 else "student_cheaper_at_all_volumes"
    elif marginal_savings == 0:
        status = "student_cheaper_at_all_volumes" if fixed_extra < 0 else "no_savings_crossover"
    else:
        # A student cheaper only at small volumes is not a scale-up savings crossover.
        status = "no_scale_up_savings_crossover"
    payback = max(0, extra) / savings if savings > 0 and extra is not None else None
    return {"status": status, "crossover_requests": crossover,
            "monthly_savings": savings, "incremental_initial": extra,
            "payback_months": payback,
            "payback_whole_months": math.ceil(payback) if payback is not None else None}


def build_report(data):
    mapping(data, "input")
    _reject_nonfinite(data)
    if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
        raise ValueError("schema_version must be 1")
    if data.get("evidence_kind") not in ("illustrative", "actual"):
        raise ValueError("evidence_kind must be illustrative or actual")
    if not isinstance(data.get("currency"), str) or not data["currency"].strip():
        raise ValueError("currency is required; all input amounts must share this currency")
    volume = number(data.get("monthly_requests"), "monthly_requests")
    months = number(data.get("horizon_months"), "horizon_months")
    if volume is None or months is None or int(months) != months or months > 1200:
        raise ValueError("volume is required; horizon_months must be an integer in 0..1200")
    raw = mapping(data.get("variants"), "variants")
    if set(raw) != set(VARIANTS):
        raise ValueError("variants must contain exactly teacher, base, fine_tuned")
    variants = {name: variant_cost(raw[name], volume) for name in VARIANTS}
    quality_ready = all(v["review_complete"] and v["quality_gate"] == "pass"
                        and v["reviewed_success_rate"] is not None for v in variants.values())
    reasons = []
    if data["evidence_kind"] == "illustrative":
        reasons.append("Illustrative values are not adoption evidence.")
    else:
        evidence_keys = ("usage_artifact", "quality_artifact", "price_url", "price_date",
                         "model_version", "tier", "region", "unit", "category_mix",
                         "hosting_hours", "conditions_hash")
        sources = [mapping(raw[name].get("source", {}), f"{name}.source") for name in VARIANTS]
        if any(source.get(key) is None or source.get(key) == ""
               for source in sources for key in evidence_keys):
            reasons.append("Actual-input provenance is incomplete; the label is not verified evidence.")
        hashes = [source.get("conditions_hash") for source in sources]
        if any(value is not None for value in hashes) and any(value != hashes[0] for value in hashes):
            reasons.append("Comparison conditions differ or are missing; do not pool these variants.")
    if not quality_ready:
        reasons.append("Comparable human-reviewed quality evidence is missing or the quality gate failed.")
    if any(v["monthly_cost"] is None or v["initial_total"] is None for v in variants.values()):
        reasons.append("Unknown costs must be resolved before a financial decision.")
    bridge = data.get("evaluation_bridge")
    if bridge is not None:
        mapping(bridge, "evaluation_bridge")
        reasons.extend(bridge.get("hold_reasons", []))
    reasons.append("Human decision required: adopt / conditional / reject / hold.")
    report = {"schema_version": 1, "evidence_kind": data["evidence_kind"],
            "currency": data["currency"], "monthly_requests": volume,
            "horizon_months": int(months), "scenario": data.get("scenario"),
            "variants": variants,
            "comparisons": {name: compare(variants["teacher"], variants[name])
                            for name in ("base", "fine_tuned")},
            "decision_draft": {"decision": "hold", "reasons": reasons}}
    if bridge is not None:
        report["evaluation_bridge"] = bridge
        if not bridge.get("comparable", False):
            report["comparisons"] = {
                name: {"status": "evaluation_evidence_not_comparable",
                       "crossover_requests": None, "monthly_savings": None,
                       "incremental_initial": None, "payback_months": None}
                for name in ("base", "fine_tuned")}
    return report


def series_rows(report, kind):
    if kind == "volume":
        maximum = max(report["monthly_requests"] * 2, 1000)
        crosses = [c["crossover_requests"] for c in report["comparisons"].values()
                   if c["crossover_requests"] is not None]
        maximum = max([maximum, *(c * 2 for c in crosses)])
        points = sorted(set([maximum * i / 20 for i in range(21)] + crosses))
    else:
        points = range(report["horizon_months"] + 1)
    rows = []
    for point in points:
        row = {"monthly_requests" if kind == "volume" else "month": point}
        for name, variant in report["variants"].items():
            if kind == "volume":
                value = (variant["per_request"] * point + variant["monthly_fixed"]
                         if variant["per_request"] is not None and variant["monthly_fixed"] is not None
                         else None)
            elif kind == "cumulative":
                value = variant["monthly_cost"] * point if variant["monthly_cost"] is not None else None
            else:
                value = (variant["initial_total"] + variant["monthly_cost"] * point
                         if variant["initial_total"] is not None and variant["monthly_cost"] is not None
                         else None)
            row[name] = value
        rows.append(row)
    return rows


def svg_chart(rows, xlabel, ylabel, title, evidence):
    xkey = next(iter(rows[0]))
    xmax = max([row[xkey] for row in rows] + [1])
    ymax = max([row[name] for row in rows for name in VARIANTS
                if row[name] is not None] + [1]) * 1.1
    esc = html.escape
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="880" height="480" viewBox="0 0 880 480" role="img">',
             f'<title>{esc(title)}</title>',
             f'<desc>{esc(evidence)}. teacher solid, base dotted, fine_tuned dashed. See accompanying CSV; blank values are unknown.</desc>',
             '<rect width="880" height="480" fill="white"/>',
             f'<text x="90" y="27" font-size="18">{esc(title)}</text>',
             f'<text x="90" y="49" font-size="13">{esc(evidence)}; unknown series omitted</text>']
    for i in range(6):
        y = 380 - i * 60
        x = 90 + i * 120
        parts.extend([f'<path d="M90 {y} H690" stroke="#ddd"/>',
                      f'<text x="82" y="{y + 4}" text-anchor="end" font-size="12">{ymax*i/5:.0f}</text>',
                      f'<text x="{x}" y="401" text-anchor="middle" font-size="12">{xmax*i/5:g}</text>'])
    parts.append('<path d="M90 80 V380 H690" fill="none" stroke="black"/>')
    for index, (name, color, dash) in enumerate((
            ("teacher", "#163b65", ""), ("base", "#a14b00", "2 5"), ("fine_tuned", "#146e4a", "10 5"))):
        coords = " ".join(f'{90+row[xkey]/xmax*600:.2f},{380-row[name]/ymax*300:.2f}'
                          for row in rows if row[name] is not None)
        if coords:
            parts.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="3" stroke-dasharray="{dash}"/>')
        y = 110 + index * 30
        parts.append(f'<path d="M710 {y} h30" stroke="{color}" stroke-width="3" stroke-dasharray="{dash}"/>')
        parts.append(f'<text x="746" y="{y+4}" font-size="12">{name}</text>')
    parts.extend([f'<text x="390" y="444" text-anchor="middle">{esc(xlabel)}</text>',
                  f'<text transform="translate(22 245) rotate(-90)" text-anchor="middle">{esc(ylabel)}</text>',
                  '</svg>'])
    return "\n".join(parts) + "\n"


def write_report(data, output):
    report = build_report(data)
    # Validate and render before reserving the output directory.
    charts = {}
    for kind in ("volume", "cumulative", "payback"):
        rows = series_rows(report, kind)
        title = ("Monthly operating cost by volume" if kind == "volume"
                 else f'Cumulative cost at {report["monthly_requests"]:g} requests/month'
                 + (" (includes initial)" if kind == "payback" else " (operating only)"))
        charts[kind] = (rows, svg_chart(
            rows, "Requests / month" if kind == "volume" else "Months",
            f'{report["currency"]} / month' if kind == "volume" else report["currency"],
            title, report["evidence_kind"].upper()
            + (" / EVALUATION COHORT ONLY; NOT PRODUCTION" if "evaluation_bridge" in report else "")))
    # Serialization catches numeric overflow before any output is created.
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if any(not math.isfinite(value) for rows, _ in charts.values() for row in rows
           for value in row.values() if value is not None):
        raise ValueError("Derived cost exceeds finite numeric range")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "input.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    (output / "report.json").write_text(serialized, encoding="utf-8", newline="\n")
    for kind, (rows, svg) in charts.items():
        (output / f"{kind}.svg").write_text(svg, encoding="utf-8", newline="\n")
        with (output / f"{kind}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    with (output / "costs.csv").open("w", encoding="utf-8", newline="") as stream:
        keys = ("per_request", "monthly_fixed", "initial_total", "monthly_cost",
                "reviewed_success_rate", "projected_cost_per_reviewed_success")
        writer = csv.DictWriter(stream, fieldnames=["variant", *keys], lineterminator="\n")
        writer.writeheader()
        writer.writerows({"variant": name, **{k: v[k] for k in keys}}
                         for name, v in report["variants"].items())
    reasons = "\n".join(f"- {reason}" for reason in report["decision_draft"]["reasons"])
    (output / "decision.md").write_text(
        f"# 判断ドラフト: hold（保留）\n\n証拠区分: {report['evidence_kind']}\n\n{reasons}\n\n"
        "## 人間が記入する判断\n\n"
        "- 採用 / 条件付き採用 / 見送り / 保留: 未記入\n"
        "- 品質基準、許容差、ケース数、反復、比較対象・期間: 未記入\n"
        "- 独立した最終holdoutの結果とレビュー証跡: 未記入\n"
        "- 失敗・未試行・基盤障害、usage欠落、信頼区間の限界: 未記入\n"
        "- 価格根拠、初期費用の配賦、月間カテゴリ比の根拠: 未記入\n"
        "- 条件・停止基準・再評価日・責任者: 未記入\n\n"
        "費用が安いだけでは採用しない。自動採点は人間確認済み成功ではない。\n",
        encoding="utf-8", newline="\n")
    return report
