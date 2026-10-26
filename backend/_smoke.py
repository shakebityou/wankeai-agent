"""临时冒烟：确认 RAG / KB 路由能真实跑通。"""
import json
from fastapi.testclient import TestClient
from app.main import create_app

app = create_app()
c = TestClient(app)

print("=== GET /internal/v1/kb/stats ===")
r = c.get("/internal/v1/kb/stats")
print(r.status_code, json.dumps(r.json(), ensure_ascii=False)[:600])

print("\n=== POST /internal/v1/kb/import (dry_run) ===")
r = c.post("/internal/v1/kb/import", json={"source_path": "data/FAQ知识库.csv", "dry_run": True})
d = r.json()
print(r.status_code, "code=", d.get("code"))
data = d.get("data") or {}
print("keys:", list(data)[:10])
for k in ("source_path", "total_rows", "total_chunks", "sample_titles"):
    if k in data:
        v = data[k]
        print(f"  {k} = {v if not isinstance(v, list) else v[:3]}")

print("\n=== POST /internal/v1/rag/retrieve ===")
r = c.post("/internal/v1/rag/retrieve", json={"query": "盲盒可以退换吗", "channel": "TAOBAO"}, params={"with_context": "true"})
d = r.json()
print(r.status_code, "code=", d.get("code"))
data = d.get("data") or {}
print("decision=", data.get("decision"), "top_score=", data.get("top_score"), "cache_hit=", data.get("cache_hit"))
print("hits=", len(data.get("hits") or []), "answer_context=", len(data.get("answer_context") or []))
for h in (data.get("hits") or [])[:2]:
    print("   -", h.get("chunk_id"), round(h.get("score") or 0, 4), (h.get("title") or "")[:40])

print("\n=== 第二次同问（应命中缓存） ===")
r = c.post("/internal/v1/rag/retrieve", json={"query": "盲盒可以退换吗", "channel": "TAOBAO"})
d = r.json()
print("cache_hit=", (d.get("data") or {}).get("cache_hit"))
