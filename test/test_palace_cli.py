#!/usr/bin/env python3
"""Tests for palace_cli. Run directly: python test/test_palace_cli.py

Everything is hermetic: a fake palace is built under this repository instead of
the real one, and no test spawns `memplc`, because a test suite that mutates a
live knowledge base is not a test suite.

The working directory is deliberately not `tempfile`: under the DSH sandbox a
temporary directory cannot be re-permissioned for cleanup, which would abort the
run at the first test and hide every later one.
"""

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
import palace_cli as pc  # noqa: E402
import palace_sleep as ps  # noqa: E402

PROBE = ROOT / ".cli-probe"
FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"ok   {label}")
    else:
        print(f"FAIL {label} {detail}")
        FAILURES.append(label)


def make_palace(path: Path) -> Path:
    """A minimal but real chroma.sqlite3: enough for a writability probe."""
    path.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path / "chroma.sqlite3"))
    con.execute("create table if not exists collections (id text primary key, name text)")
    con.commit()
    con.close()
    return path


# ------------------------------------------------------------------ paths -----
def test_resolve_db_file_tolerates_near_misses():
    """The failure this guards: a healthy palace reported as MISSING.

    The plugin's built-in default points at `~/KnowledgeBase/...`, while the
    same palace often actually lives on another root. Reporting MISSING sends
    you hunting for a problem that does not exist.
    """
    palace = make_palace(PROBE / "near" / "mempalace_kb")
    # The layout the default expects, one directory tree over.
    elsewhere = PROBE / "elsewhere" / "mempalace_kb"
    elsewhere.mkdir(parents=True, exist_ok=True)

    asked = elsewhere / "chroma.sqlite3"
    check("a missing chroma.sqlite3 in an existing palace dir is redirected",
          pc.resolve_db_file(str(asked)) == asked, pc.resolve_db_file(str(asked)))

    # Same directory, but the store is named differently.
    renamed = make_palace(PROBE / "renamed")
    (renamed / "chroma.sqlite3").rename(renamed / "chroma.db")
    check("a differently named store in that directory is found",
          pc.resolve_db_file(str(renamed / "chroma.sqlite3")) == renamed / "chroma.db",
          pc.resolve_db_file(str(renamed / "chroma.sqlite3")))

    # A directory argument yields the store inside it.
    check("a directory argument resolves to the store inside",
          pc.resolve_db_file(str(palace)) == palace / "chroma.sqlite3")

    # A path that exists is never overridden.
    real = palace / "chroma.sqlite3"
    check("an existing store is used as given", pc.resolve_db_file(str(real)) == real)
    check("an empty directory still resolves to the conventional name",
          pc.resolve_db_file(str(elsewhere)) == elsewhere / "chroma.sqlite3")


def test_profile_lookup_and_discovery():
    home = PROBE / "dshlike" / "palace-sleep"
    profile = home.parent / "profiles" / "desktop"
    profile.mkdir(parents=True, exist_ok=True)
    palace = make_palace(PROBE / "profile-palace" / "mempalace_kb")

    (profile / "cordis.yml").write_text(
        "- id: other-plugin\n"
        "  config:\n"
        "    palaceDb: C:\\should\\not\\win\n"
        "- id: palace-sleep\n"
        "  name: dsh-palace-sleep\n"
        "  config:\n"
        f"    palaceDb: {palace / 'chroma.sqlite3'}\n"
        "    home: ~/.dsh/palace-sleep\n",
        encoding="utf-8",
    )

    found = pc.profile_palace_db(home)
    check("the profile's palaceDb is read", found == palace / "chroma.sqlite3", found)
    check("another plugin's palaceDb is not used", found != Path("C:/should/not/win"), found)

    resolved, why = pc.discover_palace_db(home, PROBE / "nope" / "chroma.sqlite3")
    check("discovery follows the profile", resolved == palace / "chroma.sqlite3", resolved)
    check("discovery explains itself", why and "profile" in why, why)

    # With nothing to find, discovery must not invent an answer. Path.home is
    # redirected because the lookup falls back to the real ~/.dsh, and a test
    # that reads the machine's own profile is not testing the code.
    import pathlib
    empty_home = PROBE / "empty-dshlike"
    real_home = pathlib.Path.home
    pathlib.Path.home = classmethod(lambda cls: PROBE / "no-such-user-home")
    try:
        resolved, why = pc.discover_palace_db(empty_home, PROBE / "still-nope" / "chroma.sqlite3",
                                              workspace_roots=[PROBE / "no-such-root"])
    finally:
        pathlib.Path.home = real_home
    check("discovery returns the original path when nothing is found",
          resolved == PROBE / "still-nope" / "chroma.sqlite3", resolved)
    check("discovery gives no reason when nothing is found", why is None, why)


