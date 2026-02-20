"""
ClawFactory Temporal Worker

Connects to the Temporal server and runs workflow/activity implementations.
Env vars:
  TEMPORAL_HOST  - Temporal gRPC address (default: 127.0.0.1:7233)
  GATEWAY_PORT   - OpenClaw gateway HTTP port (default: 18789)
  GATEWAY_INTERNAL_TOKEN - Auth token for gateway API
  INSTANCE_NAME  - Bot instance name
"""

import asyncio
import json
import os
import re
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import httpx

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "127.0.0.1:7233")
GATEWAY_PORT = os.environ.get("GATEWAY_PORT", "18789")
GATEWAY_INTERNAL_TOKEN = os.environ.get("GATEWAY_INTERNAL_TOKEN", "")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "default")
TASK_QUEUE = "clawfactory"
KILLSWITCH_FILE = Path(f"/tmp/clawfactory-snapshot-sync/KILLSWITCH_{INSTANCE_NAME}")


def _killswitch_active() -> bool:
    return KILLSWITCH_FILE.exists()


@activity.defn
async def trigger_agent_turn(agent_id: str) -> str:
    """POST to the gateway API to trigger an agent turn (same mechanism as cron)."""
    if _killswitch_active():
        raise RuntimeError(f"Killswitch active for {INSTANCE_NAME} — refusing to fire agent turn")

    url = f"http://127.0.0.1:{GATEWAY_PORT}/api/cron/fire"
    headers = {}
    if GATEWAY_INTERNAL_TOKEN:
        headers["Authorization"] = f"Bearer {GATEWAY_INTERNAL_TOKEN}"

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json={"job_id": agent_id}, headers=headers)
        resp.raise_for_status()
        return resp.text


@activity.defn
async def check_weather(location: str) -> str:
    """Fetch current weather from wttr.in."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"https://wttr.in/{location}?format=j1")
        resp.raise_for_status()
        data = resp.json()
        current = data["current_condition"][0]
        return (
            f"{location}: {current['temp_F']}°F, "
            f"{current['weatherDesc'][0]['value']}, "
            f"humidity {current['humidity']}%, "
            f"wind {current['windspeedMiles']}mph {current['winddir16Point']}"
        )


@workflow.defn
class WeatherCheckWorkflow:
    """Simple workflow that checks the weather for a given location."""

    @workflow.run
    async def run(self, location: str) -> str:
        result = await workflow.execute_activity(
            check_weather,
            location,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                maximum_attempts=3,
            ),
        )
        workflow.logger.info(f"Weather result: {result}")
        return result


@workflow.defn
class PoetryResearchWorkflow:
    """Chains research → wait → synthesize with retry policies.

    Phase 1: Trigger 'poetry-research-execute' agent turn
    Phase 2: Wait 2 hours for research to settle
    Phase 3: Trigger 'poetry-research-synthesize' agent turn
    """

    @workflow.run
    async def run(self) -> str:
        # Phase 1: Execute research
        result = await workflow.execute_activity(
            trigger_agent_turn,
            "poetry-research-execute",
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        )
        workflow.logger.info(f"Research phase completed: {result}")

        # Phase 2: Wait for research to settle
        await workflow.sleep(timedelta(hours=2).total_seconds())

        # Phase 3: Synthesize results
        result = await workflow.execute_activity(
            trigger_agent_turn,
            "poetry-research-synthesize",
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        )
        workflow.logger.info(f"Synthesis phase completed: {result}")

        return "Poetry research workflow completed"


def parse_duration(s: str) -> int:
    """Parse a duration string like '30s', '5m', '2h' into seconds."""
    m = re.fullmatch(r"(\d+)\s*([smh])", s.strip().lower())
    if not m:
        raise ValueError(f"Invalid duration: {s!r} (expected e.g. '30s', '5m', '2h')")
    val, unit = int(m.group(1)), m.group(2)
    return val * {"s": 1, "m": 60, "h": 3600}[unit]


@activity.defn
async def http_request(params: str) -> str:
    """Make an HTTP request. Input is JSON: {"method", "url", "timeout"}."""
    p = json.loads(params)
    method = p.get("method", "GET").upper()
    url = p["url"]
    timeout = p.get("timeout", 30)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, url)
        resp.raise_for_status()
        return resp.text[:4000]


@workflow.defn
class CustomWorkflow:
    """Generic data-driven workflow that executes a sequence of steps from a JSON definition."""

    @workflow.run
    async def run(self, definition_json: str) -> str:
        defn = json.loads(definition_json)
        name = defn.get("name", "unnamed")
        steps = defn.get("steps", [])
        results = []

        for i, step in enumerate(steps):
            step_type = step.get("type")
            workflow.logger.info(f"[{name}] Step {i+1}/{len(steps)}: {step_type}")

            if step_type == "agent_turn":
                result = await workflow.execute_activity(
                    trigger_agent_turn,
                    step["agent_id"],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=30),
                        maximum_interval=timedelta(minutes=5),
                        maximum_attempts=3,
                    ),
                )
                results.append({"step": i, "type": "agent_turn", "result": result})

            elif step_type == "delay":
                secs = parse_duration(step["duration"])
                workflow.logger.info(f"[{name}] Sleeping {step['duration']} ({secs}s)")
                await asyncio.sleep(secs)
                results.append({"step": i, "type": "delay", "duration": step["duration"]})

            elif step_type == "http":
                params = json.dumps({
                    "method": step.get("method", "GET"),
                    "url": step["url"],
                    "timeout": step.get("timeout", 30),
                })
                result = await workflow.execute_activity(
                    http_request,
                    params,
                    start_to_close_timeout=timedelta(seconds=step.get("timeout", 30) + 10),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=5),
                        maximum_attempts=3,
                    ),
                )
                results.append({"step": i, "type": "http", "result": result[:500]})

            else:
                workflow.logger.warning(f"[{name}] Unknown step type: {step_type}")
                results.append({"step": i, "type": step_type, "error": "unknown step type"})

            workflow.logger.info(f"[{name}] Step {i+1}/{len(steps)} completed")

        return json.dumps({"name": name, "steps_completed": len(steps), "results": results})


async def main():
    client = await Client.connect(TEMPORAL_HOST)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PoetryResearchWorkflow, WeatherCheckWorkflow, CustomWorkflow],
        activities=[trigger_agent_turn, check_weather, http_request],
    )
    print(f"[temporal-worker] Starting on {TEMPORAL_HOST}, queue={TASK_QUEUE}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
