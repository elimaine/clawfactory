"""
One-time Temporal schedule setup for ClawFactory.

Run after deployment to create the poetry-research-daily schedule:
    python3 temporal_schedules.py

Env vars:
  TEMPORAL_HOST - Temporal gRPC address (default: 127.0.0.1:7233)
"""

import asyncio
import os

from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec, ScheduleIntervalSpec

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "127.0.0.1:7233")
TASK_QUEUE = "clawfactory"


async def main():
    client = await Client.connect(TEMPORAL_HOST)

    # Import the workflow class for type reference
    from temporal_worker import PoetryResearchWorkflow

    schedule_id = "poetry-research-daily"

    try:
        handle = client.get_schedule_handle(schedule_id)
        desc = await handle.describe()
        print(f"Schedule '{schedule_id}' already exists (next run: {desc.info.next_action_times})")
        print("To delete and recreate: temporal schedule delete --schedule-id poetry-research-daily")
        return
    except Exception:
        pass  # Schedule doesn't exist yet

    await client.create_schedule(
        schedule_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                PoetryResearchWorkflow.run,
                id="poetry-research",
                task_queue=TASK_QUEUE,
            ),
            spec=ScheduleSpec(
                cron_expressions=["0 4 * * *"],  # 4am daily
            ),
        ),
    )
    print(f"Schedule '{schedule_id}' created (fires at 4am daily)")
    print("View in Temporal UI → Schedules tab")


if __name__ == "__main__":
    asyncio.run(main())
