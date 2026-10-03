import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from anthropic import AsyncAnthropic
from starlette.applications import Starlette
from starlette.authentication import SimpleUser
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from a2a.helpers import get_data_parts, get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    TaskState,
)

claude = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from this Terminal window
POLICY = Path("policy.txt").read_text()
MEAL_CAP_PER_PERSON = 75
AGENT_VERSION = "0.4.0"
LOG_FILE = Path("audit.log")


# Part 6: the audit log. Adds one line per event to audit.log and never rewrites old lines.
def write_log(entry: dict) -> None:
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    with LOG_FILE.open("a") as log:
        log.write(json.dumps(entry) + "\n")

READ_INSTRUCTIONS = (
    "You read expenses and report facts. You do not decide approval.\n"
    f"For context, this is the expense policy:\n{POLICY}\n"
    "Reply with JSON only, no other text, in exactly this shape:\n"
    '{"total": number or null, "people": number or null, '
    '"alcohol": true or false, "business_purpose": true or false}\n'
    "total: the dollar amount spent, or null if none is given.\n"
    "people: how many people, or null if not stated.\n"
    "alcohol: true only if alcohol was bought. 'No wine' means false.\n"
    "business_purpose: true if a business reason is given, such as a client meeting or team event."
)

# The fixed list of reason codes, and the sentence a person sees for each one.
REASON_TEXT = {
    "could_not_read": "could not read the expense",
    "no_amount": "no dollar amount found",
    "over_meal_cap": "over the $75 per person meal cap",
    "alcohol": "alcohol is not reimbursable",
    "no_business_purpose": "no business purpose stated",
    "receipt_unreadable": "the receipt could not be read",
    "receipt_does_not_add_up": "the receipt's numbers do not add up",
    "printed_notes_on_receipt": "the receipt has printed notes that need a person to review",
}

NOTE_INSTRUCTIONS = (
    "You read a short note that comes with a receipt and report facts. You do not decide approval.\n"
    "Reply with JSON only, no other text, in exactly this shape:\n"
    '{"people": number or null, "business_purpose": true or false}\n'
    "people: how many people the expense covered, or null if not stated.\n"
    "business_purpose: true if a business reason is given, such as a client meeting or team event."
)


# Part 1a: Claude READS the expense and reports facts (probabilistic).
async def read_expense(text: str) -> dict | None:
    reply = await claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=READ_INSTRUCTIONS,
        messages=[{"role": "user", "content": text}],
    )
    raw = reply.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# Part 1b: Python DECIDES using those facts (deterministic).
# Returns a labeled answer: decision, reason codes, and the facts it relied on.
def decide(facts: dict | None) -> dict:
    if facts is None:
        return {"decision": "Flagged", "reasons": ["could_not_read"], "facts": None}
    if facts.get("total") is None:
        return {"decision": "Flagged", "reasons": ["no_amount"], "facts": facts}
    people = facts.get("people") or 1
    reasons = []
    if facts["total"] / people > MEAL_CAP_PER_PERSON:
        reasons.append("over_meal_cap")
    if facts.get("alcohol"):
        reasons.append("alcohol")
    if not facts.get("business_purpose"):
        reasons.append("no_business_purpose")
    return {"decision": "Flagged" if reasons else "Approved", "reasons": reasons, "facts": facts}


# Turns the labeled answer into one sentence for a person to read.
def as_sentence(answer: dict) -> str:
    if not answer["reasons"]:
        return "Approved."
    return "Flagged: " + "; ".join(REASON_TEXT[code] for code in answer["reasons"])


# Part 1c: Claude READS the note that comes with a receipt (probabilistic).
async def read_note(note: str) -> dict | None:
    reply = await claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=NOTE_INSTRUCTIONS,
        messages=[{"role": "user", "content": note}],
    )
    raw = reply.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# Part 1d: Python DECIDES a receipt expense, using the receipt agent's numbers and the note (deterministic).
def decide_receipt(receipt: dict | None, checks: list, note_facts: dict | None) -> dict:
    if note_facts is None:
        return {"decision": "Flagged", "reasons": ["could_not_read"], "facts": None}
    if receipt is None or "could_not_read" in checks or "unreadable_photo" in checks:
        return {"decision": "Flagged", "reasons": ["receipt_unreadable"], "facts": None}
    facts = {
        "merchant": receipt.get("merchant"),
        "total": receipt.get("total"),
        "people": note_facts.get("people"),
        "alcohol": any(item.get("alcohol") for item in receipt.get("items", [])),
        "business_purpose": note_facts.get("business_purpose"),
    }
    if facts["total"] is None:
        return {"decision": "Flagged", "reasons": ["no_amount"], "facts": facts}
    reasons = []
    if "items_do_not_add_up" in checks or "totals_do_not_add_up" in checks:
        reasons.append("receipt_does_not_add_up")
    if "printed_notes_found" in checks:
        reasons.append("printed_notes_on_receipt")
    if facts["total"] / (facts["people"] or 1) > MEAL_CAP_PER_PERSON:
        reasons.append("over_meal_cap")
    if facts["alcohol"]:
        reasons.append("alcohol")
    if not facts["business_purpose"]:
        reasons.append("no_business_purpose")
    return {"decision": "Flagged" if reasons else "Approved", "reasons": reasons, "facts": facts}