def test_resolve_palace_dir():
    db = PROBE / "rpd" / "chroma.sqlite3"
    make_palace(db.parent)
    check("a sqlite3 file resolves to its directory",
          pc.resolve_palace_dir(str(db)) == db.parent, pc.resolve_palace_dir(str(db)))
    check("a directory resolves to itself",
          pc.resolve_palace_dir(str(db.parent)) == db.parent)
    # A path that does not exist yet must still resolve by shape, not by stat.
    missing = PROBE / "nowhere" / "chroma.sqlite3"
    check("a missing chroma.sqlite3 resolves to its parent",
          pc.resolve_palace_dir(str(missing)) == missing.parent)
    check("a missing extensionless path stays itself",
          pc.resolve_palace_dir(str(PROBE / "some-dir")) == PROBE / "some-dir")


# ----------------------------------------------------------- writability ------
def test_probe_writable():
    db = make_palace(PROBE / "writable") / "chroma.sqlite3"
    check("a writable database is reported writable", pc.probe_writable(db) == "writable", pc.probe_writable(db))
    check("a missing database is reported missing",
          pc.probe_writable(PROBE / "no-such" / "chroma.sqlite3") == "missing")

    # A genuinely read-only file must be reported as denied, not assumed.
    ro = make_palace(PROBE / "readonly") / "chroma.sqlite3"
    if os.name != "nt":
        os.chmod(ro, 0o444)
    else:
        # On Windows the read-only attribute is the honest equivalent.
        import subprocess
        subprocess.run(["attrib", "+R", str(ro)], capture_output=True)
    try:
        result = pc.probe_writable(ro)
        if os.name == "nt":
            check("a read-only file is not reported writable", result != "writable", result)
        else:
            check("a read-only file is reported denied", result.startswith("denied"), result)
    finally:
        # Clear the attribute again, or the probe directory cannot be removed.
        if os.name == "nt":
            subprocess.run(["attrib", "-R", str(ro)], capture_output=True)
        else:
            os.chmod(ro, 0o644)

    # The probe must not leave anything behind.
    con = sqlite3.connect(str(db))
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    con.close()
    check("the writability probe leaves no table behind",
          "_palace_cli_probe" not in tables, tables)


# ---------------------------------------------------------------- links -------
def test_link_lifecycle():
    palace = make_palace(PROBE / "linked-palace")
    home = PROBE / "linked-home"
    link = home / "palace"

    check("no link is reported before one exists", pc.read_link(link) == (None, None))

    ok, message = pc.create_link(link, palace)
    check("creating a link succeeds", ok, message)
    kind, target = pc.read_link(link)
    check("the link is detected", kind is not None, (kind, target))
    check("the link resolves to the real palace", target == palace.resolve(), (target, palace.resolve()))
    check("the palace is reachable through the link", (link / "chroma.sqlite3").exists())

    # Idempotent: pointing at the same place again is not an error.
    ok, message = pc.create_link(link, palace)
    check("relinking to the same target is idempotent", ok, message)
    check("the link now says so", "already linked" in message, message)

    # Refuses to clobber a different target without --force.
    other = make_palace(PROBE / "other-palace")
    ok, message = pc.create_link(link, other)
    check("refuses to retarget without --force", not ok, message)
    ok, message = pc.create_link(link, other, force=True)
    check("retargets with --force", ok, message)
    check("the link now points at the new target", pc.read_link(link)[1] == other.resolve())

    # Removal must never touch the target.
    ok, message = pc.remove_link(link)
    check("removing the link succeeds", ok, message)
    check("the link is gone", pc.read_link(link) == (None, None))
    check("the palace survives link removal", (palace / "chroma.sqlite3").exists())
    check("the other palace survives too", (other / "chroma.sqlite3").exists())
    check("removing a non-existent link is a no-op", pc.remove_link(link)[0])

    # A real directory must never be silently deleted.
    real = home / "not-a-link"
    real.mkdir(parents=True, exist_ok=True)
    ok, message = pc.remove_link(real)
    check("refuses to remove a real directory", not ok, message)
    check("the real directory is still there", real.is_dir())
    ok, message = pc.create_link(real, palace)
    check("refuses to clobber a real directory without --force", not ok, message)


