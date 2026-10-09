"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import re
import sys

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"

_GROUP = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\](?!\()")   # [3]  [1, 2]  [1-3]  [2-3]; not [3](link)
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_REF_HEADING = re.compile(r"(?m)^##[ \t]+References[ \t]*$")
_REF_LINE = re.compile(r"^\[(\d+)\]")
_URL = re.compile(r"https?://\S+")


def _group_numbers(group):
    numbers = []
    for part in re.split(r"\s*,\s*", group):
        span = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if span:
            a, b = int(span.group(1)), int(span.group(2))
            numbers.extend(range(a, b + 1) if 0 <= b - a <= 200 else [a, b])
        else:
            try:
                numbers.append(int(part))
            except ValueError:
                pass
    return numbers


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK)."""
    problems = []
    if not sources or not isinstance(sources, list):
        return ["no sources in sources.json"]

    sources_by_n = {}
    seen_urls = {}

    for entry in sources:
        if not isinstance(entry, dict):
            problems.append(f"source entry is not an object: {entry!r}")
            continue
        n = entry.get("n")
        if not isinstance(n, int):
            problems.append(f"source entry has non-integer n: {n!r}")
        elif n in sources_by_n:
            problems.append(f"duplicate source number in sources.json: [{n}]")
        else:
            sources_by_n[n] = entry

        url = entry.get("url")
        if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
            problems.append(f"source [{n}] url must start with http:// or https://: {url!r}")
        else:
            if url in seen_urls:
                problems.append(f"duplicate url in sources.json: {url}")
            else:
                seen_urls[url] = n

    matches = list(_REF_HEADING.finditer(report_text))
    if not matches:
        problems.append("missing '## References' section")
        body = report_text
        ref_section = ""
    else:
        last_match = matches[-1]
        body = report_text[:last_match.start()]
        ref_section = report_text[last_match.end():]

    # Extract [n] from body only (ignoring code blocks and markdown links)
    segments = _CODE.split(body)
    cited = set()
    for i, segment in enumerate(segments):
        if i % 2:
            continue
        for match in _GROUP.finditer(segment):
            for num in _group_numbers(match.group(1)):
                cited.add(num)

    for num in sorted(cited):
        if num not in sources_by_n:
            problems.append(f"[{num}] cited but missing from sources.json")

    for n in sorted(sources_by_n):
        if n not in cited:
            problems.append(f"source [{n}] never cited")

    # Validate References section
    ref_lines_by_n = {}
    for line in ref_section.splitlines():
        line_str = line.strip()
        match = _REF_LINE.match(line_str)
        if not match:
            continue
        num = int(match.group(1))
        if num not in ref_lines_by_n:
            ref_lines_by_n[num] = []
        ref_lines_by_n[num].append(line_str)

    for num, lines in sorted(ref_lines_by_n.items()):
        if num not in sources_by_n:
            problems.append(f"reference [{num}] is not in sources.json")
        if len(lines) > 1:
            problems.append(f"multiple reference lines for [{num}] in References")

    for n in sorted(sources_by_n):
        if n not in ref_lines_by_n:
            problems.append(f"source [{n}] has no reference line in References")
        else:
            for line in ref_lines_by_n[n]:
                urls = _URL.findall(line)
                if len(urls) == 0:
                    problems.append(f"reference line [{n}] has no http(s) URL")
                elif len(urls) > 1:
                    problems.append(f"reference line [{n}] has multiple URLs (bundled sources)")
                else:
                    url_found = urls[0].rstrip(".,;)>'\"]")
                    expected_url = str(sources_by_n[n].get("url") or "").rstrip(".,;)>'\"]")
                    if url_found != expected_url:
                        problems.append(
                            f"reference line [{n}] URL does not match sources.json ({url_found} != {expected_url})"
                        )

    return problems


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
