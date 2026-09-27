"""Public-retail estimates from linked experiment evidence, never billing queries."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from ..evaluation.scoring import normalized_usage
from ..evaluation.study import _allocate_directory
from ..evaluation.workflow import combine_runs
from ..io import read_json, sha256, write_json
from .bridge import prepare_cost_input
from .costs import VARIANTS, number, total, write_report
from .evidence import discover_evidence
from .graphs import adaptive_graph_plan, write_graph_summary
from .pricing import AzurePricing


HOURS_PER_MONTH = 24 * 365 / 12
SHORT_CONTEXT_LIMIT = 272_000
RATE_KEYS = ("input_per_million", "cached_input_per_million", "output_per_million",
             "hosting_per_hour", "training_per_million")


def plan_config(value):
    defaults = {"additional_initial_cost": 0, "additional_monthly_cost": 0,
                "hosting_hours_per_day": 24}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("Cost plan accepts only additional_initial_cost, "
                         "additional_monthly_cost, hosting_hours_per_day")
    plan = {**defaults, **value}
    for key, amount in plan.items():
        if number(amount, key) is None:
            raise ValueError(f"{key}: enter a known nonnegative number")
    if plan["hosting_hours_per_day"] > 24:
        raise ValueError("hosting_hours_per_day must be in 0..24")
    return plan


def inference_cost(usage, rates, *, averaged=False):
    usage = ({key: number(usage.get(key), key)
              for key in ("input_tokens", "output_tokens", "cached_input_tokens")}
             if averaged else normalized_usage(usage))
    inp, out, cached = (usage[key] for key in ("input_tokens", "output_tokens", "cached_input_tokens"))
    if None in (inp, out, cached):
        return None
    components = []
    for quantity, key in ((inp - cached, "input_per_million"),
                          (cached, "cached_input_per_million"), (out, "output_per_million")):
        rate = number(rates.get(key), key)
        components.append(0 if quantity == 0 else None if rate is None else quantity * rate / 1_000_000)
    return total(components)


def _hours_since(value, as_of):
    if not isinstance(value, str):
        return None
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None or created > as_of:
        return None
    return (as_of - created).total_seconds() / 3600


def _context_tier(usage):
    tokens = normalized_usage(usage)["input_tokens"]
    return None if tokens is None else "long" if tokens > SHORT_CONTEXT_LIMIT else "short"


def _selected_context_tier(directory):
    evidence = read_json(directory / "evidence.json")
    tiers = []
    for record in evidence["records"]:
        events = record.get("events")
        if not isinstance(events, list):
            return None
        tiers.extend(_context_tier(event.get("usage")) for event in events
                     if event.get("event") == "model_finish")
    return tiers[0] if tiers and None not in tiers and len(set(tiers)) == 1 else None


def _matches_observed_model(identity, observed):
    if not isinstance(observed, str) or not observed:
        return True
    model, version = identity.get("model"), identity.get("version")
    return observed == model or (isinstance(model, str) and isinstance(version, str)
                                 and observed == f"{model}-{version}")


def estimate(config_path=None, runs_dir="runs", output_dir="runs/cost-report", *,
             pricing=None, discover=None, as_of=None):
    plan = plan_config({} if config_path is None else read_json(config_path))
    projected_hours = plan["hosting_hours_per_day"] * (365 / 12)
    inventory = (discover or discover_evidence)(runs_dir)
    if not inventory.get("study_dir"):
        raise ValueError("No Chapter 7 study found in runs; complete Chapter 7 first")
    selected = Path(inventory["study_dir"])
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError("Estimate date must include a timezone")
    directory = _allocate_directory(output_dir)
    diagnostics = deepcopy(inventory.get("diagnostics", []))
    metadata = {
        "schema": "public-retail-estimate-v1", "currency": "USD", "as_of": as_of.isoformat(),
        "selected_study": str(selected.resolve()),
        "plan_sha256": None if config_path is None else sha256(config_path),
        "plan_source": "defaults" if config_path is None else str(config_path),
        "hours_per_day": plan["hosting_hours_per_day"], "hours_per_month": projected_hours,
        "month_basis": "365 days / 12 months; constant average month",
        "pricing_basis": "current public retail prices applied to recorded usage; not an invoice",
        "allocation": "Linked evaluation and grading costs split equally across three alternatives; "
                      "teacher data generation and training assigned to fine_tuned.",
        "scope": "Cloud model costs for the local-tool-loop lab; optional additional USD costs",
        "additional_cost_allocation": "Incremental fine_tuned adoption expense only; "
                                      "not added to teacher or base",
        "additional_cost_interpretation": "Default zero means additional expenses, including labor, "
                                          "are not counted; it does not establish that they are free",
        "additional_initial_cost": plan["additional_initial_cost"],
        "additional_monthly_cost": plan["additional_monthly_cost"],
        "hosting_schedule_scope": "Future projection only; does not change deployments or "
                                  "historical elapsed experiment-hosting costs",
        "actual_invoice_cost": None,
    }
    write_json(directory / "plan.json", plan)
    write_json(directory / "inventory.json", inventory)
    write_json(directory / "estimate-start.json", metadata)
    try:
        combined = combine_runs([selected / f"{name}-graded" for name in VARIANTS], directory / "combined")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        write_json(directory / "estimate-error.json", {"error": str(exc), **metadata})
        raise ValueError(f"Selected Chapter 7 results cannot be combined: {exc}; evidence: {directory}") from exc
    provider = pricing or AzurePricing()
    price_records, identities, rates_by_target = [], {}, {}

    def missing(source, reason):
        item = {"source": str(source), "reason": reason}
        if item not in diagnostics:
            diagnostics.append(item)

    def get_rates(endpoint, target, context_tier=None):
        key = (endpoint, target, context_tier)
        if key in rates_by_target:
            return identities[key], rates_by_target[key]
        identity, rates = {}, {name: None for name in RATE_KEYS}
        try:
            identity = provider.deployment(endpoint, target)
            if isinstance(identity.get("model"), str) and identity["model"].startswith("gpt-5.5"):
                identity = {**identity, "context_tier": context_tier}
            rates = provider.rates(identity)
            for reason in rates.get("missing", []):
                missing(target, str(reason))
        except (OSError, ValueError, RuntimeError) as exc:
            missing(target, f"Model metadata or public prices unavailable: {exc}")
        identities[key], rates_by_target[key] = identity, rates
        price_records.append({"endpoint": endpoint, "target": target, "identity": identity, "rates": rates})
        return identity, rates

    selected_config = read_json(selected / "evaluation-config.json")
    variants, selected_prices = {}, {}
    for name in VARIANTS:
        target = selected_config["targets"][name]
        identity, rates = get_rates(selected_config["base_url"], target,
                                    _selected_context_tier(selected / f"{name}-graded"))
        records = read_json(selected / f"{name}-graded" / "evidence.json")["records"]
        observed_models = [event.get("response_model") for record in records
                           for event in record.get("events", []) if event.get("event") == "model_finish"]
        if identity and any(not _matches_observed_model(identity, observed) for observed in observed_models):
            rates = {**rates, **{key: None for key in RATE_KEYS[:3]}}
            missing(target, "Current deployment model differs from recorded responses; historical price binding unknown")
        selected_prices[name] = identity, rates
        hourly = number(rates.get("hosting_per_hour"), "hosting_per_hour")
        variants[name] = {
            "inference_mode": "tokens",
            "prices": {key: rates.get(key) for key in RATE_KEYS[:3]},
            "usage_per_request": {"input_tokens": None, "output_tokens": None, "cached_input_tokens": None},
            "variable_fees_per_request": {"agent": 0, "tools_logs_storage": 0},
            "monthly_fixed": {"model_hosting": None if hourly is None else hourly * projected_hours,
                              "agent": 0, "tools_logs_storage":
                              plan["additional_monthly_cost"] if name == "fine_tuned" else 0},
            "initial": {"generation": 0, "training": 0, "evaluation": None,
                        "other": plan["additional_initial_cost"] if name == "fine_tuned" else 0},
            "quality": {"review_complete": False, "reviewed_requests": None,
                        "successful_requests": None, "quality_gate": "unreviewed"},
            "source": {
                "price_url": "https://prices.azure.com/api/retail/prices",
                "price_date": as_of.date().isoformat(), "model_version": identity.get("version"),
                "tier": identity.get("sku"), "region": identity.get("region"),
                "hosting_hours": projected_hours, "unit": "USD per million tokens",
                "category_mix": None, "conditions_hash": sha256(selected / "input.json"),
                "cost_basis": "public_retail_estimate_not_invoice",
            },
        }
        if hourly is None:
            missing(target, "Hourly hosting price is unknown")

    ledger, generation, evaluation = [], [], []
    for call in inventory.get("calls", []):
        identity, rates = get_rates(call.get("endpoint"), call.get("target"), _context_tier(call.get("usage")))
        if (identity and call.get("response_model") != call.get("target")
                and not _matches_observed_model(identity, call.get("response_model"))):
            amount = None
            missing(call["source"], "Current deployment model differs from this recorded call")
        else:
            amount = inference_cost(call.get("usage"), rates)
        if amount is None:
            missing(call["source"], "Call usage or corresponding public unit price is incomplete")
        if call["kind"] == "generation":
            generation.append(amount)
        elif call["kind"] in ("evaluation", "grading"):
            evaluation.append(amount)
        else:
            raise ValueError(f"Unsupported cost evidence kind: {call['kind']}")
        ledger.append({**deepcopy(call), "estimated_usd": amount,
                       "allocation": "fine_tuned" if call["kind"] == "generation" else "equal_three_way",
                       "price_identity": identity})
    if inventory.get("generation", {}).get("known"):
        generation_total = total(generation)
    else:
        generation_total = None
        missing(inventory.get("data_dir", selected), "Teacher-generation usage is not fully recorded")
    variants["fine_tuned"]["initial"]["generation"] = generation_total
    if not evaluation:
        missing(selected, "Evaluation call records are missing")
    common_evaluation = (total(evaluation) if evaluation and inventory.get("evaluation_complete", True)
                         else None)
    if not inventory.get("evaluation_complete", True):
        missing(selected, "Some linked evaluation or grading evidence could not be read")
    for variant in variants.values():
        variant["initial"]["evaluation"] = None if common_evaluation is None else common_evaluation / 3

    training_amounts = []
    for job in inventory.get("training", []):
        amount = None
        rates, account = {}, {}
        tokens = job.get("trained_tokens")
        try:
            account = provider.account(job["project_endpoint"])
            identity = {**account, "model": job["model"], "sku": job["training_type"]}
            rates = provider.rates(identity, training_type=job["training_type"])
            rate = number(rates.get("training_per_million"), "training_per_million")
            if (type(tokens) is int and tokens >= 0 and rate is not None
                    and job.get("status") in {"succeeded", "failed", "cancelled"}):
                amount = tokens * rate / 1_000_000
        except (OSError, ValueError, RuntimeError) as exc:
            missing(job["path"], f"Training metadata or price unavailable: {exc}")
        if amount is None:
            missing(job["path"], "Final training usage, terminal job state, or matching token-based rate unavailable")
        training_amounts.append(amount)
        price_records.append({"training": job["path"], "account": account, "rates": rates})
        ledger.append({"kind": "training", "source": job["path"], "job_id": job.get("job_id"),
                       "trained_tokens": tokens, "estimated_usd": amount, "allocation": "fine_tuned"})
    variants["fine_tuned"]["initial"]["training"] = (
        total(training_amounts) if training_amounts and inventory.get("training_complete", True) else None)
    if not inventory.get("training_complete", True):
        missing(selected, "Some linked training evidence could not be read")
    if not training_amounts:
        missing(selected, "No linked training job record found")

    # Existing deployments accrue cost even between measured model requests.
    for name in VARIANTS:
        target = selected_config["targets"][name]
        identity, rates = selected_prices[name]
        hourly = rates.get("hosting_per_hour")
        elapsed = _hours_since(identity.get("created_at"), as_of)
        hosting = 0 if hourly == 0 else hourly * elapsed if hourly is not None and elapsed is not None else None
        if hosting is None:
            missing(target, "Experiment deployment lifetime or hosting price unknown")
        variants[name]["initial"]["evaluation"] = total([variants[name]["initial"]["evaluation"], hosting])
        ledger.append({"kind": "experiment_hosting", "source": identity.get("resource_id", target),
                       "allocation": name, "hours": elapsed, "estimated_usd": hosting,
                       "basis": "Current deployment creation to estimate time; previous deleted deployments excluded"})
    metadata["prior_deleted_deployments"] = "Not reconstructed; only retained deployment evidence can be priced"
    cost_input = {
        "schema_version": 1, "evidence_kind": "actual",
        "scenario": "Public retail estimate for the local retail experiment, not invoiced cost",
        "currency": "USD", "monthly_requests": 1000,
        "horizon_months": 12, "variants": variants,
        "evaluation_projection": {"weighting": "uniform_scheduled_slots",
                                  "expected_runtime": "local_direct_model",
                                  "production_category_mix": None, "production_equivalence_reviewed": False},
    }
    cost_input = prepare_cost_input(combined, cost_input, sha256(directory / "combined" / "scores.json"))
    for name, variant in cost_input["variants"].items():
        if inference_cost(variant["usage_per_request"], variant["prices"], averaged=True) is None:
            missing(name, "Projected customer-request cost has unknown usage or price")
    metadata["diagnostics"] = diagnostics
    metadata["complete"] = not diagnostics and all(
        value is not None for variant in variants.values()
        for value in (*variant["initial"].values(), *variant["monthly_fixed"].values()))
    cost_input["estimate"] = metadata
    graph_plan = adaptive_graph_plan(cost_input)
    middle = next(item for item in graph_plan["scenarios"] if item["id"] == "middle")
    cost_input["monthly_requests"] = middle["monthly_requests"]
    cost_input["horizon_months"] = middle["horizon_months"]
    metadata["summary_projection"] = {
        "basis": "Automatically selected middle scenario; assumption, not actual user usage",
        "monthly_requests": middle["monthly_requests"], "horizon_months": middle["horizon_months"],
    }
    write_json(directory / "prices.json", {"as_of": as_of.isoformat(), "currency": "USD", "items": price_records})
    write_json(directory / "cost-ledger.json", {"items": ledger, "allocation": metadata["allocation"]})
    report = write_report(cost_input, directory / "tables", graph_plan=graph_plan)
    for path in (directory / "tables").iterdir():
        path.rename(directory / path.name)
    (directory / "tables").rmdir()
    write_graph_summary(report, directory)
    write_json(directory / "estimate.json", metadata)
    notes = [
        "# 概算費用の計算結果", "",
        "公開単価と保存済み使用量による概算です。請求額・契約割引・税は取得していません。",
        f"通貨はUSD。将来の配置費は毎日{plan['hosting_hours_per_day']:g}時間、"
        f"平均1か月{projected_hours:g}時間（365日 / 12か月）で計算しています。",
        "配置の起動・停止は行いません。実験中の配置費は実際の経過時間を使います。",
        "最終結果は [比較グラフ](graphs.md) を参照してください。月間件数と表示期間は自動選択した仮定です。",
        f"対象の第7章の結果: `{selected}`", "",
        "実験の評価・採点費は三つの構成へ均等に配分し、教師データ生成費と学習費は学習済み構成へ計上します。",
        "任意の追加初期費用・月額費用（USD）は学習済み構成を採用する増分費用としてのみ計上します。",
        "既定値0は人件費などの追加費用を未計上という意味で、無料であることを示しません。", "",
        "## 不足している情報", "",
    ]
    notes.extend(f"- {item['source']}: {item['reason']}" for item in diagnostics)
    if not diagnostics:
        notes.append("取得対象の費用情報は揃っています。品質に関する採用判断は別に行います。")
    (directory / "estimate.md").write_text("\n".join(notes) + "\n", encoding="utf-8")
    return {"output_dir": str(directory), "complete": metadata["complete"], "report": report}
