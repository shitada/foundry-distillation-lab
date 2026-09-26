"""Independent, policy-authored supplemental development cases (not a final holdout).

No retail functions execute while preparing expectations. Domain values below are
goldens, not teacher traces or values accepted from the implementation under test.
Only opaque authority IDs are computed, using the contract's canonical hash format.
"""

from copy import deepcopy
import hashlib
from pathlib import Path
import re

from ..datasets.prepare import normalize
from ..io import canonical, read_json, read_jsonl, sha256, write_json
from ..retail import RetailSession
from .scoring import DEFAULT_MODELS, evaluate


PERIOD = 1320  # lcm(8 products/statuses, 3 tiers/variants, 5 sealed, 11 sale, 55 age)
CANDIDATES_PER_SCENARIO = 4
DATA_FILES = ("normalized.jsonl", "train.jsonl", "validation.jsonl",
              "development.jsonl", "final.jsonl")
IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])(?:ORD-(\d{3,6})(?!\d)|ITEM-(\d{3,6})-\d+(?!\d))")


def _scenario(key, base, tier, sku, product, category, price, status, days,
              window, request, reference, *, reason=None, action=None,
              eligible=None, fee_rate=0.0, refund=0, fee=0, credit=0,
              difference=0, explanation=None, extra=None, final_sale=False):
    return dict(key=key, base=base, tier=tier, sku=sku, product=product,
                product_category=category, price=price, status=status, days=days,
                window=window, request=request, reference=reference, reason=reason,
                action=action, eligible=eligible, fee_rate=fee_rate, refund=refund,
                fee=fee, credit=credit, difference=difference, explanation=explanation,
                extra=extra, final_sale=final_sale)


