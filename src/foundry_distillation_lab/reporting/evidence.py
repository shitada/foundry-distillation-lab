"""Read-only, lineage-bound inventory for the automatic experiment cost report."""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat

from ..io import canonical, parse_json
from ..training.execution import target as validated_target


MODELS = ("teacher", "base", "fine_tuned")
TOKENS = ("input_tokens", "output_tokens", "cached_input_tokens")


def _link(path):
    # Detect junctions on Python 3.11 without rejecting OneDrive placeholders.
    return path.is_symlink() or getattr(path.lstat(), "st_reparse_tag", 0) == getattr(
        stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def _time(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


class _Inventory:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.report = {
            "schema_version": 1, "study_dir": None, "graded_dirs": {}, "data_dir": None,
            "calls": [], "training": [], "generation": {"known": False, "zero_cost_reason": None},
            "evaluation_complete": False, "training_complete": False,
            "diagnostics": [], "exclusions": [], "files": [],
        }
        self.paths, self.cache, self.digests, self.calls = set(), {}, {}, {}
        self.incomplete = set()

    def note(self, path, reason, *, exclude=False):
        item = {"source": str(path), "reason": reason}
        field = "exclusions" if exclude else "diagnostics"
        if item not in self.report[field]:
            self.report[field].append(item)

    def scan(self):
        if not self.root.is_dir():
            self.note(self.root, "runs directory is missing")
            return
        if any(_link(p) for p in (self.root, *self.root.parents)):
            self.note(self.root, "symlink/junction root rejected")
            return
        for directory, dirs, files in os.walk(
                self.root, followlinks=False, onerror=lambda exc: self.note(exc.filename, str(exc))):
            parent = Path(directory)
            for name in list(dirs):
                path = parent / name
                if _link(path):
                    dirs.remove(name)
                    self.note(path, "symlink/junction skipped")
                elif (name.startswith("cost-") or name.startswith("cost_")
                      or name in {"cost", "costs", "__pycache__"}
                      or {"cost-report.json", "estimate-start.json"} & set(files)):
                    dirs.remove(name)
            if {"cost-report.json", "estimate-start.json"} & set(files) or parent.name.startswith(("cost-", "cost_")):
                continue
            for name in files:
                path = parent / name
                if _link(path):
                    self.note(path, "symlink skipped")
                else:
                    self.paths.add(path)

    def raw(self, path):
        path = Path(path).absolute()
        if path not in self.paths:
            raise ValueError(f"missing or outside safe runs inventory: {path}")
        if any(_link(p) for p in (path, *path.parents)):
            raise ValueError(f"symlink/junction rejected while reading: {path}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if path in self.digests and self.digests[path] != digest:
            raise ValueError(f"content changed during discovery: {path}")
        self.digests[path] = digest
        return raw

    def read(self, path):
        path = Path(path)
        if path not in self.cache:
            value = parse_json(self.raw(path).decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object: {path}")
            self.cache[path] = value
        return self.cache[path]

    def checked(self, path, schema, value):
        data = self.read(path)
        if type(data.get(schema)) is not type(value) or data.get(schema) != value:
            raise ValueError(f"invalid {schema}: {path}")
        return data

    def check_hash(self, path, digest):
        self.raw(path)
        if not isinstance(digest, str) or self.digests[Path(path)] != digest:
            raise ValueError(f"snapshot hash mismatch: {path}")

    def named(self, name):
        return sorted(p for p in self.paths if p.name == name)

    def usage(self, value, source):
        value = value if isinstance(value, dict) else {}
        details = value.get("input_tokens_details") or value.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            self.note(source, "invalid input token details")
            details = {}
        normalized = {
            "input_tokens": value.get("input_tokens", value.get("prompt_tokens")),
            "output_tokens": value.get("output_tokens", value.get("completion_tokens")),
            "cached_input_tokens": value.get("cached_input_tokens", details.get("cached_tokens")),
        }
        for key, count in normalized.items():
            if count is not None and (type(count) is not int or count < 0):
                self.note(source, f"invalid {key}; usage unknown")
                normalized[key] = None
        if (normalized["cached_input_tokens"] is not None and normalized["input_tokens"] is not None
                and normalized["cached_input_tokens"] > normalized["input_tokens"]):
            self.note(source, "cached tokens exceed input tokens")
            normalized["cached_input_tokens"] = None
        return normalized

    def call(self, kind, variant, endpoint, target, response, source, request_id,
             outcome, timestamp, identity, usage=None, source_metadata=None):
        response = response if isinstance(response, dict) else {}
        response_id = response.get("response_id", response.get("id"))
        entry = {
            "kind": kind, "variant": variant, "endpoint": endpoint, "target": target,
            "usage": self.usage(response.get("usage") if usage is None else usage, source),
            "response_model": response.get("response_model", response.get("model")),
            "response_id": response_id, "source": str(source), "request_id": request_id,
            "outcome": outcome, "timestamp": timestamp,
        }
        if source_metadata is not None:
            entry["source_metadata"] = source_metadata
        key = (("response", "generation" if kind == "generation" else endpoint, response_id)
               if response_id else ("evidence", kind, identity))
        if key in self.calls:
            previous = self.calls[key]
            conflict = any(previous[k] is not None and entry[k] is not None and previous[k] != entry[k]
                           for k in ("target", "variant", "kind"))
            for token in TOKENS:
                before, after = previous["usage"][token], entry["usage"][token]
                if before is None:
                    previous["usage"][token] = after
                elif after is not None and before != after:
                    conflict = True
            for field in ("endpoint", "target", "response_model"):
                if previous[field] is None:
                    previous[field] = entry[field]
            if conflict:
                self.note(source, "conflicting duplicate response/call evidence")
                previous["usage"] = dict.fromkeys(TOKENS)
                self.incomplete.add(kind)
            return
        self.calls[key] = entry
        self.report["calls"].append(entry)
        if any(entry["usage"][key] is None for key in TOKENS):
            self.note(source, "billable call usage is incomplete or unknown")

    def journals(self, directory, kind, variant, endpoint, target):
        attempts = directory / "attempts"
        starts = sorted(p for p in self.paths if p.parent == attempts and p.name.endswith(".started.json"))
        results = {p.name.removesuffix(".result.json"): p for p in self.paths
                   if p.parent == attempts and p.name.endswith(".result.json")}
        for path in starts:
            try:
                start = self.read(path)
                request_id = path.name.removesuffix(".started.json")
                if start.get("attempt_id") != request_id or not start.get("payload_sha256"):
                    raise ValueError("invalid journal start")
                _time(start["started_at"])
                result_path = results.pop(request_id, None)
                result = self.read(result_path) if result_path else {}
                if result and result.get("attempt_id") != request_id:
                    raise ValueError("journal result ID mismatch")
                response = result.get("result", {}).get("response", {})
                if kind == "generation" and isinstance(response, dict) and "response" in response:
                    response = response["response"]
                identity = (self.digests[path], self.digests.get(result_path))
                self.call(kind, variant, endpoint, target, response, result_path or path, request_id,
                          result.get("status", "outcome_unknown"), start["started_at"], identity)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.note(path, str(exc))
                self.incomplete.add(kind)
                self.call(kind, variant, endpoint, target, {}, path, path.stem,
                          "invalid_evidence", None, str(path))
        for path in results.values():
            self.note(path, "orphan journal result; request association is unverified")
            self.incomplete.add(kind)
        return len(starts)

    def finish(self):
        for path, digest in list(self.digests.items()):
            try:
                self.check_hash(path, digest)
            except (OSError, ValueError) as exc:
                self.note(path, str(exc))
                self.report["evaluation_complete"] = False
                self.report["training_complete"] = False
                self.report["generation"]["known"] = False
        self.report["files"] = [{"path": str(p), "sha256": d} for p, d in sorted(self.digests.items())]
        return self.report


def _study(inv):
    candidates = []
    undated = False
    for path in inv.named("study-start.json"):
        try:
            start = inv.read(path)
            candidates.append((_time(start["started_at"]), str(path), start))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            inv.note(path, f"invalid study start: {exc}")
            undated = True
    if undated:
        raise ValueError("cannot determine latest study while a study start is unreadable or undated")
    if not candidates:
        inv.note(inv.root, "no started local study found")
        return None
    _, name, start = max(candidates)
    directory = Path(name).parent
    inv.report["study_dir"] = str(directory)
    if (start.get("schema") != "retail-evaluation-study-v1" or start.get("mode") != "e2e"
            or start.get("runtime") != "local_tool_loop" or start.get("models") != list(MODELS)):
        raise ValueError("selected study schema/runtime/model roles are invalid")
    for filename in ("input.json", "evaluation-config.json", "grading-config.json"):
        inv.check_hash(directory / filename, start["snapshots"][filename])
    bundle = inv.read(directory / "input.json")
    config = inv.read(directory / "evaluation-config.json")
    if bundle.get("schema") != "retail-evaluation-input-v1" or bundle.get("models") != list(MODELS):
        raise ValueError("selected study input schema/model roles are invalid")
    if not isinstance(config.get("targets"), dict) or set(config["targets"]) != set(MODELS):
        raise ValueError("selected study target roles are invalid")
    if not config.get("base_url") or any(not config["targets"][m] for m in MODELS):
        raise ValueError("selected study endpoint/targets are missing")
    for model in MODELS:
        path = directory / f"{model}-graded"
        try:
            execution = inv.checked(path / "execution.json", "schema", "retail-evaluation-execution-v1")
            if execution.get("model") != model or execution.get("target") != config["targets"][model]:
                raise ValueError("selected graded role/target mismatch")
            if execution.get("config") != config or execution.get("mode") != "e2e":
                raise ValueError("selected graded execution configuration mismatch")
            if execution.get("source_input_sha256") != start["snapshots"]["input.json"]:
                raise ValueError("selected graded source input mismatch")
            for stem in ("input", "evidence"):
                inv.check_hash(path / f"{stem}.json", execution.get(f"{stem}_sha256"))
            if inv.read(path / "input.json") != bundle:
                raise ValueError("selected graded cases mismatch")
            grading = execution["grading"]
            inv.check_hash(path / "grading.json", grading["artifact_sha256"])
            inv.checked(path / "grading-start.json", "schema", "retail-model-grading-v1")
            inv.report["graded_dirs"][model] = str(path)
            if not execution.get("complete"):
                inv.note(path, "selected evaluation is incomplete")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            inv.note(path, f"selected graded evidence unavailable: {exc}")
    return bundle, config


def _data(inv, bundle):
    provenance = bundle.get("provenance", {})
    hashes = provenance.get("source_hashes")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("selected cases have no dataset source hashes")
    matches = []
    for path in inv.named("manifest.json"):
        try:
            if hashlib.sha256(inv.raw(path)).hexdigest() == hashes.get("manifest.json"):
                matches.append(path.parent)
        except (OSError, ValueError) as exc:
            inv.note(path, str(exc))
    preferred = Path(provenance.get("data_directory", "")).absolute()
    if preferred in matches:
        directory = preferred
    elif matches:
        directory = sorted(matches)[0]
    else:
        raise ValueError("selected dataset manifest cannot be bound inside runs")
    inv.report["data_dir"] = str(directory)
    for name, digest in hashes.items():
        if Path(name).name != name or "\\" in name or "/" in name:
            raise ValueError("invalid dataset source filename")
        inv.check_hash(directory / name, digest)
    manifest = inv.checked(directory / "manifest.json", "schema_version", 1)
    return hashes, manifest


def _evaluations(inv, bundle, config, hashes):
    complete = len(inv.report["graded_dirs"]) == len(MODELS)
    for path in inv.named("execution-start.json"):
        directory = path.parent
        try:
            execution = inv.checked(path, "schema", "retail-evaluation-execution-v1")
            model = execution.get("model")
            if model not in MODELS:
                inv.note(path, "not an individual model run", exclude=True)
                continue
            settings = execution.get("config", {})
            if settings.get("targets", {}).get(model) != execution.get("target"):
                raise ValueError("execution target differs from its role configuration")
            if (execution.get("target") != config["targets"][model]
                    or settings.get("base_url") != config["base_url"]):
                inv.note(path, "unrelated endpoint or target", exclude=True)
                continue
            original = inv.read(directory / "input.json")
            mode = execution.get("mode")
            associated = (
                mode == "e2e"
                and original.get("provenance", {}).get("source_hashes") == hashes
                and original.get("provenance", {}).get("kind") == bundle.get("provenance", {}).get("kind")
                and original.get("provenance", {}).get("version") == bundle.get("provenance", {}).get("version")
                and original.get("tools") == bundle.get("tools")
            ) or (
                mode == "next-action" and hashes.get("next-actions.jsonl")
                and original.get("source", {}).get("sha256") == hashes["next-actions.jsonl"]
                and original.get("source", {}).get("kind") == "development_next_actions_jsonl"
            )
            if not associated:
                if directory.parent == Path(inv.report["study_dir"]):
                    raise ValueError("selected model input no longer matches study snapshot")
                inv.note(path, "unrelated task/dataset lineage", exclude=True)
                continue
            if directory.parent == Path(inv.report["study_dir"]) and original != bundle:
                raise ValueError("selected model input no longer matches study snapshot")
            final = directory / "execution.json"
            if final in inv.paths:
                end = inv.read(final)
                for name in ("input", "evidence"):
                    inv.check_hash(directory / f"{name}.json", end.get(f"{name}_sha256"))
            grading_path = directory / "grading-start.json"
            if grading_path in inv.paths:
                metadata = inv.checked(grading_path, "schema", "retail-model-grading-v1")
                inv.check_hash(directory / "source-evidence.json", metadata["source_evidence_sha256"])
                inv.check_hash(directory / "source-evidence.json", execution.get("evidence_sha256"))
                judge = inv.read(directory / "grading-config.json")
                if metadata.get("protocol", {}).get("config") != judge:
                    raise ValueError("grading configuration mismatch")
                count = inv.journals(directory, "grading", model, judge["base_url"], judge["deployment"])
                grading_result = directory / "grading.json"
                expected = inv.read(grading_result).get("judge_calls") if grading_result in inv.paths else None
                if expected is not None and count < expected:
                    complete = False
                    inv.note(path, "grading journals are missing recorded judge calls")
            elif (directory / "source-evidence.json" in inv.paths
                  or execution.get("grading") or execution.get("review_source_evidence_sha256")):
                inv.note(path, "derivative evidence; inference calls not counted again", exclude=True)
            else:
                count = inv.journals(directory, "evaluation", model, settings["base_url"], execution["target"])
                if final in inv.paths:
                    records = inv.read(directory / "evidence.json").get("records", [])
                    expected = sum(record.get("model_calls", 1) for record in records)
                    if count < expected:
                        complete = False
                        inv.note(path, "evaluation journals are missing recorded model calls")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            inv.note(path, f"invalid evaluation evidence: {exc}")
            complete = False
    calls = [c for c in inv.report["calls"] if c["kind"] in {"evaluation", "grading"}]
    inv.report["evaluation_complete"] = bool(
        complete and calls and not inv.incomplete.intersection({"evaluation", "grading"})
        and all(all(c["usage"][t] is not None for t in TOKENS) for c in calls))


def _training(inv, hashes, config):
    jobs = {}
    complete = True
    for path in inv.named("upload-plan.json"):
        try:
            plan = inv.checked(path, "schema_version", 1)
            if plan.get("kind") != "training-upload":
                raise ValueError("invalid training upload role")
            source_hashes = {role: plan["files"][role]["source_sha256"] for role in ("train", "validation")}
            if any(source_hashes[r] != hashes.get(f"{r}.jsonl") for r in source_hashes):
                inv.note(path, "unrelated training dataset", exclude=True)
                continue
            settings, directory = plan["config"], path.parent
            if plan.get("target") != settings["project_endpoint"].rstrip("/") + "|" + settings["model"]:
                raise ValueError("training target/configuration mismatch")
            if settings.get("training_type") not in {"Standard", "GlobalStandard", "Developer"}:
                raise ValueError("invalid training type")
            inv.check_hash(directory / "config.json", plan["snapshot_config_sha256"])
            if inv.read(directory / "config.json") != settings:
                raise ValueError("training configuration snapshot differs from plan")
            submission_path = directory / "submit-plan.json"
            if submission_path in inv.paths:
                submission = inv.checked(submission_path, "schema_version", 1)
                if (submission.get("kind") != "training-submit"
                        or submission.get("target") != plan["target"]):
                    raise ValueError("training submission role/target mismatch")
                inv.check_hash(path, submission.get("upload_plan_sha256"))
            for role in source_hashes:
                item = plan["files"][role]
                if item["path"] != f"{role}.jsonl":
                    raise ValueError("invalid training file role")
                inv.check_hash(directory / item["path"], item["sha256"])
            # Deployment aliases need not equal the underlying training model.
            response_models = {c["response_model"] for c in inv.report["calls"]
                               if c["kind"] == "evaluation" and c["variant"] == "base"
                               and c["response_model"]}
            identities = {re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
                          for model in response_models | {config["targets"]["base"]}}
            if response_models and re.sub(r"-\d{4}-\d{2}-\d{2}$", "", settings["model"]) not in identities:
                inv.note(path, "training model differs from selected base response identity", exclude=True)
                continue
            responses = []
            receipt_path = directory / "job-receipt.json"
            if receipt_path in inv.paths:
                receipt = inv.checked(receipt_path, "schema_version", 1)
                if receipt.get("kind") != "training-job-receipt" or receipt.get("target") != plan["target"]:
                    raise ValueError("invalid training receipt role/target")
                responses.append((None, receipt["response"], receipt_path))
            unknown_starts = []
            for start_path in sorted(p for p in inv.paths if p.parent == directory / "attempts"
                                     and p.name.endswith(".started.json")):
                start = inv.read(start_path)
                attempt = start_path.name.removesuffix(".started.json")
                result_path = start_path.with_name(attempt + ".result.json")
                if not attempt.startswith(("training-submit-", "status-")):
                    continue
                if start.get("attempt_id") != attempt:
                    raise ValueError("training journal start ID mismatch")
                result = inv.read(result_path) if result_path in inv.paths else {}
                if result and result.get("attempt_id") != attempt:
                    raise ValueError("training journal result ID mismatch")
                response = result.get("result", {}).get("response", {})
                if attempt.startswith("training-submit-"):
                    if response.get("id"):
                        responses.append((result.get("finished_at"), response, result_path))
                    else:
                        unknown_starts.append((start, start_path))
                elif response.get("id"):
                    payload = {"operation": "training-status", "target": plan["target"],
                               "input_sha256": inv.digests[path],
                               "request": {"method": "GET", "kind": "job", "id": response["id"]}}
                    if start.get("payload_sha256") == hashlib.sha256(canonical(payload).encode()).hexdigest():
                        responses.append((result.get("finished_at"), response, result_path))
            ids = {r["id"] for _, r, _ in responses}
            for observation_path in sorted(p for p in inv.paths if p.parent == directory / "observations"):
                observation = inv.read(observation_path)
                if observation.get("kind") != "job" or observation.get("id") not in ids:
                    continue
                response = observation["response"]
                if response.get("id") != observation["id"]:
                    raise ValueError("job observation ID mismatch")
                if not any(response == r for _, r, _ in responses):
                    if response.get("finished_at") is not None:
                        stamp = _time(response["finished_at"]).isoformat()
                        responses.append((stamp, response, observation_path))
                    else:
                        inv.note(observation_path, "job observation has no timestamped journal; latest state unverified")
                        complete = False
            for stamp, response, source in responses:
                job_id = response["id"]
                key = (settings["project_endpoint"], job_id)
                observed = stamp or response.get("finished_at") or response.get("created_at")
                rank = _time(observed) if observed else datetime.min.replace(tzinfo=timezone.utc)
                tokens = response.get("trained_tokens")
                if tokens is not None and (type(tokens) is not int or tokens < 0):
                    raise ValueError("invalid trained_tokens")
                if response.get("status") not in {
                        "pending", "validating_files", "queued", "running", "cancelling",
                        "succeeded", "failed", "cancelled"}:
                    inv.note(source, "invalid or missing job status")
                    complete = False
                entry = {"path": str(directory), "model": settings["model"],
                         "project_endpoint": settings["project_endpoint"],
                         "training_type": settings["training_type"], "trained_tokens": tokens,
                         "status": response.get("status"), "job_id": job_id,
                         "created_at": response.get("created_at"), "finished_at": response.get("finished_at"),
                         "source_hashes": source_hashes}
                if key not in jobs or rank > jobs[key][0]:
                    jobs[key] = rank, entry
                elif rank == jobs[key][0] and any(
                        jobs[key][1][field] != entry[field] for field in ("trained_tokens", "status")):
                    inv.note(source, "conflicting job observations at the same timestamp")
                    jobs[key][1].update(trained_tokens=None, status="outcome_unknown")
            if unknown_starts and not responses:
                start, source = unknown_starts[0]
                key = ("unknown", inv.digests[source])
                jobs[key] = (datetime.min.replace(tzinfo=timezone.utc), {
                    "path": str(directory), "model": settings["model"],
                    "project_endpoint": settings["project_endpoint"], "training_type": settings["training_type"],
                    "trained_tokens": None, "status": "outcome_unknown", "job_id": None,
                    "created_at": start.get("started_at"), "finished_at": None, "source_hashes": source_hashes})
                inv.note(source, "training submission outcome unknown")
            elif not responses:
                inv.note(path, "associated training plan has no known submitted job")
                complete = False
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            inv.note(path, f"invalid training evidence: {exc}")
            complete = False
    inv.report["training"] = [item for _, item in jobs.values()]
    inv.report["training_complete"] = bool(
        complete and jobs and all(item["trained_tokens"] is not None for _, item in jobs.values()))
    if not jobs:
        inv.note(inv.report["data_dir"], "no associated training job evidence")


def _endpoint(source, model=None):
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        if "target" in source:
            target = source["target"]
            if not isinstance(target, str) or target.count("|") != 1:
                raise ValueError("invalid capture source target")
            endpoint, configured_model = target.split("|")
            validated_target(endpoint, configured_model)
            if model is not None and model != configured_model:
                raise ValueError("capture source target differs from request model")
            return endpoint.rstrip("/")
        return source.get("endpoint") or source.get("project_endpoint") or source.get("agent_endpoint")
    return None


def _generation(inv, manifest):
    if manifest.get("source_kinds") == ["illustrative_scripted_not_model_output"]:
        inv.report["generation"] = {
            "known": True, "zero_cost_reason": "illustrative_scripted_not_model_output: no model generation"}
        return
    matched, invalid, observations = False, False, []
    for path in inv.named("collection-manifest.json"):
        bound = False
        try:
            collection = inv.read(path)
            if collection.get("trace_sha256") != manifest.get("input_sha256"):
                inv.note(path, "unrelated generated traces", exclude=True)
                continue
            bound = True
            inv.checked(path, "schema_version", 1)
            inv.check_hash(path.parent / "traces.jsonl", collection["trace_sha256"])
            matched = True
            trace_ids = [parse_json(line)["conversation_id"] for line in
                         inv.raw(path.parent / "traces.jsonl").decode("utf-8-sig").splitlines() if line.strip()]
            observed_ids = [item["conversation_id"] for item in collection["observations"]]
            if (len(trace_ids) != len(set(trace_ids)) or sorted(trace_ids) != sorted(observed_ids)
                    or collection.get("conversations") != len(trace_ids)):
                raise ValueError("collection observations do not cover the bound traces exactly")
            for item in collection["observations"]:
                observations.append(item)
                usages = item.get("known_usage")
                usages = usages if isinstance(usages, list) else [usages]
                ids = item.get("response_ids", [item.get("response_id")])
                sources = item.get("sources", [item.get("source")])
                if (not usages or len(ids) != len(usages) or len(sources) != len(usages)
                        or item.get("model_calls", len(usages)) != len(usages)):
                    raise ValueError("collection call/usage/source counts differ")
                for index, usage in enumerate(usages):
                    inv.call("generation", "fine_tuned", _endpoint(sources[index], item.get("model")), item.get("model"),
                             {"response_id": ids[index], "response_model": item.get("model")}, path,
                             f"{item['conversation_id']}:{index + 1}", "imported", None,
                             (collection["trace_sha256"], item["conversation_id"], index), usage=usage,
                             source_metadata=sources[index])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            inv.note(path, f"invalid generation evidence: {exc}")
            invalid = invalid or bound
    for path in inv.named("invocation-plan.json"):
        try:
            plan = inv.checked(path, "schema_version", 1)
            if plan.get("kind") != "hosted-invocation":
                raise ValueError("invalid invocation role")
            associated = any(
                item.get("conversation_id") == plan.get("input", {}).get("conversation_id")
                and item.get("model") == plan.get("model")
                and any(_endpoint(source, item.get("model")) in {plan.get("project_endpoint"), plan.get("agent_endpoint")}
                        for source in item.get("sources", [item.get("source")]))
                for item in observations)
            if associated:
                inv.journals(path.parent, "generation", "fine_tuned",
                             plan["project_endpoint"], plan["model"])
            else:
                inv.note(path, "invocation cannot be bound to selected trace lineage", exclude=True)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            inv.note(path, f"invalid invocation evidence: {exc}")
    calls = [c for c in inv.report["calls"] if c["kind"] == "generation"]
    known = matched and not invalid and "generation" not in inv.incomplete and bool(calls) and all(
        c["endpoint"] and c["target"] and all(c["usage"][t] is not None for t in TOKENS) for c in calls)
    inv.report["generation"]["known"] = bool(known)
    if not known:
        inv.note(inv.report["data_dir"], "real generation acquisition/usage is unknown; not zero cost")


def discover_evidence(runs_dir):
    """Inventory only the latest started study and costs proven to share its lineage.

    This function neither invokes models nor changes artifacts. Diagnostics are part
    of the result, including invalid snapshots and unpriced/unknown observations.
    """
    inv = _Inventory(runs_dir)
    try:
        inv.scan()
        selected = _study(inv)
        if selected:
            bundle, config = selected
            hashes, manifest = _data(inv, bundle)
            _evaluations(inv, bundle, config, hashes)
            _training(inv, hashes, config)
            _generation(inv, manifest)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        inv.note(inv.report["study_dir"] or inv.root, str(exc))
    return inv.finish()
