import asyncio
import os
import sys
from pathlib import Path

import httpx

from a2a.client import A2ACardResolver, A2AClientError, ClientConfig, create_client
from a2a.helpers import get_data_parts, get_text_parts, new_data_part, new_message, new_raw_part, new_text_message, new_text_part
from a2a.types import Role, SendMessageRequest, TaskState

POLICY_AGENT_URL = "http://127.0.0.1:9999"
RECEIPT_AGENT_URL = "http://127.0.0.1:10000"
MY_TOKEN = os.environ["MANAGER_TOKEN"]  # loaded from Keychain into this window
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


# All calls share one web connection (http), which main() closes at the very end.
# Finds an agent through its card, checks it offers the skill we need, and returns a client for it.
async def connect(http, url: str, skill: str):
    card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()
    skills = [s.id for s in card.skills]
    print(f"  Found {card.name} at {url}, skills: {skills}")
    if skill not in skills:
        raise SystemExit(f"  {card.name} does not offer {skill}. Stopping.")
    return await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))


# Sends one message and returns the labeled data and the sentence from the answer.
async def ask(client, message) -> tuple[dict, str]:
    data, text = {}, ""
    async for reply in client.send_message(SendMessageRequest(message=message)):
        print("  Task status:", TaskState.Name(reply.task.status.state))
        if reply.task.status.state == TaskState.TASK_STATE_FAILED:
            # The agent failed and kept the details to itself. Show its plain message and stop.
            raise SystemExit("  " + (get_text_parts(reply.task.status.message.parts) or ["The agent failed."])[0])
        for artifact in reply.task.artifacts:
            data = (get_data_parts(artifact.parts) or [{}])[0]
            text = (get_text_parts(artifact.parts) or [""])[0]
    return data, text


def show_decision(data: dict, text: str) -> None:
    print("\nAnswer:", text)
    print("Why:")
    print("   decision:", data.get("decision"))
    print("   reasons: ", data.get("reasons") or "none")
    print("   facts the agent used:", data.get("facts"))


# Typed expense: one agent, same as before.
async def typed_expense(http, expense: str) -> None:
    print("Step 1: policy agent")
    policy = await connect(http, POLICY_AGENT_URL, "check_expense")
    data, text = await ask(policy, new_text_message(expense, role=Role.ROLE_USER))
    show_decision(data, text)


# Receipt expense: two agents, one after the other.
async def receipt_expense(http, picture: Path, note: str) -> None:
    print("Step 1: receipt agent reads the picture")
    reader = await connect(http, RECEIPT_AGENT_URL, "read_receipt")
    picture_message = new_message(parts=[
        new_raw_part(picture.read_bytes(), media_type=MEDIA_TYPES[picture.suffix.lower()], filename=picture.name),
        new_text_part(text="Please read this receipt."),
    ], role=Role.ROLE_USER)
    reading, _ = await ask(reader, picture_message)
    receipt = reading.get("reading") or {}
    print(f"  Read: {receipt.get('merchant')}, total {receipt.get('total')}, checks {reading.get('checks') or 'all good'}")

    print("Step 2: policy agent decides, using the receipt's numbers and your note")
    policy = await connect(http, POLICY_AGENT_URL, "check_receipt_expense")
    package = {"receipt": reading.get("reading"), "checks": reading.get("checks", []), "note": note}
    data, text = await ask(policy, new_message(parts=[new_data_part(data=package, media_type="application/json")],
                                               role=Role.ROLE_USER))
    show_decision(data, text)


async def main(args: list[str]) -> None:
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {MY_TOKEN}"}, timeout=60)
    try:
        if args[0] == "--receipt":
            await receipt_expense(http, Path(args[1]), args[2])
        else:
            await typed_expense(http, args[0])
    except A2AClientError as error:
        print("An agent did not answer:", error)
    await http.aclose()


if __name__ == "__main__":
    # python manager.py 'Client dinner, $180 for 2 people'
    # python manager.py --receipt tests/receipts/02_wine_dinner.png 'Client dinner, 2 people'
    asyncio.run(main(sys.argv[1:]))