# Prices, tiers, ages, windows, eligibility and amounts are explicit reviewable
# business goldens. Changing store behavior must fail tests, not rewrite these.
SCENARIOS = (
    _scenario("shipping", 1327, "ゴールド", "SKU-008-2", "保湿クリーム", "パーソナルケア",
              3400, "発送済み", None, 30,
              "{order}の配送状況だけを教えてください。変更手続きは不要です。",
              "注文は発送済みです。配達済みではありません。変更処理は行っていません。"),
    _scenario("refund_standard", 1488, "標準", "SKU-001-1", "ワイヤレスヘッドホン", "電子機器",
              12900, "配達済み", 8, 15,
              "{order}の{item}だけ、お客様都合で返品・返金を希望します。他の商品は変更しないでください。シミュレーションを確定してください。",
              "返品期限15日以内です。12,900円から15%の手数料1,935円を差し引き、10,965円の返金をシミュレーションしました。実処理ではありません。",
              reason="お客様都合", action="返金", eligible=["返金"], fee_rate=0.15,
              refund=10965, fee=1935, explanation="返品期限と商品条件を満たしています。",
              extra=("SKU-004-2", 25000)),
    _scenario("refund_gold", 1336, "ゴールド", "SKU-001-2", "ワイヤレスヘッドホン", "電子機器",
              13000, "配達済み", 21, 30,
              "{order}の{item}をお客様都合で返品・返金し、シミュレーションを確定してください。",
              "ゴールド会員の返品期限30日以内です。13,000円から7.5%の手数料975円を差し引いた返金12,025円をシミュレーションしました。実処理ではありません。",
              reason="お客様都合", action="返金", eligible=["返金"], fee_rate=0.075,
              refund=12025, fee=975, explanation="返品期限と商品条件を満たしています。"),
    _scenario("refund_platinum", 1322, "プラチナ", "SKU-003-3", "電気ケトル", "電子機器",
              6800, "配達済み", 7, 45,
              "{order}の{item}をお客様都合で返品・返金し、シミュレーションを確定してください。",
              "プラチナ会員の返品期限45日以内です。手数料は0円、返金6,800円をシミュレーションしました。実処理ではありません。",
              reason="お客様都合", action="返金", eligible=["返金"],
              refund=6800, explanation="返品期限と商品条件を満たしています。"),
    _scenario("exchange", 1321, "ゴールド", "SKU-002-2", "ランニングシューズ", "衣料品",
              9100, "配達済み", 6, 45,
              "{order}の{item}はサイズ違いです。同じ商品のSKU-002-3へ交換希望です。差額があってもシミュレーションを確定してください。",
              "在庫を確認し、同じ商品のSKU-002-3への交換をシミュレーションしました。差額は追加100円です。返金・手数料・クレジットは0円で、実処理ではありません。",
              reason="サイズ違い", action="交換", eligible=["交換", "返金"],
              difference=100, explanation="返品期限と商品条件を満たしています。"),
    _scenario("cancellation", 1325, "プラチナ", "SKU-006-3", "ヨガマット", "生活雑貨",
              4500, "受付済み", None, 60,
              "{order}の{item}をお客様都合でキャンセル希望です。シミュレーションを確定してください。",
              "受付済みで発送前のためキャンセル可能です。全額4,500円の返金をシミュレーションしました。手数料0円で、実処理ではありません。",
              reason="お客様都合", action="キャンセル", eligible=["キャンセル"],
              refund=4500, explanation="発送前のためキャンセルできます。"),
    _scenario("expired_return", 1344, "標準", "SKU-001-1", "ワイヤレスヘッドホン", "電子機器",
              12900, "配達済み", 29, 15,
              "{order}の{item}だけ、お客様都合で返品・返金できますか。対象外なら変更せず理由を教えてください。",
              "標準会員の電子機器の返品期限15日に対し配達後29日です。期限切れのため対象外で、返金や確定処理は行っていません。",
              reason="お客様都合", eligible=["対象外"],
              explanation="返品期限、開封状態、または最終処分品の条件を満たしません。",
              extra=("SKU-004-2", 25000)),
    _scenario("lost_delivery", 1323, "標準", "SKU-004-1", "オフィスチェア", "生活雑貨",
              24900, "紛失", None, 30,
              "{order}の{item}だけ未着です。紛失なら代替品ではなく返金を希望します。他の商品は変更せず、シミュレーションを確定してください。",
              "配送中の紛失が確認されました。対象商品の全額24,900円を手数料なしで返金するシミュレーションです。他の商品は変更せず、実処理ではありません。",
              reason="未着", action="返金", eligible=["代替品発送", "返金"],
              refund=24900, explanation="配送中の紛失として代替品または返金を選べます。",
              extra=("SKU-007-2", 30000)),
    _scenario("late_credit", 1324, "ゴールド", "SKU-005-2", "ウールセーター", "衣料品",
              10000, "配達済み", 9, 60,
              "{order}の{item}が遅配でした。返品ではなく配送遅延クレジットだけを希望します。シミュレーションを確定してください。",
              "4日間の遅配により1,000円の配送遅延クレジットをシミュレーションしました。返金と手数料は0円、返品期限は15日延長され60日です。実処理ではありません。",
              reason="遅配", action="配送遅延クレジット", eligible=["配送遅延クレジット"],
              credit=1000, explanation="約束日より3日以上遅れたためクレジット対象です。"),
    _scenario("damaged_final_sale", 1408, "ゴールド", "SKU-001-2", "ワイヤレスヘッドホン", "電子機器",
              13000, "配達済み", 38, 30,
              "{order}の{item}が破損しています。最終処分品でも対応できるストアクレジットを希望します。シミュレーションを確定してください。",
              "破損した最終処分品は期限の例外としてストアクレジット対象です。13,000円のクレジット、返金・手数料0円をシミュレーションしました。実処理ではありません。",
              reason="破損", action="ストアクレジット", eligible=["ストアクレジット"],
              credit=13000, explanation="破損・不良品は期限と手数料の例外対象です。",
              final_sale=True),
)


def _reference_id(prefix, value):
    digest = hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:12].upper()
    return f"{prefix}-{digest}"


def _call(name, arguments, result):
    return {"name": name, "arguments": arguments, "result": result, "match": "subset"}


