#!/usr/bin/env python3
"""palace_cli — the writable companion to the read-only sleep layer.

Why this exists, and why it is not a DSH tool
---------------------------------------------
The sleep plugin runs inside a confined DSH session and spawns helpers that
inherit that confinement. ChromaDB's `PersistentClient` validates its tenant
and database on open, so it writes even for a pure query. When the palace lives
outside the session workspace the sandbox denies those writes and every recall
fails with `attempt to write a readonly database`.

Measured, on this machine (2026-10-03):

    write through a link inside the workspace -> target outside   DENIED
    write through a link inside the workspace -> target inside    ALLOWED
    write direct to that outside target                           DENIED

and the same three results when the link was created *unconfined*, by hand.
The sandbox resolves a link to its real target, so no amount of linking makes
an outside palace writable from inside a session. A CLI started in your own
terminal inherits no such confinement, which is the whole reason this file
exists: it is the writable client, not a way to unblock the plugin.

What the plugin still does, unchanged: read-only scan and the digest.
What only this CLI can do: recall against MemPalace and record it truthfully.

Subcommands:
    doctor     report whether the palace can be opened for writing, and exactly why not
    status     sidecar + palace + ledger state, including how many recalls ever succeeded
    link       put a stable link to the palace at <home>/palace (junction/symlink)
    unlink     remove that link; the palace itself is never touched
    mirror     publish a snapshot into <home>/palace for confined analysis
    recall     search MemPalace, print the hits, and record the recall
    sync       re-scan the palace through the link and rebuild the digest
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import palace_sleep as ps  # noqa: E402

DEFAULT_PALACE_DB = ps.DEFAULT_PALACE_DB
DEFAULT_HOME = ps.DEFAULT_HOME
DEFAULT_MEMPLC = "memplc"
LINK_NAME = "palace"
# Recorded in recall_ledger.tool. The plugin writes `palace_recall`; this marks
# the rows that came from a session that could actually reach MemPalace.
CLI_TOOL = "palace_cli"

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_USAGE = 2


# --------------------------------------------------------------------- paths ---
def resolve_palace_dir(palace_db: str, exists=os.path.exists) -> Path:
    """The palace *directory* — what `memplc --palace` and a link both want.

    Accepts either the chroma.sqlite3 file (what the plugin's `palaceDb` is) or
    the directory itself (what `memplc --palace` is), because both spellings
    appear in the wild and guessing wrong is the kind of mistake that reads as
    "palace not found" much later.
    """
    path = Path(palace_db).expanduser()
    if path.is_dir():
        return path
    if exists(str(path)):
        return path.parent
    if path.suffix.lower() in {".sqlite3", ".db", ".sqlite"}:
        return path.parent
    return path


def find_chroma_db(palace_dir: Path):
    """The store inside a palace directory, whatever it ended up being called.

    `chroma.sqlite3` is the convention, not a guarantee, so a plain
    "chroma.sqlite3 is missing" from the obvious directory would be a false
    alarm about a perfectly good palace.
    """
    for name in ("chroma.sqlite3", "chroma.db", "chroma.sqlite"):
        candidate = palace_dir / name
        if candidate.is_file():
            return candidate
    return palace_dir / "chroma.sqlite3"


def resolve_db_file(palace_db: str, palace_dir: Path = None):
    """The store on disk, tolerating the two easy near-misses.

    The plugin's default `palaceDb` is `~/KnowledgeBase/.../chroma.sqlite3`
    while a real deployment often sits at `C:\\Workspace\\KnowledgeBase\\...`.
    Without this, `doctor` on a perfectly healthy palace reports MISSING and
    sends you looking for a problem that does not exist.
    """
    path = Path(palace_db).expanduser()
    if path.is_file():
        return path

    # The path names a database that is not there, but the palace around it is.
    if path.suffix.lower() in {".sqlite3", ".db", ".sqlite"} and path.parent.is_dir():
        found = find_chroma_db(path.parent)
        if found.is_file():
            return found

    # The path names a directory, or the database is expected inside it.
    if path.is_dir():
        return find_chroma_db(path)

    return path


def profile_palace_db(home: Path):
    """The palace path a DSH profile already points this plugin at, if any.

    The plugin's built-in default is a neutral `~/KnowledgeBase/...`, but a real
    deployment overrides it in the profile patch — and that file is the single
    authoritative answer. Reading it beats guessing, and beats telling someone
    their healthy palace is MISSING because they happen to keep it on a
    different drive.
    """
    import re
    dsh_home = home.parent if home.name == "palace-sleep" else Path.home() / ".dsh"
    for path in (dsh_home / "profiles" / "desktop" / "cordis.yml",
                 dsh_home / "profiles" / "desktop" / "cordis.patch.yml"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Only inside a `palace-sleep` entry, and only its own `palaceDb`.
        for block in re.split(r"(?m)^-\s+id:", text)[1:]:
            if block.lstrip().startswith("palace-sleep"):
                match = re.search(r"(?m)^\s+palaceDb:\s*['\"]?([^'\"\r\n]+)['\"]?\s*$", block)
                if match:
                    return Path(match.group(1).strip()).expanduser()
    return None


def discover_palace_db(home: Path, asked_for: Path, workspace_roots=None):
    """Find the palace when the requested path is empty. Returns `(path, why)`.

    Strictly a last resort: it never overrides a path that resolves, and it
    reports exactly which candidate it used, so a surprising answer is visible
    rather than silent.
    """
    from_profile = profile_palace_db(home)
    roots = list(workspace_roots or [])
    if not roots:
        roots = [Path.home(), Path("C:/Workspace"), Path.home() / "Workspace"]
    candidates = []
    if from_profile:
        candidates.append((from_profile, f"the DSH profile names it ({from_profile})"))
    for root in roots:
        candidates.append((root / "KnowledgeBase" / "mempalace_kb",
                           f"found under {root}"))
        candidates.append((root / "mempalace_kb", f"found under {root}"))
    for candidate, why in candidates:
        resolved = resolve_db_file(str(candidate))
        if resolved.is_file():
            return resolved, why
    return asked_for, None


def sidecar_link(home: Path) -> Path:
    return home / LINK_NAME


def read_link(target: Path):
    """`(kind, resolved)` for a link, or `(None, None)` when there is no link."""
    try:
        if not os.path.lexists(str(target)):
            return None, None
    except OSError:
        return None, None
    try:
        if target.is_junction():
            return "junction", Path(os.path.realpath(target))
        if target.is_symlink():
            return "symlink", Path(os.path.realpath(target))
    except OSError:
        return None, None
    return None, None


# ------------------------------------------------------------------- probes ---
def probe_writable(db_file: Path) -> str:
    """Can this database be opened for writing *right now*?

    Asked by attempting it, because that is the only answer that holds: the
    same palace is writable from your terminal and not from a confined session,
    so a path-based guess would be wrong half the time.
    """
    if not db_file.exists():
        return "missing"
    try:
        con = sqlite3.connect(f"file:{db_file.as_posix()}?mode=rw", uri=True, timeout=10)
    except sqlite3.Error as exc:
        return f"denied: {exc}"
    try:
        con.execute("begin immediate")
        con.execute(
            "create table if not exists _palace_cli_probe (k text primary key)"
        )
        con.execute("delete from _palace_cli_probe")
        con.rollback()
        return "writable"
    except sqlite3.Error as exc:
        return f"denied: {exc}"
    finally:
        con.close()


def _run(command: str, args: list[str], timeout: int = 300):
    """Run a helper, never raise, decode leniently.

    MemPalace prints its own failures as JSON on stdout, so a clean exit code is
    not a successful search — callers must inspect the payload, not the status.
    """
    try:
        done = subprocess.run(
            [command, *args],
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
    except FileNotFoundError:
        return None, "", f"{command} not found on PATH"
    except OSError as exc:
        return None, "", str(exc)
    except subprocess.TimeoutExpired:
        return None, "", f"{command} timed out after {timeout}s"
    out = done.stdout.decode("utf-8", errors="replace")
    err = done.stderr.decode("utf-8", errors="replace")
    return done.returncode, out, err


def parse_search_payload(stdout: str, code, stderr: str) -> dict:
    """Turn a `memplc search` run into `{rows, sources, top, error}`.

    Mirrors the plugin's classifier on purpose: an error-shaped payload is a
    retrieval failure, not an empty result. Conflating them is what made a
    broken search look like a schema bug once already.
    """
    parsed = ps.extract_json(stdout)

    if parsed is None:
        return {"rows": [], "sources": [], "similarities": [], "top": None,
                "error": f"memplc exit {code}: {(stderr or '').strip()[-300:]}"}

    error = parsed.get("error") if isinstance(parsed.get("error"), str) else None
    rows = parsed.get("results") if isinstance(parsed.get("results"), list) else []
    sources = [str(r.get("source_file", "")) for r in rows if r.get("source_file")]
    sims = [float(r["similarity"]) for r in rows if isinstance(r.get("similarity"), (int, float))]
    return {
        "rows": rows,
        "sources": sources,
        "similarities": sims,
        "top": max(sims) if sims else None,
        "error": error,
    }


def record_recall(home: Path, query: str, returned: int, ids: list[str], hits: int, note: str) -> int:
    """Append one recall to the ledger. Returns the ledger row count."""
    con = ps.connect_sidecar(home)
    try:
        con.execute(
            "insert into recall_ledger(at, query, tool, returned, drawer_ids, hits, note)"
            " values(?,?,?,?,?,?,?)",
            (ps.now_iso(), query, CLI_TOOL, returned, ",".join(ids), hits, note),
        )
        con.commit()
        return con.execute("select count(*) from recall_ledger").fetchone()[0]
    finally:
        con.close()


# ----------------------------------------------------------------- linking ---
def create_link(link: Path, target: Path, force: bool = False):
    """Point `link` at `target`. Returns `(ok, message)`; never raises.

    A junction on Windows, a symlink elsewhere. The link is a convenience for
    humans and other tools with a stable path — it does not make a palace
    outside a session workspace writable from inside one, which is why
    `doctor` reports writability separately from link state.
    """
    existing_kind, existing_target = read_link(link)
    if existing_kind:
        if existing_target and Path(os.path.realpath(target)) == existing_target:
            return True, f"already linked ({existing_kind}) -> {target}"
        if not force:
            return False, (
                f"{link} already exists as a {existing_kind} to {existing_target}; "
                "pass --force to replace it"
            )
        ok, message = remove_link(link)
        if not ok:
            return False, f"could not replace existing link: {message}"
    elif os.path.lexists(str(link)):
        if not force:
            return False, f"{link} exists and is not a link; pass --force to move it aside"
        try:
            shutil.rmtree(link)
        except OSError as exc:
            return False, f"{link} exists and could not be moved aside: {exc}"

    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                return False, f"mklink /J failed: {detail or 'access denied'}"
            return True, f"junction created: {link} -> {target}"
        os.symlink(str(target), str(link), target_is_directory=True)
        return True, f"symlink created: {link} -> {target}"
    except OSError as exc:
        return False, f"could not create link: {exc}"


def remove_link(link: Path):
    """Remove the link only. `rmdir` on a junction cannot traverse into the target."""
    kind, target = read_link(link)
    if not kind:
        if os.path.lexists(str(link)):
            return False, f"{link} exists but is not a link; refusing to remove it"
        return True, f"nothing to remove at {link}"
    try:
        if kind == "junction" and os.name == "nt":
            result = subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, timeout=30)
            if result.returncode != 0:
                return False, f"rmdir failed: {result.stderr.decode('utf-8', errors='replace').strip()}"
        else:
            link.unlink()
    except OSError as exc:
        return False, f"could not remove link: {exc}"
    return True, f"removed {kind} {link} (target {target} untouched)"


# ----------------------------------------------------------- subcommands -----
def cmd_doctor(args) -> int:
    home = Path(args.home).expanduser()
    asked_for = Path(args.palace_db).expanduser()
    db_file = resolve_db_file(args.palace_db)
    discovered_why = None
    if not db_file.is_file() and not args.no_discover:
        db_file, discovered_why = discover_palace_db(home, db_file)
    palace_dir = db_file.parent
    link = sidecar_link(home)

    writable = probe_writable(db_file)
    kind, link_target = read_link(link)
    memplc_path = shutil.which(args.memplc)
    substituted = db_file != asked_for

    lines = []
    add = lines.append
    add(f"palace db    : {db_file}")
    add(f"palace dir   : {palace_dir}")
    add(f"sidecar home : {home}")
    add(f"link         : {link}")
    add("")
    if discovered_why:
        add(f"[--] nothing at {asked_for}; using this one because {discovered_why}")
        add("     (pass --no-discover to disable, or --palace-db to pin it)")
    elif substituted:
        add(f"[--] {asked_for} is not there; using {db_file.name} in that directory")
    add(f"[{'ok' if db_file.exists() else '!!'}] palace database "
        f"{'found' if db_file.exists() else 'MISSING'}")
    add(f"[{'ok' if writable == 'writable' else '!!'}] writable by this process: {writable}")
    add(f"[{'ok' if kind else '--'}] link: "
        + (f"{kind} -> {link_target}" if kind else "not created (run: palace-cli link)"))
    add(f"[{'ok' if memplc_path else '!!'}] {args.memplc}: "
        + (memplc_path or "NOT on PATH — recall cannot work"))

    if writable == "writable":
        add("")
        add("Recalls from THIS terminal will work and will be recorded.")
    elif writable == "missing":
        add("")
        add("No palace at that path. Point --palace-db at your chroma.sqlite3.")
    else:
        add("")
        add("Recalls from a confined DSH session CANNOT work against this palace.")
        add("That is the sandbox, not the palace: ChromaDB writes its tenant/database")
        add("metadata on open, and the write is denied outside the session workspace.")
        add("A link does not change this — the sandbox resolves links to their real target.")
        add("Run `palace-cli recall ...` here instead, or mirror the palace into a workspace.")

    print("\n".join(lines))
    if not db_file.exists() or memplc_path is None:
        return EXIT_PROBLEM
    return EXIT_OK if writable == "writable" else EXIT_PROBLEM


def cmd_link(args) -> int:
    home = Path(args.home).expanduser()
    palace_dir = resolve_palace_dir(args.palace_db)
    if not palace_dir.is_dir():
        print(f"palace directory not found: {palace_dir}", file=sys.stderr)
        return EXIT_PROBLEM
    link = Path(args.at).expanduser() if args.at else sidecar_link(home)
    ok, message = create_link(link, palace_dir, force=args.force)
    print(message)
    if ok and not args.json:
        db = palace_dir / "chroma.sqlite3"
        if db.exists():
            print(f"writability from this process: {probe_writable(db)}")
        print("note: a link is a convenience path only; a confined session still "
              "cannot write outside its workspace, link or no link.")
    return EXIT_OK if ok else EXIT_PROBLEM


def cmd_unlink(args) -> int:
    home = Path(args.home).expanduser()
    link = Path(args.at).expanduser() if args.at else sidecar_link(home)
    ok, message = remove_link(link)
    print(message)
    return EXIT_OK if ok else EXIT_PROBLEM


def cmd_mirror(args) -> int:
    """Publish a copy of the palace under the sidecar, for confined analysis.

    Deliberately a copy, not a link: a confined reader needs the bytes to live
    inside a workspace, and a link would resolve straight back outside it.
    """
    home = Path(args.home).expanduser()
    source = Path(args.source).expanduser() if args.source else resolve_palace_dir(args.palace_db)
    if not source.is_dir():
        print(f"source palace directory not found: {source}", file=sys.stderr)
        return EXIT_PROBLEM
    dest = Path(args.at).expanduser() if args.at else sidecar_link(home)

    if read_link(dest)[0]:
        print(f"{dest} is a link; run `unlink` first if you want a snapshot there", file=sys.stderr)
        return EXIT_PROBLEM

    files = copied = 0
    try:
        for root, _dirs, names in os.walk(source):
            rel = Path(root).relative_to(source)
            (dest / rel).mkdir(parents=True, exist_ok=True)
            for name in names:
                src = Path(root) / name
                dst = dest / rel / name
                shutil.copy2(src, dst)
                files += 1
                copied += src.stat().st_size
    except OSError as exc:
        print(f"mirror failed after {files} file(s): {exc}", file=sys.stderr)
        return EXIT_PROBLEM

    print(json.dumps({
        "mirrored": str(dest),
        "source": str(source),
        "files": files,
        "bytes": copied,
        "evidence": ps.now_iso(),
        "warning": "snapshot, not a live palace — MemPalace will not see new mining here",
    }, ensure_ascii=True, indent=2))
    return EXIT_OK


def cmd_recall(args) -> int:
    home = Path(args.home).expanduser()
    palace_dir = Path(args.at).expanduser() if args.at else (
        sidecar_link(home) if read_link(sidecar_link(home))[0] else resolve_palace_dir(args.palace_db)
    )

    code, out, err = _run(args.memplc, ["--palace", str(palace_dir), "search", args.query,
                                        "--limit", str(args.limit)])
    if code is None:
        print(f"could not run {args.memplc}: {err}", file=sys.stderr)
        return EXIT_PROBLEM

    answer = parse_search_payload(out, code, err)
    note = (f"top_similarity={answer['top'] if answer['top'] is not None else 'n/a'} "
            f"{len(answer['rows'])} rows")
    if args.note:
        note += f" {args.note}"

    if answer["error"]:
        # A failed recall is still recorded: "I asked and retrieval was broken" is
        # a finding, and hiding it is how a broken index stays broken for months.
        rows = record_recall(home, args.query, 0, [], 0, f"FAILED: {answer['error']} {note}")
        print(f"MemPalace search failed: {answer['error']}", file=sys.stderr)
        print(f"(recorded as a failure in the ledger; ledger now has {rows} row(s))")
        return EXIT_PROBLEM

    ids = [str(r.get("drawer_id") or r.get("id") or "") for r in answer["rows"]]
    ids = [i for i in ids if i]
    rows = record_recall(home, args.query, len(answer["rows"]), ids or answer["sources"],
                         len(answer["rows"]), note)

    if not answer["rows"]:
        print(f"0 result(s) for {args.query!r} (recorded; ledger now has {rows} row(s))")
        return EXIT_OK

    print(f"{len(answer['rows'])} result(s), top similarity "
          f"{answer['top'] if answer['top'] is not None else 'n/a'}")
    if answer["top"] is not None and answer["top"] < 0.3:
        print("  ! that is near-random — retrieval may be returning unrelated memories")
    for row in answer["rows"]:
        source = row.get("source_file") or row.get("drawer_id") or "?"
        sim = row.get("similarity")
        sim_text = f"{sim:.3f}" if isinstance(sim, (int, float)) else "n/a"
        print(f"  [{sim_text}] {source}")
    print(f"(recorded in the ledger; ledger now has {rows} row(s))")
    return EXIT_OK


def cmd_sync(args) -> int:
    home = Path(args.home).expanduser()
    link = sidecar_link(home)
    kind, _ = read_link(link)
    palace_dir = link if kind else resolve_palace_dir(args.palace_db)
    db_file = find_chroma_db(palace_dir)
    if not db_file.exists():
        print(f"no chroma.sqlite3 under {palace_dir}", file=sys.stderr)
        return EXIT_PROBLEM
    argv = ["--home", str(home), "--palace-db", str(db_file)]
    if args.policy:
        argv += ["--policy", args.policy]
    argv += ["scan"]
    if args.note:
        argv += ["--note", args.note]
    return ps.main(argv)


def cmd_status(args) -> int:
    home = Path(args.home).expanduser()
    link = sidecar_link(home)
    kind, target = read_link(link)
    palace_dir = link if kind else resolve_palace_dir(args.palace_db)
    db_file = find_chroma_db(palace_dir) if kind else resolve_db_file(args.palace_db, palace_dir)

    out = {
        "home": str(home),
        "palace_db": str(db_file),
        "palace_dir": str(palace_dir),
        "link": {"kind": kind, "target": str(target) if target else None},
        "writable_now": probe_writable(db_file),
        "memplc": shutil.which(args.memplc),
    }

    sidecar = home / "sleep.sqlite3"
    if sidecar.exists():
        con = sqlite3.connect(f"file:{sidecar.as_posix()}?mode=ro", uri=True)
        try:
            # One literal query per table rather than a name interpolated into a
            # statement: no value ever reaches the SQL text.
            for table, statement in ps.SIDECAR_COUNTS:
                out[table] = con.execute(statement).fetchone()[0]
            # The number this whole project exists to make visible.
            out["recalls_succeeded"] = con.execute(
                "select count(*) from recall_ledger where returned > 0"
            ).fetchone()[0]
            out["recalls_failed"] = con.execute(
                "select count(*) from recall_ledger where returned = 0"
            ).fetchone()[0]
            out["recalls_by_tool"] = dict(con.execute(
                "select tool, count(*) from recall_ledger group by tool"
            ).fetchall())
            row = con.execute(
                "select finished_at, drawers, mean_score, noise, unresolved from runs order by id desc limit 1"
            ).fetchone()
            out["last_run"] = list(row) if row else None
        except sqlite3.Error as exc:
            out["sidecar_error"] = str(exc)
        finally:
            con.close()
    else:
        out["sidecar"] = "missing (run: palace-cli sync)"

    print(json.dumps(out, ensure_ascii=True, indent=2))
    return EXIT_OK


# -------------------------------------------------------------------- main ---
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="palace-cli",
        description="Writable companion to the read-only palace-sleep layer.",
    )
    ap.add_argument("--palace-db", default=DEFAULT_PALACE_DB,
                    help="path to chroma.sqlite3 (or the palace directory)")
    ap.add_argument("--home", default=DEFAULT_HOME, help="sidecar directory")
    ap.add_argument("--memplc", default=DEFAULT_MEMPLC, help="memplc executable")
    ap.add_argument("--policy", default=None, help="policy override for sync")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="explain writability and link state")
    p.add_argument("--no-discover", action="store_true",
                   help="do not look in the DSH profile or usual roots when the path is empty")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="sidecar, palace and ledger state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("link", help="publish a stable link to the palace")
    p.add_argument("--at", default=None, help="link path (default <home>/palace)")
    p.add_argument("--force", action="store_true", help="replace an existing link")
    p.add_argument("--json", action="store_true", help="suppress the extra notes")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="remove the link, never the palace")
    p.add_argument("--at", default=None)
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("mirror", help="copy the palace into the sidecar for confined analysis")
    p.add_argument("--source", default=None, help="palace directory to copy")
    p.add_argument("--at", default=None, help="destination (default <home>/palace)")
    p.set_defaults(func=cmd_mirror)

    p = sub.add_parser("recall", help="search MemPalace and record the recall")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--at", default=None, help="palace directory to search (default: the link)")
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("sync", help="re-scan through the link and rebuild the digest")
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_sync)

    return ap


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse exits on bad usage; return the code so an embedding caller
        # sees a value instead of an escape.
        return int(exc.code or EXIT_USAGE)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
