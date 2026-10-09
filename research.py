"""research.py - STUDENT IMPLEMENTS.  The main script.   Guide: GUIDE.md, part 3.

Usage:  python research.py "survey about world model"
Result: reports/<slug>.md   reports/<slug>.sources.json   reports/<slug>.meta.json
"""
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sys
import time  # noqa: F401

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
from collections import Counter  # noqa: F401
from pathlib import Path

from agents import FINALIZER_PATH, REPORT_PATH, SOURCES_PATH, VALIDATOR_PATH, WORKDIR, build_lead_agent  # noqa: F401
from model import make_model  # noqa: F401
from sandbox import download, open_sandbox, upload  # noqa: F401

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
VALIDATOR_SOURCE = ROOT / "check_citations.py"
FINALIZER_SOURCE = ROOT / "finalize_citations.py"   # provided: uploaded next to your validator


def slugify(topic):
    """Turn a topic into a safe file name: lower case, runs of non-word characters become one "-", max 60 chars,
    never empty (fall back to "topic"). The topic is user input: "../../x" must not escape reports/."""
    cleaned = re.sub(r"[^\w]+", "-", str(topic or "").strip().lower()).strip("-")
    truncated = cleaned[:60].rstrip("-")
    return truncated or "topic"


def build_prompt(topic):
    """The user message sent to the lead agent."""
    return (
        f"Please conduct an in-depth, authoritative research survey on the topic: '{topic}'.\n\n"
        f"MANDATORY REQUIREMENTS:\n"
        f"1. Plan with write_todos and break this topic into at least 3 independent sub-questions.\n"
        f"2. Delegate each sub-question to researcher subagents covering ALL source families: arXiv, Hugging Face (hf-daily / hf-search), and Web (Exa).\n"
        f"3. In the report body, cite sources across at least 3 distinct source families (e.g. arxiv, hf-daily/hf-search, web) using inline [n] citations.\n"
        f"4. Do NOT write a ## References section yourself. Execute `python3 {FINALIZER_PATH}` to generate it.\n"
        f"5. IMPORTANT: finalize_citations.py DROPS sources not cited in the text. Check that the surviving {SOURCES_PATH} has at least 3 distinct source families ('source' field: at least 3 of arxiv, hf-daily, hf-search, web). If not, edit the report to cite the missing sources and re-run finalizer.\n"
        f"6. Execute `python3 {VALIDATOR_PATH}` and ensure it outputs 'OK: ...' before completing."
    )



def summarize(messages, elapsed, model_name):
    """Return {"model", "elapsed_s", "subagent_calls", "tool_calls": {name: count}, "tokens": {"input", "output"}}."""
    tool_counter = Counter()
    in_tokens = 0
    out_tokens = 0

    for msg in messages:
        # Check tool calls
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name:
                    tool_counter[name] += 1

        # Check usage metadata
        usage = getattr(msg, "usage_metadata", None)
        if isinstance(usage, dict):
            in_tokens += int(usage.get("input_tokens") or 0)
            out_tokens += int(usage.get("output_tokens") or 0)

    return {
        "model": str(model_name or "unknown"),
        "elapsed_s": round(float(elapsed), 1),
        "subagent_calls": tool_counter.get("task", 0),
        "tool_calls": dict(tool_counter),
        "tokens": {"input": in_tokens, "output": out_tokens},
    }


def save_outputs(backend, topic, messages, elapsed, model_name, reports_dir=REPORTS):
    """Download the report from the sandbox and write the three files into reports_dir. Return the report path."""
    files = download(backend, [REPORT_PATH, SOURCES_PATH])
    report_bytes = files.get(REPORT_PATH)
    sources_bytes = files.get(SOURCES_PATH)

    if not report_bytes or not report_bytes.strip():
        raise RuntimeError(f"Report at {REPORT_PATH} is missing or empty")

    if not sources_bytes or not sources_bytes.strip():
        raise RuntimeError(f"Sources at {SOURCES_PATH} is missing or empty")

    try:
        sources_json = json.loads(sources_bytes.decode("utf-8"))
        if not isinstance(sources_json, list):
            raise ValueError("sources.json must be a JSON list")
    except Exception as exc:
        raise RuntimeError(f"sources.json is invalid JSON: {exc}") from exc

    source_families = sorted({
        str(s.get("source", "")).strip()
        for s in sources_json
        if isinstance(s, dict) and s.get("source")
    })

    slug = slugify(topic)
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "topic": topic,
        **summarize(messages, elapsed, model_name),
        "n_sources": len(sources_json),
        "source_families": source_families,
    }

    md_path = out_dir / f"{slug}.md"
    sources_path = out_dir / f"{slug}.sources.json"
    meta_path = out_dir / f"{slug}.meta.json"

    sources_path.write_bytes(sources_bytes)
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_bytes(report_bytes)

    return md_path


def main(topic):
    """Return the process exit code (0 ok, 1 failed run, 2 no topic)."""
    if not topic or not topic.strip():
        print("Usage: python research.py <topic>", file=sys.stderr)
        return 2

    model = make_model()
    model_name = getattr(model, "model_name", None) or getattr(model, "model", None) or str(model)
    start = time.monotonic()

    with open_sandbox() as backend:
        backend.execute(f"mkdir -p {WORKDIR}/research/notes {WORKDIR}/report")
        upload(
            backend,
            {
                VALIDATOR_PATH: VALIDATOR_SOURCE.read_bytes(),
                FINALIZER_PATH: FINALIZER_SOURCE.read_bytes(),
            },
        )
        agent = build_lead_agent(backend, model)
        try:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": build_prompt(topic)}]},
                config={"recursion_limit": 1000},
            )
        except Exception as exc:
            print(f"FAILED: agent execution crashed: {exc}", file=sys.stderr)
            return 1

        elapsed = time.monotonic() - start
        messages = result.get("messages", []) if isinstance(result, dict) else []

        try:
            saved_path = save_outputs(backend, topic, messages, elapsed, model_name)
        except RuntimeError as err:
            print(f"FAILED: {err}", file=sys.stderr)
            return 1

    print(f"Report saved to {saved_path}")
    return 0


def run_all():
    """Run research pipeline sequentially for all topics in topics.md."""
    topics = re.findall(r"^\d+\. (.+)$", (ROOT / "topics.md").read_text(encoding="utf-8"), re.M)
    print(f"Found {len(topics)} topics in topics.md to research.")
    overall_ok = True
    for idx, topic in enumerate(topics, 1):
        print(f"\n[{idx}/{len(topics)}] Starting research for: '{topic}'")
        code = main(topic)
        if code != 0:
            print(f"FAILED on topic: '{topic}' (exit code {code})", file=sys.stderr)
            overall_ok = False
    return 0 if overall_ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--all" in args:
        sys.exit(run_all())
    topic_arg = " ".join(args).strip()
    sys.exit(main(topic_arg))
