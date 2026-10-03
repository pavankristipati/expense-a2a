import asyncio
import json
import os
import sys
import time
from collections import defaultdict

import httpx

from a2a.client import A2ACardResolver, A2AClientError, ClientConfig, create_client
from a2a.helpers import get_data_parts, new_text_message
from a2a.types import Role, SendMessageRequest

POLICY_AGENT_URL = "http://127.0.0.1:9999"
RUNS_PER_CASE = int(sys.argv[1]) if len(sys.argv) > 1 else 5
SECONDS_TO_WAIT = 60
MY_TOKEN = os.environ["EVAL_TOKEN"]  # loaded from Keychain into this window  # how long to wait for one answer before counting it as an error


# Send one expense through A2A, exactly like the manager, and return the labeled answer and seconds taken.
async def ask_agent(client, expense: str) -> tuple[dict, float]:
    start = time.perf_counter()
    request = SendMessageRequest(message=new_text_message(expense, role=Role.ROLE_USER))
    answer = {"decision": "Error", "reasons": ["no_answer"]}
    try:
        async for reply in client.send_message(request):
            for artifact in reply.task.artifacts:
                for data in get_data_parts(artifact.parts):
                    answer = data
    except A2AClientError:
        # A timeout or failed call counts as a failed run, and the test keeps going.
        answer = {"decision": "Error", "reasons": ["timeout_or_error"]}
    return answer, time.perf_counter() - start


# Grading is plain code: the decision must match and the reason codes must match, in any order.
def is_pass(answer: dict, case: dict) -> bool:
    return answer["decision"] == case["decision"] and set(answer["reasons"]) == set(case["reasons"])


async def main() -> None:
    cases = json.load(open("tests/expenses.json"))
    # Every request from the test script carries its own token, so the agent can tell it apart from the manager.
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {MY_TOKEN}"}, timeout=SECONDS_TO_WAIT)
    card = await A2ACardResolver(httpx_client=http, base_url=POLICY_AGENT_URL).get_agent_card()
    client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))

    print(f"Testing {card.name} {card.version}: {len(cases)} cases x {RUNS_PER_CASE} runs\n")
    total_pass, seconds, by_type, unsteady, could_not_read, errors = 0, [], defaultdict(lambda: [0, 0]), [], 0, 0

    for case in cases:
        # One run at a time, so we never flood the agent or Claude with requests at once.
        results = [await ask_agent(client, case["expense"]) for _ in range(RUNS_PER_CASE)]
        passes = sum(is_pass(answer, case) for answer, _ in results)
        seconds += [s for _, s in results]
        could_not_read += sum("could_not_read" in answer["reasons"] for answer, _ in results)
        errors += sum(answer["decision"] == "Error" for answer, _ in results)
        total_pass += passes
        by_type[case["type"]][0] += passes
        by_type[case["type"]][1] += RUNS_PER_CASE

        distinct = {(a["decision"], tuple(sorted(a["reasons"]))) for a, _ in results}
        if len(distinct) > 1:
            unsteady.append(case["id"])
        mark = "PASS" if passes == RUNS_PER_CASE else "FAIL"
        print(f"{mark}  #{case['id']:<2} {passes}/{RUNS_PER_CASE}  {case['type']:<11} {case['expense']}")
        if passes < RUNS_PER_CASE:
            print(f"        expected {case['decision']} {case['reasons']}, got {sorted(distinct)}")

    await client.close()
    await http.aclose()
    runs = len(cases) * RUNS_PER_CASE
    print("\n----- Report -----")
    print(f"Pass rate:            {total_pass}/{runs} ({100 * total_pass / runs:.0f}%)")
    for kind, (p, n) in by_type.items():
        print(f"   {kind:<11}        {p}/{n}")
    print(f"Unsteady cases:       {unsteady or 'none'}  (different answers across runs)")
    print(f"Could not read:       {could_not_read}")
    print(f"Timeouts or errors:   {errors}")
    print(f"Average seconds:      {sum(seconds) / len(seconds):.2f} per expense")


if __name__ == "__main__":
    asyncio.run(main())