async def check_receipt_expense(data: dict, caller: str) -> dict:
    note = data.get("note", "")
    note_facts = await read_note(note)
    answer = decide_receipt(data.get("receipt"), data.get("checks", []), note_facts)
    print("\n----- Policy agent (receipt) -----")
    print("Caller:        ", caller)
    print("Note:          ", note)
    print("Claude read:   ", note_facts)
    print("Receipt total: ", (data.get("receipt") or {}).get("total"), "| receipt checks:", data.get("checks"))
    print("Python decided:", answer["decision"], answer["reasons"])
    print("----------------------------------\n")
    return answer


# Part 1: the brain. Claude reads, Python decides, the server prints what happened.
async def check_expense(text: str, caller: str) -> dict:
    facts = await read_expense(text)
    answer = decide(facts)
    print("\n----- Policy agent -----")
    print("Caller:        ", caller)
    print("Expense:       ", text)
    print("Claude read:")
    for name, value in (facts or {}).items():
        print(f"   {name:<17} {value}")
    if facts is None:
        print("   (could not read Claude's reply)")
    print("Python decided:", answer["decision"], answer["reasons"])
    print("------------------------\n")
    return answer


# Part 2: the adapter between the A2A rules and the brain.
class PolicyExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)

        await updater.update_status(state=TaskState.TASK_STATE_WORKING)
        # The token check (Part 5) already ran, so we know who is calling.
        caller = context.call_context.user.user_name if context.call_context else "unknown"
        data_parts = get_data_parts(context.message.parts)
        started = time.perf_counter()
        if data_parts:
            # A receipt expense: numbers from the receipt agent, plus the person's note.
            expense = "receipt: " + data_parts[0].get("note", "")
            answer = await check_receipt_expense(data_parts[0], caller)
        else:
            # A typed expense, the original path, still used by eval.py.
            expense = get_message_text(context.message) or ""
            answer = await check_expense(expense, caller)
        write_log(
            {
                "event": "decision",
                "task_id": task.id,
                "caller": caller,
                "agent_version": AGENT_VERSION,
                "expense": expense,
                "facts": answer["facts"],
                "decision": answer["decision"],
                "reasons": answer["reasons"],
                "seconds": round(time.perf_counter() - started, 2),
            }
        )
        # The artifact now carries two parts: a sentence for people, and labeled data for programs.
        await updater.add_artifact(
            parts=[
                new_text_part(text=as_sentence(answer), media_type="text/plain"),
                new_data_part(data=answer, media_type="application/json"),
            ]
        )
        await updater.update_status(state=TaskState.TASK_STATE_COMPLETED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")


# Part 3: the agent card, the public description other agents read.
card = AgentCard(
    name="Policy Agent",
    description="Checks an expense against the company expense policy.",
    version=AGENT_VERSION,
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain", "application/json"],
    capabilities=AgentCapabilities(streaming=False),
    supported_interfaces=[
        AgentInterface(protocol_binding="JSONRPC", url="http://127.0.0.1:9999", protocol_version="1.0")
    ],
    # Tells every caller, before its first request, that a token is required.
    security_schemes={
        "bearer": SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(
                scheme="bearer", description="Each approved caller gets its own token."
            )
        )
    },
    security_requirements=[SecurityRequirement(schemes={"bearer": StringList(list=[])})],
    skills=[
        AgentSkill(
            id="check_expense",
            name="Check expense",
            description=(
                "Returns Approved or Flagged. Also returns JSON with decision, reason codes "
                "(could_not_read, no_amount, over_meal_cap, alcohol, no_business_purpose), and the facts used."
            ),
            tags=["expense", "policy"],
            examples=["Client dinner, $180 for 2 people, included wine"],
        ),
        AgentSkill(
            id="check_receipt_expense",
            name="Check receipt expense",
            description=(
                "Send JSON with the receipt agent's reading, its checks, and a note such as 'client dinner, 2 people'. "
                "Returns the same answer format, with extra reason codes receipt_unreadable, "
                "receipt_does_not_add_up, and printed_notes_on_receipt."
            ),
            tags=["expense", "policy", "receipt"],
        ),
    ],
)

# Part 5: the token check. Runs before any request reaches the agent.
# Each approved caller has its own token, loaded from Keychain into this window.
APPROVED_CALLERS = {
    os.environ["MANAGER_TOKEN"]: "manager",
    os.environ["EVAL_TOKEN"]: "eval",
}


class TokenCheck(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # The card stays public, so callers can learn that a token is required.
        if request.url.path.startswith("/.well-known/"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip()
        caller = APPROVED_CALLERS.get(token)
        if caller is None:
            print("Refused a request with a missing or unknown token.")
            # The token itself is never written to the log, because the log is not a safe place for secrets.
            write_log({"event": "refused", "reason": "missing or unknown token", "path": request.url.path})
            return JSONResponse({"error": "missing or invalid token"}, status_code=401)
        request.scope["user"] = SimpleUser(caller)
        return await call_next(request)


# Part 4: the server. Wire the pieces together and listen on port 9999.
handler = DefaultRequestHandler(agent_executor=PolicyExecutor(), task_store=InMemoryTaskStore(), agent_card=card)
routes = create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/")
app = Starlette(routes=routes, middleware=[Middleware(TokenCheck)])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9999)
