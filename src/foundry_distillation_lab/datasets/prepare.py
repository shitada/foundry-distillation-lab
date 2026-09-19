from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import random
import re

from ..io import canonical, parse_json, read_jsonl, sha256, write_json, write_jsonl
from ..retail import RetailSession, validate_value

ORDER = re.compile(r"\bORD-\d{3,6}\b")
PRIVATE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?i:Bearer\s+\S+|api[_-]?key\s*[:=])")


def normalize(row):
    if not isinstance(row, dict):
        raise ValueError("row_not_object")
    session = RetailSession()
    if not isinstance(row.get("conversation_id"), str) or not row["conversation_id"]:
        raise ValueError("missing_conversation_id")
    if not isinstance(row.get("category"), str) or not row["category"]:
        raise ValueError("missing_category")
    if not isinstance(row.get("messages"), list) or not row["messages"]:
        raise ValueError("missing_messages")
    if "tools" in row and row["tools"] != session.tools:
        raise ValueError("tool_contract_mismatch")
    messages = deepcopy(row["messages"])
    changes = []
    if not isinstance(messages[0], dict):
        raise ValueError("invalid_first_message")
    if messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": session.system_prompt})
        changes.append("added_frozen_system_prompt")
    if messages[0] != {"role": "system", "content": session.system_prompt}:
        raise ValueError("system_contract_mismatch")
    if len(messages) < 3:
        raise ValueError("incomplete_conversation")
    definitions = {t["function"]["name"]: t["function"]["parameters"] for t in session.tools}
    pending, seen = set(), set()
    last_role = "system"
    call_count = user_count = 0
    for index, message in enumerate(messages[1:], 1):
        if not isinstance(message, dict):
            raise ValueError("invalid_message")
        role = message.get("role")
        if set(message) - {"role", "content", "tool_calls", "tool_call_id"}:
            raise ValueError("unsupported_message_fields")
        if role not in ("user", "assistant", "tool"):
            raise ValueError("invalid_role")
        if pending and role != "tool":
            raise ValueError("tool_results_not_immediate")
        if role == "tool":
            if set(message) != {"role", "content", "tool_call_id"}:
                raise ValueError("invalid_tool_result_shape")
            if message.get("tool_call_id") not in pending:
                raise ValueError("orphan_or_duplicate_tool_result")
            if not isinstance(message.get("content"), str):
                raise ValueError("tool_result_not_text")
            result = parse_json(message["content"])
            if not isinstance(result, dict):
                raise ValueError("tool_result_not_object")
            pending.remove(message["tool_call_id"])
        elif role == "user":
            user_count += 1
            if user_count > 1:
                raise ValueError("multiple_customer_turns_out_of_scope")
            if set(message) != {"role", "content"} or last_role not in ("system", "assistant"):
                raise ValueError("invalid_user_turn")
        else:
            if "tool_call_id" in message or last_role not in ("user", "tool"):
                raise ValueError("invalid_assistant_turn")
            calls = message.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list) or not calls:
                    raise ValueError("invalid_tool_calls")
                if message.get("content") in (None, "", "null"):
                    if "content" in message:
                        message.pop("content")
                        changes.append(f"removed_empty_tool_content:{index}")
                for call in calls:
                    if (not isinstance(call, dict) or set(call) != {"id", "type", "function"}
                            or call["type"] != "function" or not isinstance(call["id"], str)
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call["id"])
                            or call["id"] in seen):
                        raise ValueError("invalid_or_reused_tool_call_id")
                    function = call["function"]
                    if not isinstance(function, dict) or set(function) != {"name", "arguments"}:
                        raise ValueError("invalid_function")
                    if function["name"] not in definitions or not isinstance(function["arguments"], str):
                        raise ValueError("unknown_tool_or_invalid_arguments")
                    arguments = parse_json(function["arguments"])
                    validate_value(arguments, definitions[function["name"]])
                    function["arguments"] = canonical(arguments)
                    seen.add(call["id"])
                    pending.add(call["id"])
                    call_count += 1
        if "content" in message:
            if not isinstance(message["content"], str) or not message["content"].strip():
                raise ValueError("empty_content")
            if "\ufffd" in message["content"]:
                raise ValueError("replacement_character")
        elif not message.get("tool_calls"):
            raise ValueError("missing_content")
        last_role = role
    if pending or last_role != "assistant" or messages[-1].get("tool_calls"):
        raise ValueError("incomplete_conversation")
    if messages[1]["role"] != "user" or not call_count:
        raise ValueError("missing_user_or_tool_training_signal")
    if PRIVATE.search(canonical(messages)):
        raise ValueError("possible_private_content")
    return {"conversation_id": row["conversation_id"], "category": row["category"],
            "source_kind": row.get("source_kind", "unreviewed_export"),
            "messages": messages, "tools": session.tools}, changes


