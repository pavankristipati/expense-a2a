import asyncio
import os
import sys
from pathlib import Path

import httpx

from a2a.client import A2ACardResolver, A2AClientError, ClientConfig, create_client
from a2a.helpers import get_data_parts, new_message, new_raw_part, new_text_part
from a2a.types import Role, SendMessageRequest

RECEIPT_AGENT_URL = "http://127.0.0.1:10000"
MY_TOKEN = os.environ["MANAGER_TOKEN"]
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


# Sends one receipt picture to the receipt agent and prints what it read.
async def main(path: str) -> None:
    file = Path(path)
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {MY_TOKEN}"}, timeout=60)
    card = await A2ACardResolver(httpx_client=http, base_url=RECEIPT_AGENT_URL).get_agent_card()
    print(f"Found agent: {card.name}, skills: {[s.id for s in card.skills]}")
    client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))

    # The message carries the picture itself as a file part, plus a short line of text.
    message = new_message(parts=[
        new_raw_part(file.read_bytes(), media_type=MEDIA_TYPES[file.suffix.lower()], filename=file.name),
        new_text_part(text="Please read this receipt."),
    ], role=Role.ROLE_USER)
    try:
        async for reply in client.send_message(SendMessageRequest(message=message)):
            for artifact in reply.task.artifacts:
                for data in get_data_parts(artifact.parts):
                    r = data["reading"] or {}
                    print("Merchant:", r.get("merchant"), "| Date:", r.get("date"))
                    for item in r.get("items", []):
                        flag = "  (alcohol)" if item.get("alcohol") else ""
                        print(f"   {item['name']:<24} {item['price']:>8.2f}{flag}")
                    print("Subtotal:", r.get("subtotal"), "| Tax:", r.get("tax"), "| Total:", r.get("total"))
                    if r.get("printed_notes"):
                        print("Printed notes:", r["printed_notes"])
                    print("Checks:", data["checks"] or "all good")
    except A2AClientError as error:
        print("The receipt agent did not answer:", error)
    await client.close()
    await http.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
