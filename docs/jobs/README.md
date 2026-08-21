# Jobs

Each ask is an OS thread **and** a Discord thread on the user message. Two cooks at once. Same machine. Not a cloud VM. Follow-ups in that Discord thread stay there (steer) with thread history.

Analyze work overlaps. Implement writes serialize per `write_key` (the realm cwd) so two channels do not fight one working tree.

Cap is 8 live jobs.

## Listen path

`drain_inbound` persists the watermark, then `JobPool.submit`. An inbound message in a live job thread calls `orchestrator.steer` (append to the running worker) instead of submitting a sibling. Idle-thread follow-ups still start a new job in that thread. `--once` waits the pool. The host loop reaps finished receipts without blocking the next channel.

## Code

- `src/agent_discord/orchestration/jobs.py` — `JobPool`, `realm_write_key`
- `src/agent_discord/orchestration/listen.py` — submit instead of blocking `run_task`
- `src/agent_discord/cli.py` — host / listen loop
- Tests: `tests/test_jobs.py`, `tests/test_e2e_host.py`