def _grouped(rows):
    groups = []
    for index, row in enumerate(rows):
        orders = set(ORDER.findall(canonical(row["messages"])))
        keys = {"conversation:" + row["conversation_id"]} | {"order:" + x for x in orders}
        overlaps = [g for g in groups if g["keys"] & keys]
        merged = {"keys": set(keys), "indices": [index]}
        for group in overlaps:
            merged["keys"].update(group["keys"])
            merged["indices"].extend(group["indices"])
            groups.remove(group)
        groups.append(merged)
    return groups


def partitions(rows, seed=42):
    if type(seed) is not int:
        raise ValueError("seed_must_be_integer")
    if len({r["conversation_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate_conversation_id")
    semantic = []
    for row in rows:
        messages = deepcopy(row["messages"])
        for message in messages:
            message.pop("tool_call_id", None)
            for call in message.get("tool_calls", []):
                call.pop("id", None)
        semantic.append(hashlib.sha256(canonical(messages).encode()).hexdigest())
    if len(set(semantic)) != len(semantic):
        raise ValueError("duplicate_semantic_conversation")
    groups = _grouped(rows)
    if len(groups) < 10:
        raise ValueError("at_least_10_independent_groups_required_for_four_splits")
    random.Random(seed).shuffle(groups)
    n = len(groups)
    n_train, n_val, n_dev = int(n * .6), max(1, int(n * .15)), max(1, int(n * .15))
    selections = {
        "train": groups[:n_train],
        "validation": groups[n_train:n_train + n_val],
        "development": groups[n_train + n_val:n_train + n_val + n_dev],
        "final": groups[n_train + n_val + n_dev:],
    }
    return {name: [rows[i] for group in selected for i in group["indices"]]
            for name, selected in selections.items()}


def next_actions(rows):
    cases = []
    for row in rows:
        for index, message in enumerate(row["messages"]):
            if message["role"] == "assistant":
                cases.append({
                    "case_id": f"{row['conversation_id']}-a{index}",
                    "conversation_id": row["conversation_id"], "category": row["category"],
                    "messages": row["messages"][:index], "tools": row["tools"],
                    "reference": message, "reference_text": message.get("content"),
                    "ground_truth": message.get("tool_calls", []),
                    "kind": "tool" if message.get("tool_calls") else "text",
                })
    return cases


def prepare(input_path, output, seed=42):
    input_path, output = Path(input_path), Path(output)
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}")
    rows, audit = [], []
    for line, raw in enumerate(input_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row, changes = normalize(parse_json(raw))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            audit.append({"line": line, "status": "held", "reason": str(error),
                          "raw_sha256": hashlib.sha256(raw.encode()).hexdigest()})
        else:
            rows.append(row)
            audit.append({"line": line, "status": "candidate", "changes": changes})
    output.mkdir(parents=True)
    write_json(output / "audit.json", {
        "input_sha256": sha256(input_path), "rows": audit,
        "business_quality_review": "required",
        "privacy_review": "required_regex_is_not_a_privacy_guarantee",
    })
    if any(row["status"] == "held" for row in audit):
        raise ValueError("Rows held; inspect audit.json, repair the source in a NEW version and rerun")
    split = partitions(rows, seed)
    write_jsonl(output / "normalized.jsonl", rows)
    for name, values in split.items():
        write_jsonl(output / f"{name}.jsonl", [
            {"messages": r["messages"], "tools": r["tools"]} for r in values])
    write_jsonl(output / "next-actions.jsonl", next_actions(split["development"]))
    files = sorted(output.glob("*.jsonl"))
    manifest = {
        "schema_version": 1, "seed": seed, "input_sha256": sha256(input_path),
        "split_by": "connected conversation/order groups; deterministic 60/15/15/remainder",
        "partitions": {name: [{"conversation_id": r["conversation_id"], "category": r["category"]}
                              for r in values] for name, values in split.items()},
        "category_counts": {name: dict(Counter(r["category"] for r in values))
                           for name, values in split.items()},
        "output_hashes": {p.name: sha256(p) for p in files},
        "source_kinds": sorted({r["source_kind"] for r in rows}),
        "quality_review_approved": False,
        "limitations": [
            "Development next-actions are correlated decisions, not independent conversations.",
            "Final traces must remain unseen during tuning; E2E cases require separate input/oracles.",
            "Order separation does not prove independence from shared synthetic templates.",
            "Category coverage must be reviewed; this split is not stratified.",
            "Scripted samples are not teacher-generated evidence or sufficient SFT data.",
        ],
    }
    write_json(output / "manifest.json", manifest)
    return manifest
