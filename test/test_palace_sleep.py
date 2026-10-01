#!/usr/bin/env python3
"""Tests for the scan helper. Run directly: python test/test_palace_sleep.py"""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
import palace_sleep as ps  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"ok   {label}")
    else:
        print(f"FAIL {label} {detail}")
        FAILURES.append(label)


def test_saturation():
    check("saturating is bounded", 0.0 <= ps.saturating(0, 4) < 0.01)
    check("saturating rises", ps.saturating(3, 4) > ps.saturating(1, 4) > 0)
    check("saturating is bounded by 1", 0.0 <= ps.saturating(10_000, 4) <= 1.0, ps.saturating(10_000, 4))
    # The first draft used min(1, hits/3), which pinned the top of the ranking
    # at exactly 1.0 and destroyed discrimination there.
    check("saturating still discriminates at 8 hits", ps.saturating(8, 4) < 0.95, ps.saturating(8, 4))


def test_substance_curve():
    check("empty scores zero", ps.substance_curve(0) == 0.0)
    check("mid length is full", ps.substance_curve(1200) == 1.0)
    check("a raw dump is discounted", ps.substance_curve(50_000) < 0.5)


def test_noise_classifier():
    check("obsidian config path is noise", ps.is_noise(r"C:\V\.obsidian\plugins\x.json", "{}") is not None)
    check("node_modules is noise", ps.is_noise("/p/node_modules/x/index.js", "code") is not None)
    check("a normal note is not noise", ps.is_noise(r"C:\V\notes.md", "# A heading\n\nsome prose") is None)
    check(
        "a serialised config blob is noise",
        ps.is_noise("notes.md", '{\n' + ',\n'.join(f'  "key_{i}": [{i}]' for i in range(20)) + '\n}') is not None,
    )
    check(
        "a short json snippet is not noise",
        ps.is_noise("notes.md", '{"a": 1, "b": [2]}') is None,
    )


def test_entity_filter():
    kept = ps.meaningful_entities(["Obsidian", "Create", "Use", "Excalidraw", "ab", "Canvas"])
    check("stopwords dropped", "Create" not in kept and "Use" not in kept, kept)
    check("short latin dropped", "ab" not in kept)
    check("proper nouns kept", {"Obsidian", "Excalidraw", "Canvas"} <= set(kept), kept)
    check("cjk kept", ps.meaningful_entities(["知识库"]) == ["知识库"])


def test_preview_anchors_on_a_heading():
    body = "mid sentence garbage\n\n# Real Heading\n\ncontent follows here"
    check("preview starts at the heading", ps.preview(body, 40).startswith("Real Heading"), ps.preview(body, 40))


def test_sidecar_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        con = ps.connect_sidecar(home)
        con.execute(
            "insert into drawer_scores(drawer_id, score, components, scored_at, policy)"
            " values('d1', 42.0, '{}', '2026-10-01T00:00:00', 'test')"
        )
        con.commit()
        row = con.execute("select score from drawer_scores where drawer_id='d1'").fetchone()
        con.close()
        check("sidecar persists a score", row is not None and row[0] == 42.0)
        check("sidecar file exists", (home / "sleep.sqlite3").exists())
        check("schema version recorded", json.loads(json.dumps({"v": ps.SCHEMA_VERSION}))["v"] == 1)


def test_policy_defaults_written_and_merged():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        policy = ps.load_policy(home, None)
        check("policy file created", (home / "policy.json").exists())
        check("noise excluded from digest by default", policy["exclude_noise_from_digest"] is True)
        custom = home / "custom.json"
        custom.write_text(json.dumps({"weights": {"unresolved": 9.0}}), encoding="utf-8")
        merged = ps.load_policy(home, str(custom))
        check("override merges into defaults", merged["weights"]["unresolved"] == 9.0)
        check("untouched keys survive", merged["weights"]["decision"] == ps.DEFAULT_POLICY["weights"]["decision"])


def test_palace_read_is_read_only():
    db = ps.DEFAULT_PALACE_DB
    if not os.path.exists(db):
        print("skip real-palace read-only check (palace not present)")
        return
    before = os.stat(db)
    drawers, health = ps.read_palace(db)
    after = os.stat(db)
    check("palace opened read-only did not change size", before.st_size == after.st_size)
    check("palace opened read-only did not change mtime", before.st_mtime == after.st_mtime)
    check("drawers were read", len(drawers) > 0, len(drawers))
    check("every drawer carries an id", all(d["id"] for d in drawers))
    check("embedding count matches", health["embeddings"] == len(drawers), (health, len(drawers)))


def test_scoring_is_deterministic():
    drawers = [
        {"id": "a", "text": "# 报错: 修复方案\n\n决定使用 v1.2.3，因为更稳。\n```bash\nfoo --bar\n```",
         "wing": "w", "room": "r", "source_file": "notes.md", "filed_at": "2026-09-01T00:00:00", "entities": None},
        {"id": "b", "text": "short", "wing": "w", "room": "r", "source_file": "x.txt",
         "filed_at": "2026-09-01T00:00:00", "entities": None},
    ]
    policy = ps.DEFAULT_POLICY
    now = datetime(2026, 10, 1)
    ps.score_drawers(drawers, policy, now)
    first = [d["score"] for d in drawers]
    ps.score_drawers(drawers, policy, now)
    check("scores are deterministic", first == [d["score"] for d in drawers], first)
    check("the substantive drawer outranks the stub", drawers[0]["score"] > drawers[1]["score"], first)
    check("components are recorded", set(drawers[0]["components"]) >= {"unresolved", "decision", "recency"})
    check("unresolved flag set", drawers[0]["unresolved"] == 1)


def test_digest_lists_are_disjoint():
    policy = json.loads(json.dumps(ps.DEFAULT_POLICY))
    now = datetime(2026, 10, 1)
    drawers = []
    for index in range(6):
        drawers.append(
            {
                "id": f"d{index}",
                "text": f"# 报错与修复 {index}\n\n决定: 选择方案 {index}，因为稳定。\n```bash\ncmd --flag {index}\n```",
                "wing": "w", "room": "r", "source_file": f"f{index}.md",
                "filed_at": "2026-09-01T00:00:00", "entities": None,
            }
        )
    ps.score_drawers(drawers, policy, now)
    digest, line, stats = ps.build_digest(drawers, policy, {}, 0, 0, now, policy["version"])
    top_block = digest.split("**未解决 / 风险**")[0].split("**优先回读**")[1]
    if "**未解决 / 风险**" in digest:
        risky_block = digest.split("**未解决 / 风险**")[1].split("**待办")[0]
    else:
        risky_block = ""
    top_lines = [l for l in top_block.splitlines() if l.startswith("- ")]
    risky_lines = [l for l in risky_block.splitlines() if l.startswith("- ")]
    check("digest has priority picks", len(top_lines) == policy["promote_top_n"], top_lines)
    check("the two lists do not overlap", not (set(top_lines) & set(risky_lines)), (top_lines, risky_lines))
    check("the one-line form is short", len(line) < 200, len(line))
    check("stats carry the drawer count", stats["total"] == 6)


if __name__ == "__main__":
    for fn in (
        test_saturation,
        test_substance_curve,
        test_noise_classifier,
        test_entity_filter,
        test_preview_anchors_on_a_heading,
        test_sidecar_roundtrip,
        test_policy_defaults_written_and_merged,
        test_palace_read_is_read_only,
        test_scoring_is_deterministic,
        test_digest_lists_are_disjoint,
    ):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        raise SystemExit(1)
    print("all python checks passed")
