import os, time, uuid
import requests
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException
from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import (Distance, FieldCondition, Filter, MatchValue,
                                  PayloadSchemaType, PointIdsList, PointStruct,
                                  VectorParams)

load_dotenv()
LYZR_API_KEY = os.getenv("LYZR_API_KEY", "")
LYZR_AGENT_ID = os.getenv("LYZR_AGENT_ID", "")
LYZR_REVIEWER_AGENT_ID = os.getenv("LYZR_REVIEWER_AGENT_ID", "")
LYZR_URL = "https://agent-prod.studio.lyzr.ai/v3/inference/chat/"
MEMORY = "hush_memory"
AUDIT = "hush_audit"
VISIBILITIES = ("private", "shared")

# --- Qdrant + embeddings ---
embedder = TextEmbedding("BAAI/bge-small-en-v1.5")  # 384 dimensions
qdrant = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.environ["QDRANT_API_KEY"])


def setup():
    if not qdrant.collection_exists(MEMORY):
        qdrant.create_collection(
            MEMORY, vectors_config=VectorParams(size=384, distance=Distance.COSINE))
        for field in ("uid", "visibility"):
            qdrant.create_payload_index(
                MEMORY, field_name=field, field_schema=PayloadSchemaType.KEYWORD)
    if not qdrant.collection_exists(AUDIT):
        qdrant.create_collection(
            AUDIT, vectors_config=VectorParams(size=1, distance=Distance.DOT))
        qdrant.create_payload_index(
            AUDIT, field_name="uid", field_schema=PayloadSchemaType.KEYWORD)


setup()


def embed(text: str) -> list:
    return next(iter(embedder.embed([text]))).tolist()


def log(uid: str, event: str, detail: str = ""):
    """Audit log. Never stores memory text, only ids and counts."""
    print(f"[hush] {event}: {detail}")
    qdrant.upsert(AUDIT, points=[PointStruct(
        id=str(uuid.uuid4()), vector=[1.0],
        payload={"uid": uid, "event": event, "detail": detail, "ts": time.time()})])


def uid_filter(uid: str, visibility: str = None) -> Filter:
    must = [FieldCondition(key="uid", match=MatchValue(value=uid))]
    if visibility:
        must.append(FieldCondition(key="visibility", match=MatchValue(value=visibility)))
    return Filter(must=must)


def remember(uid: str, text: str, source: str, visibility: str = "private") -> str:
    pid = str(uuid.uuid4())
    qdrant.upsert(MEMORY, points=[PointStruct(
        id=pid, vector=embed(text),
        payload={"uid": uid, "text": text, "source": source,
                 "visibility": visibility, "ts": time.time()})])
    log(uid, "memory_stored", f"source={source} visibility={visibility} id={pid}")
    return pid


def recall(uid: str, question: str, k: int = 5) -> list:
    """Only SHARED memories can ever reach the agents."""
    hits = qdrant.query_points(
        MEMORY, query=embed(question),
        query_filter=uid_filter(uid, "shared"), limit=k).points
    return [{"id": str(h.id), "text": h.payload["text"]} for h in hits]


def check_owner(uid: str, pid: str):
    pts = qdrant.retrieve(MEMORY, ids=[pid])
    if not pts or pts[0].payload.get("uid") != uid:
        raise HTTPException(404, "Memory not found")


# --- Lyzr agents, with Plan B fallback ---
def call_lyzr(uid: str, agent_id: str, message: str) -> str:
    # Fresh session every call: if a memory is made private later,
    # it can never linger in an old agent chat history.
    r = requests.post(
        LYZR_URL,
        headers={"Content-Type": "application/json", "x-api-key": LYZR_API_KEY},
        json={"user_id": uid, "agent_id": agent_id,
              "session_id": f"{agent_id}-{uid}-{uuid.uuid4()}",
              "message": message},
        timeout=60)
    r.raise_for_status()
    return r.json().get("response", "")


