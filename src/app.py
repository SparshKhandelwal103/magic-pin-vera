"""FastAPI app — the 5 challenge endpoints + optional teardown.

Wires the engines together. Every handler is defensive: a single bad context or
composition must never crash the server (health-probe failures = disqualification)
and `/v1/tick` must always return within budget, even if that means an empty
`actions` list. We never raise out of a handler.
"""
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .composer import Composer
from .context_engine import ContextStore
from .conversation_engine import ConversationEngine
from .trigger_engine import TriggerEngine
from .models import (ContextPush, ContextAck, TickRequest, TickResponse, Action,
                     ReplyRequest, ReplyResponse)

START = time.time()

app = FastAPI(title="Vera — magicpin Merchant AI", version="1.0.0")

store = ContextStore()
triggers = TriggerEngine(store)
composer = Composer()
conversations = ConversationEngine(store, triggers)


def _now(s: Optional[str]) -> datetime:
    if s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# liveness / identity                                                         #
# --------------------------------------------------------------------------- #
@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": store.counts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.getenv("VERA_TEAM_NAME", "Vera Reforged"),
        "team_members": [os.getenv("VERA_TEAM_MEMBER", "Sparsh Khandelwal")],
        "model": "deterministic-grounded-composer + gemini-1.5-flash polish",
        "approach": (
            "4-context resolver hydrates trigger references into ground-truth facts; "
            "modular engines (trigger prioritisation, merchant intelligence, engagement "
            "levers) build fact+insight+action+cta messages; self-scoring gate + LLM "
            "polish with deterministic fallback; stateful conversation engine with "
            "auto-reply detection and intent-transition routing."
        ),
        "contact_email": os.getenv("VERA_CONTACT", "sparshk103legend@gmail.com"),
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# context push                                                                #
# --------------------------------------------------------------------------- #
@app.post("/v1/context")
async def push_context(body: ContextPush):
    try:
        accepted, current = store.put(body.scope, body.context_id, body.version, body.payload)
        if not accepted:
            return JSONResponse(
                status_code=409,
                content=ContextAck(accepted=False, reason="stale_version",
                                   current_version=current).model_dump(exclude_none=True))
        return ContextAck(
            accepted=True,
            ack_id=f"ack_{body.context_id}_v{body.version}",
            stored_at=datetime.now(timezone.utc).isoformat() + "Z",
        ).model_dump(exclude_none=True)
    except Exception as e:  # never crash the warmup
        return JSONResponse(status_code=400,
                            content={"accepted": False, "reason": "invalid_payload",
                                     "details": str(e)})


# --------------------------------------------------------------------------- #
# tick — proactive sends                                                       #
# --------------------------------------------------------------------------- #
@app.post("/v1/tick")
async def tick(body: TickRequest):
    deadline = time.time() + 12.0  # stay well under the 30s budget
    now = _now(body.now)
    actions: list[Action] = []
    try:
        selected = triggers.select(body.available_triggers, now, max_actions=8)
        for s in selected:
            if time.time() > deadline:
                break
            try:
                msg = composer.compose(s.resolved)
            except Exception:
                continue  # skip a bad composition, never fail the whole tick
            if not msg.body.strip():
                continue
            merchant_id = s.resolved.merchant.get("merchant_id", "")
            customer_id = (s.resolved.customer or {}).get("customer_id")
            conv_id = _conversation_id(merchant_id, customer_id, s.trigger_id)

            if triggers.is_conversation_closed(conv_id):
                continue

            actions.append(Action(
                conversation_id=conv_id,
                merchant_id=merchant_id,
                customer_id=customer_id,
                send_as=msg.send_as,
                trigger_id=s.trigger_id,
                template_name=msg.template_name,
                template_params=msg.template_params,
                body=msg.body,
                cta=msg.cta,
                suppression_key=msg.suppression_key,
                rationale=msg.rationale,
            ))
            triggers.mark_fired(msg.suppression_key)
            conversations.register(conv_id, merchant_id, customer_id, s.trigger_id,
                                   msg.body, prepared_action=msg.deliverable or "the next step",
                                   offer_title=(s.resolved.offer or {}).get("title", ""))
    except Exception:
        traceback.print_exc()
        return TickResponse(actions=[]).model_dump()
    return TickResponse(actions=actions).model_dump()


# --------------------------------------------------------------------------- #
# reply — conversation continuation                                            #
# --------------------------------------------------------------------------- #
@app.post("/v1/reply")
async def reply(body: ReplyRequest):
    try:
        resp = conversations.handle_reply(
            conversation_id=body.conversation_id,
            merchant_id=body.merchant_id or "",
            message=body.message,
            turn_number=body.turn_number,
        )
        return resp.model_dump(exclude_none=True)
    except Exception:
        traceback.print_exc()
        return ReplyResponse(action="wait", wait_seconds=3600,
                             rationale="Internal hiccup; backing off rather than sending a bad reply.").model_dump(exclude_none=True)


# --------------------------------------------------------------------------- #
# optional teardown                                                            #
# --------------------------------------------------------------------------- #
@app.post("/v1/teardown")
async def teardown():
    global store, triggers, composer, conversations
    store = ContextStore()
    triggers = TriggerEngine(store)
    composer = Composer()
    conversations = ConversationEngine(store, triggers)
    return {"wiped": True}


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _conversation_id(merchant_id: str, customer_id: Optional[str], trigger_id: Optional[str]) -> str:
    base = customer_id or merchant_id or "conv"
    tail = (trigger_id or "").replace("trg_", "")
    return f"conv_{base}_{tail}".rstrip("_")
