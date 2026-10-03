import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

from a2a.client import A2ACardResolver, A2AClientError, ClientConfig, create_client
from a2a.helpers import get_data_parts, new_message, new_raw_part, new_text_part
from a2a.types import Role, SendMessageRequest, TaskState

RECEIPT_AGENT_URL = "http://127.0.0.1:10000"
RUNS_PER_RECEIPT = int(sys.argv[1]) if len(sys.argv) > 1 else 5
SECONDS_TO_WAIT = 60
MY_TOKEN = os.environ["EVAL_TOKEN"]
FOLDER = Path("tests/receipts")


# Sends one receipt picture through A2A and returns the agent's reading, its checks, and the seconds taken.
async def read_once(client, file: Path) -> tuple[dict, float]:
    started = time.perf_counter()
    message = new_message(parts=[
        new_raw_part(file.read_bytes(), media_type="image/png", filename=file.name),
        new_text_part(text="Please read this receipt."),
    ], role=Role.ROLE_USER)
    answer = {"reading": None, "checks": ["no_answer"]}
    try:
        async for reply in client.send_message(SendMessageRequest(message=message)):
            if reply.task.status.state == TaskState.TASK_STATE_FAILED:
                answer = {"reading": None, "checks": ["agent_failed"]}
            for artifact in reply.task.artifacts:
                for data in get_data_parts(artifact.parts):
                    answer = data
    except A2AClientError:
        answer = {"reading": None, "checks": ["timeout_or_error"]}
    return answer, time.perf_counter() - started


# Grading is plain code: the total must match to the cent, the alcohol flag must match,
# and every check the answer key expects must be raised.
def grade(answer: dict, key: dict) -> list[str]:
    reading = answer.get("reading") or {}
    problems = []
    total = reading.get("total")
    if key["total"] is None:
        if total is not None:
            problems.append(f"guessed a total of {total}")
    elif total is None or abs(total - key["total"]) > 0.01:
        problems.append(f"total {total}, expected {key['total']}")
    alcohol = any(item.get("alcohol") for item in reading.get("items", []))
    if alcohol != key["alcohol"]:
        problems.append(f"alcohol {alcohol}, expected {key['alcohol']}")
    for check in key["checks"]:
        if check not in answer.get("checks", []):
            problems.append(f"missing check {check}")
    return problems


async def main() -> None:
    keys = json.loads((FOLDER / "answers.json").read_text())
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {MY_TOKEN}"}, timeout=SECONDS_TO_WAIT)
    card = await A2ACardResolver(httpx_client=http, base_url=RECEIPT_AGENT_URL).get_agent_card()
    client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))
    print(f"Testing {card.name} {card.version}: {len(keys)} receipts x {RUNS_PER_RECEIPT} runs\n")

    total_pass, seconds, unsteady = 0, [], []
    for key in keys:
        results = [await read_once(client, FOLDER / key["file"]) for _ in range(RUNS_PER_RECEIPT)]
        graded = [grade(answer, key) for answer, _ in results]
        passes = sum(not problems for problems in graded)
        total_pass += passes
        seconds += [s for _, s in results]
        totals = {(answer.get("reading") or {}).get("total") for answer, _ in results}
        if len(totals) > 1:
            unsteady.append(key["file"])
        mark = "PASS" if passes == RUNS_PER_RECEIPT else "FAIL"
        print(f"{mark}  {passes}/{RUNS_PER_RECEIPT}  {key['file']:<22} {key['tests']}")
        for problems in {tuple(p) for p in graded if p}:
            print(f"        {'; '.join(problems)}")

    await http.aclose()
    runs = len(keys) * RUNS_PER_RECEIPT
    print("\n----- Report -----")
    print(f"Pass rate:            {total_pass}/{runs} ({100 * total_pass / runs:.0f}%)")
    print(f"Unsteady receipts:    {unsteady or 'none'}  (different totals across runs)")
    print(f"Average seconds:      {sum(seconds) / len(seconds):.2f} per receipt")


if __name__ == "__main__":
    asyncio.run(main())
