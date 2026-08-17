import sys, time, traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from src.config import load_config
from src.llm.client import LLMClient
from src.brand.loader import load_brand_knowledge
from src.pipeline import planner
from src.errors import AgentError

briefs = Path(
    r"C:\Users\ssabev\AppData\Local\Temp\claude\C--Users-ssabev-Desktop-Custom-Linkedin-Agent\edd884d7-b2cf-42c1-9c4f-0b2d7be53878\scratchpad\briefs.txt"
).read_text(encoding="utf-8").splitlines()
briefs = [b.strip() for b in briefs if b.strip()]

config = load_config()
client = LLMClient(config)
knowledge = load_brand_knowledge()

base = Path("posts/_stress-batch")
results = []

for i, brief in enumerate(briefs, start=1):
    post_dir = base / f"{i:02d}"
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "briefing.md").write_text(brief + "\n", encoding="utf-8")

    started = time.monotonic()
    try:
        plan = planner.run(post_dir, client, knowledge.stable_context, brief, force=True)
        dur = time.monotonic() - started
        results.append({
            "i": i, "brief": brief, "ok": True, "dur": round(dur, 1),
            "post_type": plan.post_type, "format": plan.format,
            "is_sensitive": plan.is_sensitive, "needs_photos": plan.needs_photos,
            "notes_len": len(plan.notes or ""),
        })
        print(f"[{i:02d}] OK  {dur:5.1f}s  тип='{plan.post_type}'  формат='{plan.format}'  sensitive={plan.is_sensitive}  :: '{brief}'")
    except AgentError as exc:
        dur = time.monotonic() - started
        results.append({"i": i, "brief": brief, "ok": False, "dur": round(dur, 1), "error": type(exc).__name__, "detail": exc.human_message})
        print(f"[{i:02d}] FAIL {dur:5.1f}s  {type(exc).__name__}: {exc.human_message[:150]}  :: '{brief}'")
    except Exception as exc:
        dur = time.monotonic() - started
        results.append({"i": i, "brief": brief, "ok": False, "dur": round(dur, 1), "error": type(exc).__name__, "detail": str(exc)})
        print(f"[{i:02d}] CRASH {dur:5.1f}s  {type(exc).__name__}: {exc}  :: '{brief}'")
        traceback.print_exc()

ok_count = sum(1 for r in results if r["ok"])
print(f"\n=== ОБОБЩЕНИЕ: {ok_count}/{len(results)} успешни ===")
for r in results:
    if not r["ok"]:
        print(f"  ПРОВАЛ [{r['i']:02d}] '{r['brief']}' -> {r['error']}: {r['detail'][:150]}")
