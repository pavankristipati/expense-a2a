import asyncio
import os
import time

import httpx
import streamlit as st

from a2a.client import A2ACardResolver, A2AClientError, ClientConfig, create_client
from a2a.helpers import get_data_parts, get_text_parts, new_data_part, new_message, new_raw_part, new_text_part
from a2a.types import Role, SendMessageRequest, TaskState

RECEIPT_AGENT_URL = "http://127.0.0.1:10000"
POLICY_AGENT_URL = "http://127.0.0.1:9999"
MY_TOKEN = os.environ["MANAGER_TOKEN"]  # the page calls the agents as the manager


# ---------- Part 1: talking to the agents (the same steps manager.py does) ----------

async def connect(http, url: str, skill: str, trace: list):
    card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()
    skills = [s.id for s in card.skills]
    trace.append({"who": card.name, "what": f"Read its card at {url}: version {card.version}, skills {skills}"})
    if skill not in skills:
        raise RuntimeError(f"{card.name} does not offer {skill}")
    return card.name, await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))


async def ask(client, message) -> tuple[dict, str]:
    data, text = {}, ""
    async for reply in client.send_message(SendMessageRequest(message=message)):
        if reply.task.status.state == TaskState.TASK_STATE_FAILED:
            raise RuntimeError((get_text_parts(reply.task.status.message.parts) or ["The agent failed."])[0])
        for artifact in reply.task.artifacts:
            data = (get_data_parts(artifact.parts) or [{}])[0]
            text = (get_text_parts(artifact.parts) or [""])[0]
    return data, text


async def run_check(picture: bytes, media_type: str, filename: str, note: str) -> dict:
    trace = []
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {MY_TOKEN}"}, timeout=60) as http:
        # Hop 1: the picture goes to the receipt agent.
        name, reader = await connect(http, RECEIPT_AGENT_URL, "read_receipt", trace)
        started = time.perf_counter()
        reading, _ = await ask(reader, new_message(parts=[
            new_raw_part(picture, media_type=media_type, filename=filename),
            new_text_part(text="Please read this receipt."),
        ], role=Role.ROLE_USER))
        receipt = reading.get("reading") or {}
        trace.append({"who": name, "what": f"Sent the picture ({len(picture) // 1024} KB). Got back merchant "
                      f"{receipt.get('merchant')}, total {receipt.get('total')}, checks: "
                      f"{', '.join(reading.get('checks', [])) or 'all good'}", "seconds": time.perf_counter() - started})

        # Hop 2: the reading, its checks, and the note go to the policy agent.
        name, policy = await connect(http, POLICY_AGENT_URL, "check_receipt_expense", trace)
        started = time.perf_counter()
        package = {"receipt": reading.get("reading"), "checks": reading.get("checks", []), "note": note}
        decision, sentence = await ask(policy, new_message(
            parts=[new_data_part(data=package, media_type="application/json")], role=Role.ROLE_USER))
        trace.append({"who": name, "what": f"Sent the receipt numbers, checks, and the note \"{note}\". "
                      f"Got back: {sentence}", "seconds": time.perf_counter() - started})
    return {"receipt": receipt, "decision": decision, "sentence": sentence, "trace": trace}


# ---------- Part 2: the page ----------

st.set_page_config(page_title="Expense check", layout="wide")
st.title("Expense check")
st.caption("A receipt agent reads the photo. A policy agent decides. This page carries the work between them over A2A.")

left, right = st.columns(2, gap="large")

with left:
    uploaded = st.file_uploader("Receipt photo", type=["png", "jpg", "jpeg"])
    note = st.text_input("Who was there and why", placeholder="Client dinner, 2 people")
    go = st.button("Check expense", type="primary", disabled=not (uploaded and note))
    if uploaded:
        st.image(uploaded, width=320)

if go:
    with right, st.spinner("Asking the agents..."):
        try:
            st.session_state.result = asyncio.run(
                run_check(uploaded.getvalue(), uploaded.type, uploaded.name, note))
            st.session_state.error = None
        except (A2AClientError, httpx.HTTPError, RuntimeError) as error:
            st.session_state.result = None
            st.session_state.error = f"{error}"

with right:
    if st.session_state.get("error"):
        st.error(st.session_state.error)
    result = st.session_state.get("result")
    if result:
        d = result["decision"]
        if d.get("decision") == "Approved":
            st.success(result["sentence"])
        else:
            st.error(result["sentence"])

        st.subheader("Why")
        st.write("Reason codes:", ", ".join(d.get("reasons", [])) or "none")
        st.table({k: [str(v)] for k, v in (d.get("facts") or {}).items()})

        st.subheader("What the receipt agent read")
        items = result["receipt"].get("items", [])
        if items:
            st.table({"item": [i["name"] for i in items],
                      "price": [f"{i['price']:.2f}" for i in items],
                      "alcohol": ["yes" if i.get("alcohol") else "" for i in items]})
        if result["receipt"].get("printed_notes"):
            st.warning("Printed on the receipt: " + " / ".join(result["receipt"]["printed_notes"]))

        st.subheader("Trace: every hop between the agents")
        for number, hop in enumerate(result["trace"], start=1):
            timing = f"  ({hop['seconds']:.2f} s)" if "seconds" in hop else ""
            st.markdown(f"**{number}. {hop['who']}**{timing}  \n{hop['what']}")
