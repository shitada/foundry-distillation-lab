"""Cloud-unverified hosted skeleton; bounded collection from a mounted input plan."""

import asyncio
import hashlib
from importlib.metadata import version
import os
from pathlib import Path


def main():
    from foundry_distillation_lab.io import read_json, sha256
    from foundry_distillation_lab.safety import Journal
    from foundry_distillation_lab.retail import RetailSession
    from agent_framework import Agent, AgentSession, ResponseStream
    from agent_framework.foundry import FoundryChatClient
    from agent_framework_foundry_hosting import ResponsesHostServer
    from azure.ai.projects.aio import AIProjectClient
    from azure.identity.aio import DefaultAzureCredential
    from capture import Capture
    from tools import bind_tools

    plan_path = Path(os.environ["COLLECTION_PLAN"])
    plan = read_json(plan_path)
    if plan.get("kind") != "collection" or plan.get("schema_version") != 1:
        raise ValueError("Expected a reviewed collection plan")
    config = plan["config"]
    if plan["target"] != config["project_endpoint"].rstrip("/") + "|" + config["model"]:
        raise ValueError("Collection model target does not match the plan")
    for key in ("max_model_calls", "max_tool_calls", "max_output_tokens", "conversation_seconds"):
        if type(config.get(key)) is not int or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    run_dir = Path(os.environ["COLLECTION_RUN_DIR"])
    package = Path(__file__).parent / "foundry_distillation_lab"
    runtime_identity = {
        "framework_agent_name": "retail-trace-collector",
        "model": config["model"],
        "endpoint": config["project_endpoint"],
        "package_versions": {name: version(name) for name in (
            "agent-framework-core", "agent-framework-foundry", "agent-framework-foundry-hosting",
            "azure-ai-projects", "openai")},
        "retail_hashes": {str(path.relative_to(package)): sha256(path)
                          for path in (package / "retail").rglob("*") if path.is_file()
                          and path.suffix in {".py", ".md", ".json"}},
        "hosted_binding": None,
        "runtime_hashes": {name: sha256(Path(__file__).parent / name)
                           for name in ("main.py", "capture.py", "tools.py", "requirements.txt")},
    }
    if not runtime_identity["retail_hashes"]:
        raise ValueError("Missing staged retail package; build the allowlisted context first")

    class IsolatedRetailAgent:
        name = "retail-trace-collector"

        def create_session(self, *, session_id=None):
            return AgentSession(session_id=session_id)

        def run(self, messages=None, *, stream=False, session=None, options=None, **kwargs):
            if not stream:
                raise ValueError("Hosted adapter requires streaming")

            async def updates():
                if kwargs or options or not isinstance(messages, list) or len(messages) != 1:
                    raise ValueError("One initial message only; runtime overrides are forbidden")
                message = messages[0]
                if message.role != "user" or any(c.type != "text" for c in message.contents):
                    raise ValueError("Only user text is supported")
                if session is not None and session.state:
                    raise ValueError("Conversation continuation is not supported")
                digest = hashlib.sha256(message.text.encode()).hexdigest()
                matches = [r for r in plan["inputs"] if r["input_sha256"] == digest]
                if len(matches) != 1:
                    raise ValueError("Input not uniquely present in the collection plan")
                selected = matches[0]
                if selected["prompt"] != message.text:
                    raise ValueError("Collection prompt does not match its recorded identity")
                journal = Journal(run_dir)
                attempt_id = "conversation-" + digest[:32]
                journal.start(attempt_id, {"input_sha256": digest})
                if session is not None:
                    session.state["started"] = attempt_id
                capture = Capture(plan, selected, run_dir, runtime_identity)
                capture.event("attempt_start", input_sha256=digest, source=capture.source,
                              runtime_identity=runtime_identity)
                status = "failed_or_unknown"
                try:
                    async with asyncio.timeout(config["conversation_seconds"]):
                        retail = RetailSession()
                        capture.set_contract(retail.tools, retail.system_prompt)
                        async with DefaultAzureCredential() as credential:
                            async with AIProjectClient(endpoint=config["project_endpoint"],
                                                       credential=credential) as project:
                                client = FoundryChatClient(project_client=project, model=config["model"])
                                async with client.client:
                                    client.client.max_retries = 0
                                    client.client._client.event_hooks["request"].append(capture.request)
                                    client.client._client.event_hooks["response"].append(capture.response)
                                    agent = Agent(client=client, name=self.name,
                                        instructions=retail.system_prompt, tools=bind_tools(retail, capture),
                                        default_options={"store": False,
                                                         "max_tokens": config["max_output_tokens"]})
                                    result = agent.run(messages, stream=True)
                                    async for update in result:
                                        yield update
                                    await result.get_final_response()
                                    if capture.tool_failure or capture.provider_calls:
                                        raise RuntimeError("Tool outcome or proposal linkage unknown")
                    journal.finish(attempt_id, "completed_unreviewed", {"model_calls": capture.counter})
                    status = "completed_unreviewed"
                except BaseException as exc:
                    journal.finish(attempt_id, "failed_or_unknown", {"error_type": type(exc).__name__})
                    raise
                finally:
                    capture.finish_unknown()
                    capture.finish(status)

            return ResponseStream(updates())

    ResponsesHostServer(IsolatedRetailAgent()).run()


if __name__ == "__main__":
    main()
