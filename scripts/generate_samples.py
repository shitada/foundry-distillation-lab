"""Create scripted Japanese trace-format examples; never calls a model."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from foundry_distillation_lab.io import canonical, write_jsonl
from foundry_distillation_lab.retail import RetailSession


def samples():
    for i in range(1, 21):
        session = RetailSession()
        order_id = f"ORD-{i:04d}"
        messages = [{"role": "user", "content": f"注文{order_id}の配送状況を教えてください。"}]
        result = None
        for j, name in enumerate(("get_order_details", "get_fulfillment_status"), 1):
            call_id = f"sample_{i}_{j}"
            arguments = {"order_id": order_id}
            result = session.call(name, arguments)
            messages.append({"role": "assistant", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": canonical(arguments)}}]})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": canonical(result)})
        messages.append({"role": "assistant", "content": f"注文{order_id}の配送状況は「{result['status']}」です。"})
        yield {"conversation_id": f"sample-{i:03d}", "category": "配送照会",
               "source_kind": "illustrative_scripted_not_model_output", "messages": messages}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_jsonl(args.output, list(samples()))


if __name__ == "__main__":
    main()