def _case(template, number, session):
    s = template
    order, item = f"ORD-{number:04d}", f"ITEM-{number:04d}-1"
    item_values = [{"item_id": item, "sku": s["sku"], "unit_price_jpy": s["price"],
                    "category": s["product_category"], "quantity": 1,
                    "final_sale": s["final_sale"], "sealed": True}]
    if s["extra"]:
        item_values.append({"item_id": f"ITEM-{number:04d}-2", "sku": s["extra"][0],
                            "unit_price_jpy": s["extra"][1], "quantity": 1})
    calls = [
        _call("get_order_details", {"order_id": order},
              {"order_id": order, "customer": {"loyalty_tier": s["tier"]},
               "items": item_values, "currency": "JPY"}),
        _call("get_fulfillment_status", {"order_id": order},
              {"order_id": order, "status": s["status"], "days_since_delivery": s["days"],
               "late_delivery": s["key"] == "late_credit",
               "days_late": 4 if s["key"] == "late_credit" else 0}),
    ]
    expected = {"initial_tools": ["get_order_details", "get_fulfillment_status"],
                "required_calls": calls, "allowed_mutations": [],
                "final_state": {"terminal": "answer_only", "submissions": []},
                "final_state_match": "subset"}
    if s["reason"]:
        policy_args = {"order_id": order, "item_id": item, "reason": s["reason"]}
        policy = {**policy_args, "eligible": s["eligible"] != ["対象外"],
                  "eligible_actions": s["eligible"], "restocking_fee_rate": s["fee_rate"],
                  "explanation": s["explanation"]}
        policy["policy_id"] = _reference_id("POLICY", policy)
        calls.append(_call("check_resolution_policy", policy_args, policy))
    if s["action"]:
        request = {"item_id": item, "actions": [s["action"]], "reason": s["reason"],
                   "policy_id": policy["policy_id"]}
        if s["action"] == "交換":
            inventory = {"sku": "SKU-002-3", "product_name": "ランニングシューズ",
                         "unit_price_jpy": 9200, "available": True, "quantity": 2,
                         "message": "交換可能です。"}
            inventory["inventory_check_id"] = _reference_id("INVENTORY", inventory)
            calls.append(_call("check_inventory", {"sku": "SKU-002-3"}, inventory))
            request.update(replacement_sku="SKU-002-3",
                           inventory_check_id=inventory["inventory_check_id"])
        calculation = {
            "order_id": order, "currency": "JPY",
            "items": [{"item_id": item, "product_name": s["product"], "actions": [s["action"]],
                       "reason": s["reason"], "replacement_sku": request.get("replacement_sku"),
                       "refund_jpy": s["refund"], "restocking_fee_jpy": s["fee"],
                       "credit_jpy": s["credit"], "exchange_difference_jpy": s["difference"]}],
            "total_refund_jpy": s["refund"], "total_restocking_fee_jpy": s["fee"],
            "total_credit_jpy": s["credit"], "total_exchange_difference_jpy": s["difference"],
            "message": "解決内容の計算が完了しました。",
        }
        calculation["calculation_id"] = _reference_id("CALC", calculation)
        calls.append(_call("calculate_resolution", {"order_id": order, "items": [request]}, calculation))
        authority = {"order_id": order, "calculation_id": calculation["calculation_id"]}
        submitted = {**authority, "status": "処理シミュレーション完了", "external_side_effect": False}
        mutation = _call("submit_resolution", authority, submitted)
        calls.append(mutation)
        expected["allowed_mutations"] = [deepcopy(mutation)]
        expected["final_state"] = {"terminal": "simulation_submitted", "submissions": [submitted]}
    return {
        "case_id": f"supplemental-{s['key']}", "category": s["key"],
        "user_input": s["request"].format(order=order, item=item),
        "system_prompt": session.system_prompt,
        "reference": {"role": "assistant", "content": s["reference"]},
        "context": {"benchmark_split": "development", "source": "independent_policy_golden_v1",
                    "order_id": order, "item_id": item, "return_window_days": s["window"],
                    "business_scenario": s["reference"]},
        "expected": expected,
    }