# -------------------------------------------------------------- recall --------
def test_parse_search_payload():
    ok_payload = json.dumps({
        "query": "q",
        "results": [
            {"source_file": "a.md", "similarity": 0.42, "drawer_id": "d1"},
            {"source_file": "b.md", "similarity": 0.11, "drawer_id": "d2"},
        ],
    })
    answer = pc.parse_search_payload(ok_payload + "\n", 0, "")
    check("rows are parsed", len(answer["rows"]) == 2, answer)
    check("the top similarity is the maximum", answer["top"] == 0.42, answer["top"])
    check("sources are collected", answer["sources"] == ["a.md", "b.md"], answer["sources"])
    check("a good payload is not an error", answer["error"] is None, answer["error"])

    # Exactly what the live store returns when the index cannot be reached.
    failed = pc.parse_search_payload(
        json.dumps({"error": "Search error: Internal error: Error finding id"}), 0, "")
    check("an error-shaped payload is a failure", failed["error"] is not None, failed)
    check("an error-shaped payload yields no rows", failed["rows"] == [], failed)

    crashed = pc.parse_search_payload("", 2, "boom")
    check("a crash is reported", "exit 2" in (crashed["error"] or ""), crashed["error"])

    empty = pc.parse_search_payload(json.dumps({"results": []}), 0, "")
    check("an empty result is not an error", empty["error"] is None and empty["rows"] == [])

    # Noisy stdout: the payload is still found on its own line.
    noisy = "Embedding function initialized\n" + ok_payload + "\ntrailing noise\n"
    check("the JSON payload is found among noise",
          len(pc.parse_search_payload(noisy, 0, "")["rows"]) == 2)

    # The real shape. `memplc` pretty-prints with indent=2, so a line-oriented
    # parser sees no JSON at all and calls a successful search an empty one.
    pretty = json.dumps({
        "query": "Obsidian callout",
        "filters": {"wing": None, "room": None},
        "total_before_filter": 2,
        "results": [
            {"source_file": "CALLOUTS.md", "similarity": 0.0, "wing": "personal-tools",
             "closet_preview": 'a|b|"quoted"|{braces}', "text": "{\n  \"id\": \"x\"\n}"},
            {"source_file": "workflow.md", "similarity": 0.025, "wing": "personal-tools"},
        ],
    }, indent=2)
    multiline = pc.parse_search_payload(pretty, 0, "")
    check("a pretty-printed payload is parsed", len(multiline["rows"]) == 2, multiline)
    check("a pretty-printed payload is not an error", multiline["error"] is None, multiline["error"])
    check("the top similarity survives pretty-printing", multiline["top"] == 0.025, multiline["top"])
    check("sources survive pretty-printing",
          multiline["sources"] == ["CALLOUTS.md", "workflow.md"], multiline["sources"])
    check("a zero similarity is not mistaken for absent", multiline["top"] == 0.025)


def test_extract_json():
    check("empty text has no payload", ps.extract_json("") is None)
    check("text with no object has no payload", ps.extract_json("just noise") is None)
    check("a compact object is found", ps.extract_json('noise {"a": 1} tail') == {"a": 1})
    check("a pretty object is found", ps.extract_json('x\n{\n "a": 1\n}\ny') == {"a": 1})
    check("the LAST object wins",
          ps.extract_json('{"a": 1}\n{"b": 2}') == {"b": 2})
    check("braces inside strings do not confuse it",
          ps.extract_json('{"text": "a } b { c", "n": 2}') == {"text": "a } b { c", "n": 2})
    check("escaped quotes do not confuse it",
          ps.extract_json(r'{"text": "say \"hi\" }", "n": 1}') == {"text": 'say "hi" }', "n": 1})
    check("an unterminated object yields nothing", ps.extract_json('{"a": 1') is None)
    check("a broken object before a good one still finds the good one",
          ps.extract_json('{oops}\n{"a": 1}') == {"a": 1})
    check("nested objects come back whole",
          ps.extract_json('{"a": {"b": {"c": 1}}, "d": 2}') == {"a": {"b": {"c": 1}}, "d": 2})


def test_record_recall_is_the_ledger():
    home = PROBE / "recall-home"
    rows = pc.record_recall(home, "how do I X", 2, ["d1", "d2"], 2, "top_similarity=0.42 2 rows")
    check("a recall appends one ledger row", rows == 1, rows)
    con = sqlite3.connect(str(home / "sleep.sqlite3"))
    at, query, tool, returned, ids = con.execute(
        "select at, query, tool, returned, drawer_ids from recall_ledger").fetchone()
    con.close()
    check("the query is stored", query == "how do I X", query)
    check("the recall is attributed to the CLI, not the plugin", tool == pc.CLI_TOOL, tool)
    check("the row count is stored", returned == 2, returned)
    check("the drawer ids are stored", ids == "d1,d2", ids)
    check("the timestamp is an ISO string", "T" in at, at)

    rows = pc.record_recall(home, "second", 0, [], 0, "FAILED: something")
    check("a failure can be recorded too", rows == 2, rows)


