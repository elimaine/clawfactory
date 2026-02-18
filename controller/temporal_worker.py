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
import os
from datetime import timedelta

import httpx
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "127.0.0.1:7233")
GATEWAY_PORT = os.environ.get("GATEWAY_PORT", "18789")
GATEWAY_INTERNAL_TOKEN = os.environ.get("GATEWAY_INTERNAL_TOKEN", "")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "default")
TASK_QUEUE = "clawfactory"


@activity.defn
async def trigger_agent_turn(agent_id: str) -> str:
    """POST to the gateway API to trigger an agent turn (same mechanism as cron)."""
    url = f"http://127.0.0.1:{GATEWAY_PORT}/api/cron/fire"
    headers = {}
    if GATEWAY_INTERNAL_TOKEN:
        headers["Authorization"] = f"Bearer {GATEWAY_INTERNAL_TOKEN}"

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json={"job_id": agent_id}, headers=headers)
        resp.raise_for_status()
        return resp.text


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
            retry_policy=workflow.RetryPolicy(
                initial_interval=timedelta(seconds=30),
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        )
        workflow.logger.info(f"Research phase completed: {result}")

        # Phase 2: Wait for research to settle
        await asyncio.sleep(timedelta(hours=2).total_seconds())

        # Phase 3: Synthesize results
        result = await workflow.execute_activity(
            trigger_agent_turn,
            "poetry-research-synthesize",
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=workflow.RetryPolicy(
                initial_interval=timedelta(seconds=30),
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        )
        workflow.logger.info(f"Synthesis phase completed: {result}")

        return "Poetry research workflow completed"


async def main():
    client = await Client.connect(TEMPORAL_HOST)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PoetryResearchWorkflow],
        activities=[trigger_agent_turn],
    )
    print(f"[temporal-worker] Starting on {TEMPORAL_HOST}, queue={TASK_QUEUE}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