def _sources(data_dir):
    """Fail closed on incomplete/held/tampered chapter data; scan every split."""
    directory = Path(data_dir)
    manifest = read_json(directory / "manifest.json")
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
            or not isinstance(manifest.get("input_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["input_sha256"])):
        raise ValueError("Invalid previous chapter manifest")
    hashes = manifest.get("output_hashes")
    if not isinstance(hashes, dict) or not set(DATA_FILES).issubset(hashes):
        raise ValueError("Manifest must cover normalized/train/validation/development/final JSONL")
    audit = read_json(directory / "audit.json")
    if (not isinstance(audit, dict) or audit.get("input_sha256") != manifest.get("input_sha256")
            or not isinstance(audit.get("rows"), list) or not audit["rows"]
            or any(not isinstance(row, dict) or row.get("status") != "candidate" for row in audit["rows"])):
        raise ValueError("Previous chapter audit contains skipped/held/invalid rows")
    rows_by_file = {}
    sources = {"manifest.json": sha256(directory / "manifest.json"),
               "audit.json": sha256(directory / "audit.json")}
    for name, digest in sorted(hashes.items()):
        if (not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name
                or not name.endswith(".jsonl") or not isinstance(digest, str)):
            raise ValueError("Invalid manifest output path/hash")
        path = directory / name
        if sha256(path) != digest:
            raise ValueError(f"Previous chapter file hash mismatch: {name}")
        rows = read_jsonl(path)
        if not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Empty or invalid previous chapter rows: {name}")
        rows_by_file[name] = rows
        sources[name] = digest
    normalized = rows_by_file["normalized.jsonl"]
    by_id = {}
    for row in normalized:
        validated, _ = normalize(row)
        key = validated["conversation_id"]
        if key in by_id or canonical(validated) != canonical(row):
            raise ValueError("Invalid or duplicate normalized conversation")
        by_id[key] = row
    if len(audit["rows"]) != len(normalized):
        raise ValueError("Previous chapter audit/normalized row count mismatch")
    partitions = manifest.get("partitions")
    names = {"train", "validation", "development", "final"}
    if not isinstance(partitions, dict) or set(partitions) != names:
        raise ValueError("Previous chapter four partitions required")
    seen = set()
    for name in sorted(names):
        entries, rows = partitions[name], rows_by_file[f"{name}.jsonl"]
        if not isinstance(entries, list) or len(entries) != len(rows):
            raise ValueError(f"Manifest partition size mismatch: {name}")
        for entry, row in zip(entries, rows):
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid manifest partition entry: {name}")
            key = entry.get("conversation_id")
            if not isinstance(key, str) or key in seen or key not in by_id:
                raise ValueError("Missing/duplicate partition conversation")
            source = by_id[key]
            if (entry.get("category") != source["category"]
                    or canonical(row) != canonical({k: source[k] for k in ("messages", "tools")})):
                raise ValueError(f"Partition differs from normalized source: {name}")
            seen.add(key)
    if seen != set(by_id):
        raise ValueError("Partitions do not cover normalized source")
    excluded = {}
    for name, rows in rows_by_file.items():
        for row in rows:
            for match in IDENTIFIER.finditer(canonical(row)):
                number = int(match.group(1) or match.group(2))
                excluded.setdefault(number, set()).add(name)
    return sources, excluded


def prepare_cases(data_dir, output):
    """Write a new, unexecuted 11-case supplemental development bundle offline.

    Requires the complete successful ``prepare_data.py`` output directory. Order
    aliases and ITEM references are normalized numerically across all source rows,
    including the untouched final split (used only for exclusion, never an oracle).
    Four fixed periodic candidates per scenario are tried in order, never chosen
    using model results. Identical existing bundles are reused without writes;
    differing outputs and exhausted scenarios fail closed.
    """
    output = Path(output)
    sources, excluded = _sources(data_dir)
    session = RetailSession()
    cases, choices, chosen = [], [], set()
    for scenario in SCENARIOS:
        candidates = [scenario["base"] + PERIOD * offset for offset in range(CANDIDATES_PER_SCENARIO)]
        number = next((n for n in candidates if n not in excluded and n not in chosen), None)
        if number is None:
            raise ValueError(f"Curated candidates exhausted for {scenario['key']}: {candidates}")
        chosen.add(number)
        cases.append(_case(scenario, number, session))
        choices.append({"category": scenario["key"], "candidate_order_numbers": candidates,
                        "selected_order_number": number, "group": f"order:{number}",
                        "excluded_candidates": [
                            {"order_number": n, "sources": sorted(excluded[n])}
                            for n in candidates if n in excluded]})
    cases.append({
        "case_id": "supplemental-missing_order", "category": "missing_order",
        "user_input": "返品したいのですが、注文番号がわかりません。何をお伝えすればよいですか。",
        "system_prompt": session.system_prompt,
        "reference": {"role": "assistant", "content": "注文番号、対象商品、返品理由を教えてください。注文を推測したり、返金処理を行ったりはしていません。"},
        "context": {"benchmark_split": "development", "source": "independent_policy_golden_v1",
                    "business_scenario": "注文番号不足の確認質問。注文を捏造せず変更しない。"},
        "expected": {"initial_tools": [], "required_calls": [], "allowed_mutations": [],
                     "final_state": {"terminal": "answer_only", "submissions": []}},
    })
    bundle = {
        "schema": "retail-evaluation-input-v1", "models": list(DEFAULT_MODELS),
        "tools": session.tools, "cases": cases, "records": [],
        "benchmark_split": "development",
        "provenance": {
            "kind": "supplemental_curated_policy_benchmark", "version": 1,
            "data_directory": str(Path(data_dir).resolve()), "source_hashes": sources,
            "selection": choices,
            "excluded_order_groups": [
                {"group": f"order:{n}", "order_number": n, "sources": sorted(files)}
                for n, files in sorted(excluded.items())],
            "oracle": "Independent hardcoded policy/domain goldens; structural hashes only; no teacher or store execution.",
            "limitations": [
                "Supplemental development benchmark, NOT an untouched final holdout.",
                "Order separation does not establish independence from shared synthetic business templates.",
                "Eleven educational scenarios are not production coverage or proof of business success.",
                "Freeform Japanese answers and missing-information behavior require quality review.",
            ],
        },
    }
    evaluate(bundle, "e2e")
    if output.exists():
        try:
            existing = read_json(output)
        except (OSError, ValueError) as error:
            raise FileExistsError(
                f"Existing output cannot be reused: {output}; choose a new output path") from error
        if canonical(existing) != canonical(bundle):
            raise FileExistsError(
                f"Existing output differs from prepared bundle: {output}; choose a new output path")
        return bundle
    write_json(output, bundle)
    return bundle