def test_recall_command_without_memplc_does_not_touch_the_ledger():
    home = PROBE / "recall-home-missing"
    pc.record_recall(home, "seed", 1, ["d"], 1, "seed")
    code = pc.main([
        "--home", str(home),
        "--memplc", "definitely-not-a-real-executable-xyz",
        "recall", "anything",
    ])
    check("a missing memplc is a problem exit", code == pc.EXIT_PROBLEM, code)
    con = sqlite3.connect(f"file:{(home / 'sleep.sqlite3').as_posix()}?mode=ro", uri=True)
    count = con.execute("select count(*) from recall_ledger").fetchone()[0]
    con.close()
    check("a missing memplc adds no ledger row", count == 1, count)


# ------------------------------------------------------------- mirror ---------
def test_mirror_copies_bytes():
    source = make_palace(PROBE / "mirror-source")
    (source / "segment-a").mkdir(parents=True, exist_ok=True)
    (source / "segment-a" / "data_level0.bin").write_bytes(b"\x00\x01\x02")
    home = PROBE / "mirror-home"

    code = pc.main(["--home", str(home), "mirror", "--source", str(source)])
    check("mirror exits cleanly", code == pc.EXIT_OK, code)
    dest = home / "palace"
    check("the mirrored database is a real file, not a link",
          (dest / "chroma.sqlite3").is_file() and pc.read_link(dest)[0] is None)
    check("nested segment data is copied",
          (dest / "segment-a" / "data_level0.bin").read_bytes() == b"\x00\x01\x02")
    check("the source is untouched", (source / "chroma.sqlite3").exists())

    code = pc.main(["--home", str(home), "mirror", "--source", str(PROBE / "nope")])
    check("mirroring a missing source is a problem exit", code == pc.EXIT_PROBLEM, code)


# ------------------------------------------------------------- doctor ---------
def test_doctor_and_status_report_the_truth():
    palace = make_palace(PROBE / "doctor-palace")
    home = PROBE / "doctor-home"
    code = pc.main(["--palace-db", str(palace / "chroma.sqlite3"), "--home", str(home),
                    "--memplc", "definitely-not-a-real-executable-xyz", "doctor"])
    check("doctor exits non-zero when memplc is absent", code == pc.EXIT_PROBLEM, code)

    code = pc.main(["--palace-db", str(palace / "chroma.sqlite3"), "--home", str(home),
                    "--memplc", sys.executable, "doctor"])
    check("doctor exits zero for a writable palace with a memplc", code == pc.EXIT_OK, code)

    pc.main(["--palace-db", str(palace / "chroma.sqlite3"), "--home", str(home), "link"])
    code = pc.main(["--palace-db", str(palace / "chroma.sqlite3"), "--home", str(home),
                    "--memplc", sys.executable, "status"])
    check("status exits cleanly", code == pc.EXIT_OK, code)
    # `status` reports, it does not provision: a reader must not create state as a
    # side effect just because it was asked a question.
    check("status does not conjure a sidecar", not (home / "sleep.sqlite3").exists())
    check("bad usage returns a code rather than raising",
          pc.main(["--palace-db", str(palace), "--home", str(home), "--nonsense"]) == pc.EXIT_USAGE)


def test_status_counts_successful_recalls():
    home = PROBE / "status-home"
    pc.record_recall(home, "worked", 3, ["a"], 3, "ok")
    pc.record_recall(home, "broke", 0, [], 0, "FAILED: nope")
    pc.record_recall(home, "broke again", 0, [], 0, "FAILED: nope")
    con = sqlite3.connect(f"file:{(home / 'sleep.sqlite3').as_posix()}?mode=ro", uri=True)
    succeeded = con.execute("select count(*) from recall_ledger where returned > 0").fetchone()[0]
    failed = con.execute("select count(*) from recall_ledger where returned = 0").fetchone()[0]
    con.close()
    check("successful recalls are counted", succeeded == 1, succeeded)
    check("failed recalls are counted separately", failed == 2, failed)


if __name__ == "__main__":
    if PROBE.exists():
        shutil.rmtree(PROBE, ignore_errors=True)
    PROBE.mkdir(parents=True, exist_ok=True)
    try:
        for fn in (
            test_resolve_palace_dir,
            test_resolve_db_file_tolerates_near_misses,
            test_profile_lookup_and_discovery,
            test_probe_writable,
            test_link_lifecycle,
            test_parse_search_payload,
            test_extract_json,
            test_record_recall_is_the_ledger,
            test_recall_command_without_memplc_does_not_touch_the_ledger,
            test_mirror_copies_bytes,
            test_doctor_and_status_report_the_truth,
            test_status_counts_successful_recalls,
        ):
            fn()
    finally:
        shutil.rmtree(PROBE, ignore_errors=True)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        raise SystemExit(1)
    print("all palace_cli checks passed")