def run_agents(uid: str, question: str, context: list):
    """Agent 1 drafts an answer, Agent 2 (Privacy Reviewer) checks it."""
    steps = []
    if not context:
        return "None of your shared memories answer this.", "no_context", steps
    if LYZR_API_KEY and LYZR_AGENT_ID:
        ctx = "\n".join("- " + c for c in context)
        base = f"CONTEXT:\n{ctx}\n\nQUESTION: {question}"
        try:
            draft = call_lyzr(uid, LYZR_AGENT_ID, base)
            steps.append({"agent": "answer_agent", "output": draft})
            log(uid, "agent_answered", "answer agent done")
        except Exception as e:
            log(uid, "lyzr_failed", f"answer agent: {type(e).__name__}")
            draft = None
        if draft is not None:
            if LYZR_REVIEWER_AGENT_ID:
                try:
                    final = call_lyzr(uid, LYZR_REVIEWER_AGENT_ID,
                                      base + f"\n\nDRAFT ANSWER:\n{draft}")
                    steps.append({"agent": "privacy_reviewer", "output": final})
                    log(uid, "agent_reviewed", "privacy reviewer done")
                    return final, "lyzr_multi_agent", steps
                except Exception as e:
                    log(uid, "lyzr_failed", f"reviewer: {type(e).__name__}")
            return draft, "lyzr", steps
    # Plan B: no agent available, show the best-matching shared memories
    return ("Agent unavailable. Most relevant shared memories:\n- "
            + "\n- ".join(context)), "fallback", steps


app = FastAPI(title="Hush")


@app.get("/")
def health():
    return {"status": "Hush is running"}


# --- Omi webhook: fires when a conversation finishes ---
@app.post("/omi/conversation")
def on_conversation(uid: str, payload: dict = Body(...)):
    segments = payload.get("transcript_segments", [])
    transcript = " ".join(s.get("text", "") for s in segments)
    overview = (payload.get("structured") or {}).get("overview", "")
    text = (overview + " " + transcript).strip()
    if text:
        remember(uid, text, source="omi", visibility="private")
    return {"status": "ok"}


# --- Memory management ---
@app.post("/memories")
def add_memory(body: dict = Body(...)):
    visibility = body.get("visibility", "private")
    if visibility not in VISIBILITIES:
        raise HTTPException(400, "visibility must be 'private' or 'shared'")
    pid = remember(body["uid"], body["text"], "manual", visibility)
    return {"id": pid, "visibility": visibility}


@app.get("/memories")
def list_memories(uid: str):
    points, _ = qdrant.scroll(MEMORY, scroll_filter=uid_filter(uid),
                              limit=200, with_payload=True)
    items = [{"id": str(p.id), "text": p.payload["text"],
              "visibility": p.payload["visibility"],
              "source": p.payload["source"], "ts": p.payload["ts"]} for p in points]
    return sorted(items, key=lambda m: m["ts"], reverse=True)


@app.patch("/memories/{memory_id}")
def set_visibility(memory_id: str, body: dict = Body(...)):
    uid, visibility = body["uid"], body.get("visibility")
    if visibility not in VISIBILITIES:
        raise HTTPException(400, "visibility must be 'private' or 'shared'")
    check_owner(uid, memory_id)
    qdrant.set_payload(MEMORY, payload={"visibility": visibility}, points=[memory_id])
    log(uid, "visibility_changed", f"id={memory_id} now={visibility}")
    return {"id": memory_id, "visibility": visibility}


@app.delete("/memories/{memory_id}")
def forget(memory_id: str, uid: str):
    check_owner(uid, memory_id)
    qdrant.delete(MEMORY, points_selector=PointIdsList(points=[memory_id]))
    log(uid, "memory_deleted", f"id={memory_id}")
    return {"deleted": memory_id}


# --- Ask ---
@app.post("/ask")
def ask(body: dict = Body(...)):
    uid, question = body["uid"], body["question"]
    log(uid, "question_received")
    context = recall(uid, question)
    withheld = qdrant.count(MEMORY, count_filter=uid_filter(uid, "private"),
                            exact=True).count
    log(uid, "memories_retrieved",
        f"{len(context)} shared sent to agents, {withheld} private withheld")
    answer, mode, steps = run_agents(uid, question, [c["text"] for c in context])
    log(uid, "answered", f"mode={mode}")
    return {"answer": answer, "mode": mode, "steps": steps,
            "shared_used": context, "private_withheld": withheld}


# --- Audit log ---
@app.get("/audit")
def audit(uid: str):
    points, _ = qdrant.scroll(AUDIT, scroll_filter=uid_filter(uid),
                              limit=200, with_payload=True)
    events = [{"event": p.payload["event"], "detail": p.payload["detail"],
               "ts": p.payload["ts"]} for p in points]
    return sorted(events, key=lambda e: e["ts"], reverse=True)