#!/usr/bin/env python3
"""palace_sleep — the read-only consolidation skeleton for a MemPalace store.

Design rules, in order of importance:

  1. **The palace is opened `mode=ro`, always.** This program never writes to
     Chroma, never opens the chromadb client, never touches the HNSW index.
     Everything it produces goes into a sidecar store outside the palace.
  2. **Original drawers are immutable.** Salience, recall history and archive
     marks live in the sidecar keyed by drawer id. Nothing is rewritten.
  3. **No write-back, yet.** This is the observation phase: score, digest,
     measure. Once the digest proves useful, a later version may write derived
     drawers back through MemPalace's own tool surface.

Subcommands:
    scan     read the palace, score every drawer, refresh the sidecar + digest
    digest   print the current digest (from cache)
    status   sidecar statistics
    recall   append one retrieval to the recall ledger (the seed of "was this
             memory ever used?" — the thing no memory system currently tracks)
    init     create the sidecar schema only
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

DEFAULT_PALACE_DB = str(Path.home() / "KnowledgeBase" / "mempalace_kb" / "chroma.sqlite3")
DEFAULT_HOME = str(Path.home() / ".dsh" / "palace-sleep")
SCHEMA_VERSION = 1

# --- default policy -------------------------------------------------------
# The same rubric the audit report proposed, with the exponential saturation it
# was calibrated to after the first draft pinned every top item at 1.0.
DEFAULT_POLICY = {
    "version": "2026-10-01.1",
    "weights": {
        "unresolved": 3.0,
        "decision": 2.5,
        "reusable": 2.0,
        "entities": 1.5,
        "centrality": 1.5,
        "substance": 1.0,
        "recency": 1.0,
        "provenance": 0.5,
    },
    "saturation": {
        "unresolved": 4.0,
        "decision": 4.0,
        "reusable": 3.0,
        "entities": 3.0,
        "centrality": 8.0,
    },
    "recency_half_life_days": 120.0,
    # 0.3 with scale 4 means "two or more problem keywords" — a drawer saying
    # "报错：权限被拒绝" is a risk item. The first value (0.5) required three
    # hits, which silently dropped exactly the short, concrete ones.
    "unresolved_flag_at": 0.3,
    "promote_top_n": 5,
    "unresolved_top_n": 3,
    "exclude_noise_from_digest": True,
}

UNRESOLVED_RE = re.compile(
    r"报错|失败|不可用|踩坑|坑|规避|绕开|workaround|catch|gotcha|error|failed|failure|"
    r"crash|不可逆|注意|警告|fix|修复|bug|regression|回滚|超时|timeout|"
    r"权限|拒绝|冲突|不兼容|遗留|未解决|TODO",
    re.IGNORECASE,
)
DECISION_RE = re.compile(
    r"决定|选择|选型|因为|所以|取舍|权衡|instead of|chose|because|tradeoff|"
    r"配置|参数|环境变量|版本|默认|约定|规范|标准|config|default|convention|"
    r"v?\d+\.\d+(\.\d+)?",
    re.IGNORECASE,
)
REUSABLE_RE = re.compile(r"```|`[^`\n]{3,}`|(?:^|\s)[a-z_][\w-]*\s+--?[\w-]+|\S+[\\/]\S+\.\w{1,5}")
LATIN_TOKEN_RE = re.compile(r"[a-z0-9_][a-z0-9_./:\\-]{2,}")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
HEADING_RE = re.compile(r"(?m)^\s*(?:#{1,6}\s|={3,}|-{3,}\s*$)")

NOISE_SOURCE_RE = re.compile(
    r"(?:[\\/]\.obsidian[\\/]|[\\/]node_modules[\\/]|[\\/]\.git[\\/]|[\\/]\.vscode[\\/]|"
    r"[\\/]AppData[\\/]|[\\/]site-packages[\\/]|[\\/]\.cache[\\/])",
    re.IGNORECASE,
)
CONFIG_BLOB_RE = re.compile(r"^\s*[\{\[]")
CONFIG_KEY_RE = re.compile(r'"\s*:\s*[\[\{"\d]')

ENTITY_STOPWORDS = {
    "create", "use", "add", "text", "node", "all", "file", "files", "project", "path",
    "generated", "true", "false", "null", "name", "type", "value", "data", "example",
    "note", "notes", "test", "code", "page", "list", "item", "items", "new", "get",
    "set", "run", "read", "write", "edit", "find", "search", "index", "source",
    "summary", "content", "output", "input", "config", "default", "python", "json",
    "none", "string", "number", "object", "array", "result",
}

SCHEMA = """
create table if not exists meta (key text primary key, value text);
create table if not exists drawer_scores (
    drawer_id  text primary key,
    score      real not null,
    components text not null,
    text_len   integer,
    wing       text,
    room       text,
    source_file text,
    filed_at   text,
    noise      text,
    unresolved integer default 0,
    scored_at  text,
    policy     text
);
create table if not exists runs (
    id integer primary key autoincrement,
    started_at text, finished_at text,
    drawers integer, mean_score real, noise integer,
    unresolved integer, policy text, notes text
);
create table if not exists digest_snapshots (
    id integer primary key autoincrement,
    created_at text, run_id integer, text text, bytes integer, policy text
);
create table if not exists recall_ledger (
    id integer primary key autoincrement,
    at text, query text, tool text, returned integer,
    drawer_ids text, hits integer, note text
);
create table if not exists archive_marks (
    drawer_id text primary key,
    marked_at text, reason text, reverted_at text
);
create index if not exists idx_scores_score on drawer_scores(score desc);
create index if not exists idx_recall_drawer on recall_ledger(drawer_ids);
"""

# Table counts for `status`. Written as one literal statement per table rather
# than a table name interpolated into the query text: the names are internal
# constants, but a statement built by interpolation is the shape that turns into
# an injection the day someone feeds it a variable.
SIDECAR_COUNTS = (
    ("drawer_scores", "select count(*) from drawer_scores"),
    ("runs", "select count(*) from runs"),
    ("digest_snapshots", "select count(*) from digest_snapshots"),
    ("recall_ledger", "select count(*) from recall_ledger"),
    ("archive_marks", "select count(*) from archive_marks"),
)


# ------------------------------------------------------------------ utils ---
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def extract_json(text: str):
    """The last complete JSON object in `text`, or None.

    Scans for a balanced top-level object rather than trying line by line,
    because the payload is not always on one line: `memplc` is an in-process
    MemPalace wrapper and pretty-prints its results with `indent=2`. A
    line-oriented parser reports that as "no JSON", which then reads as an empty
    search — the exact confusion between a failure and an empty result that this
    project already had to fix once.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    found = None
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            index = start + 1
            continue
        if isinstance(value, dict):
            found = value
        index = end if end > start else start + 1


def parse_time(value):
    if not value:
        return None
    text = str(value).replace("Z", "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return None


def saturating(hits: int, scale: float) -> float:
    return 1.0 - math.exp(-hits / scale) if scale else 0.0


def substance_curve(length: int) -> float:
    if length <= 0:
        return 0.0
    if length < 400:
        return length / 400
    if length <= 2500:
        return 1.0
    return max(0.3, 1.0 - (length - 2500) / 20000)


def tokens_of(text: str) -> set:
    out = set(LATIN_TOKEN_RE.findall(text.lower()))
    for run in CJK_RUN_RE.findall(text):
        out.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            out.add(run)
    return out


def parse_entities(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    text = str(raw).strip()
    if text[:1] in "[{":
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x) for x in data]
            if isinstance(data, dict):
                return [str(k) for k in data]
        except json.JSONDecodeError:
            pass
    return [p.strip() for p in re.split(r"[,;|\n]", text) if p.strip()]


def meaningful_entities(entities) -> list:
    out = []
    for raw in entities:
        ent = str(raw).strip()
        if not ent:
            continue
        if CJK_CHAR_RE.search(ent):
            out.append(ent)
            continue
        if ent.lower() in ENTITY_STOPWORDS or len(ent) < 4:
            continue
        if not re.match(r"^[A-Za-z][\w.+#-]*$", ent):
            continue
        out.append(ent)
    return out


def is_noise(source_file: str, text: str):
    if NOISE_SOURCE_RE.search(source_file or ""):
        return "配置/依赖目录"
    body = (text or "").strip()
    if len(body) > 200 and CONFIG_BLOB_RE.match(body) and len(CONFIG_KEY_RE.findall(body)) > 3:
        return "序列化配置块"
    return None


def preview(text: str, limit: int = 78) -> str:
    match = HEADING_RE.search(text[:800])
    body = text[match.start() :] if match else text
    body = re.sub(r"^[\s=>#-]+", "", body)
    flat = re.sub(r"\s+", " ", body).strip()
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


# -------------------------------------------------------------- sidecar -----
def connect_sidecar(home: Path) -> sqlite3.Connection:
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(home / "sleep.sqlite3"))
    con.executescript(SCHEMA)
    con.execute(
        "insert into meta(key, value) values('schema_version', ?) "
        "on conflict(key) do update set value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return con


def load_policy(home: Path, override: str | None) -> dict:
    path = Path(override) if override else home / "policy.json"
    if override is None and not path.exists():
        home.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_POLICY, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_POLICY))
    if not path.exists():
        raise SystemExit(f"policy file not found: {path}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(policy.get(key), dict):
            policy[key].update(value)
        else:
            policy[key] = value
    return policy


# ---------------------------------------------------------------- palace ----
def read_palace(palace_db: str) -> tuple[list[dict], dict]:
    """Every drawer, read-only. The vector index is never opened."""
    uri = "file:" + palace_db.replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    cur = con.cursor()

    meta: dict[int, dict] = {}
    for rid, key, sval, ival, fval, bval in cur.execute(
        "select id, key, string_value, int_value, float_value, bool_value from embedding_metadata"
    ):
        value = sval if sval is not None else (ival if ival is not None else fval if fval is not None else bval)
        meta.setdefault(rid, {})[key] = value

    identity = {rid: emb for rid, emb in cur.execute("select id, embedding_id from embeddings")}

    drawers = []
    for rid, fields in meta.items():
        drawers.append(
            {
                "id": identity.get(rid, f"rowid:{rid}"),
                "text": fields.get("chroma:document") or "",
                "wing": fields.get("wing") or "?",
                "room": fields.get("room") or "?",
                "source_file": fields.get("source_file") or "",
                "filed_at": fields.get("filed_at"),
                "entities": fields.get("entities"),
            }
        )
    health = {"embeddings": cur.execute("select count(*) from embeddings").fetchone()[0]}
    con.close()
    return drawers, health


def score_drawers(drawers: list[dict], policy: dict, now: datetime) -> None:
    weights = policy["weights"]
    scales = policy["saturation"]
    total_weight = sum(weights.values())

    links: Counter = Counter()
    for d in drawers:
        d["entities_set"] = meaningful_entities(parse_entities(d.get("entities")))
        for ent in set(d["entities_set"]):
            links[ent] += 1

    for d in drawers:
        text = d["text"]
        ents = set(d["entities_set"])
        caps = set(re.findall(r"\b[A-Z][A-Za-z0-9_.-]{2,}\b", text))

        components = {
            "unresolved": saturating(len(UNRESOLVED_RE.findall(text)), scales["unresolved"]),
            "decision": saturating(len(DECISION_RE.findall(text)), scales["decision"]),
            "reusable": saturating(len(REUSABLE_RE.findall(text)), scales["reusable"]),
            "entities": saturating(len(ents) + len(caps), scales["entities"]),
            "centrality": saturating(max((links[e] - 1 for e in ents), default=0), scales["centrality"]),
            "substance": substance_curve(len(text)),
            "recency": 0.0,
            "provenance": 1.0
            if d["source_file"] and re.search(r"\.(md|txt|py|js|ts|json|ya?ml)$", d["source_file"], re.I)
            else 0.0,
        }

        when = parse_time(d.get("filed_at"))
        components["recency"] = (
            0.5 ** (max(0.0, (now - when).total_seconds() / 86400) / policy["recency_half_life_days"])
            if when
            else 0.4
        )

        d["components"] = {k: round(v, 4) for k, v in components.items()}
        d["score"] = round(100.0 * sum(weights[k] * v for k, v in components.items()) / total_weight, 1)
        d["noise"] = is_noise(d["source_file"], text)
        d["unresolved"] = 1 if components["unresolved"] >= policy["unresolved_flag_at"] else 0
        d["tokens"] = tokens_of(text)


def near_duplicate_stats(drawers: list[dict], threshold: float = 0.55) -> tuple[int, int]:
    groups: dict[str, list[dict]] = {}
    for d in drawers:
        groups.setdefault(d["source_file"] or f"<none:{d['wing']}/{d['room']}>", []).append(d)

    clusters = 0
    members = 0
    for source, group in groups.items():
        if len(group) < 2 or len(group) > 400:
            continue
        parent = {d["id"]: d["id"] for d in group}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if not a["tokens"] or not b["tokens"]:
                    continue
                inter = len(a["tokens"] & b["tokens"])
                if inter == 0:
                    continue
                if inter / len(a["tokens"] | b["tokens"]) >= threshold:
                    ra, rb = find(a["id"]), find(b["id"])
                    if ra != rb:
                        parent[rb] = ra
        tally = Counter(find(d["id"]) for d in group)
        for _, count in tally.items():
            if count > 1:
                clusters += 1
                members += count
    return clusters, members


# ---------------------------------------------------------------- digest ----
def build_digest(drawers: list[dict], policy: dict, health: dict, clusters: int,
                 cluster_members: int, now: datetime, policy_version: str) -> tuple[str, str, dict]:
    total = len(drawers)
    noise = [d for d in drawers if d["noise"]]
    noise_share = len(noise) * 100 // max(1, total)
    mean_score = round(sum(d["score"] for d in drawers) / max(1, total), 1)
    # "Newest" must exclude anything written today, or a single write this
    # session makes a dormant palace look alive.
    stamps = [t for t in (parse_time(d["filed_at"]) for d in drawers) if t and (now - t).days >= 1]
    fresh = max(stamps, default=None)
    fresh_days = round((now - fresh).total_seconds() / 86400) if fresh else None

    pool = [d for d in drawers if not (policy["exclude_noise_from_digest"] and d["noise"])]
    seen: set[str] = set()

    def pick(items, count: int) -> list[dict]:
        """Distinct picks only.

        Running the first version showed the same drawer occupying a slot in
        both lists (the top-scoring items are also the unresolved ones) and
        near-duplicate twins each taking one. In a digest this small every line
        costs context, so a pick keyed on the preview prefix is skipped once.
        """
        out = []
        for d in items:
            key = re.sub(r"\W+", "", preview(d["text"], 40))[:32].lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(d)
            if len(out) >= count:
                break
        return out

    top = pick(sorted(pool, key=lambda d: -d["score"]), policy["promote_top_n"])
    risky = pick(sorted((d for d in pool if d["unresolved"]), key=lambda d: -d["score"]),
                 policy["unresolved_top_n"])

    lines = []
    age = f"{fresh_days} 天前" if fresh_days is not None else "未知"
    lines.append(f"## Palace 睡眠摘要（扫描 {now.strftime('%m-%d %H:%M')}）")
    lines.append(
        f"记忆 {total} 条 · 均分 {mean_score} · 未解决 {sum(d['unresolved'] for d in drawers)} 条 · "
        f"噪声 {noise_share}% · 最新一条 {age}"
    )
    if top:
        lines.append("")
        lines.append("**优先回读**")
        for d in top:
            lines.append(f"- [{d['score']:.0f}] {preview(d['text'], 70)}")
    if risky:
        lines.append("")
        lines.append("**未解决 / 风险**")
        for d in risky:
            lines.append(f"- [{d['score']:.0f}] {preview(d['text'], 70)}")
    lines.append("")
    lines.append("**待办（本地账本，未写回 MemPalace）**")
    lines.append(f"- 近重复簇 {clusters} 个 / 涉 {cluster_members} 条，尚未合并")
    lines.append(f"- 噪声 {len(noise)} 条占 {noise_share}%，应在 mining 源头过滤")
    lines.append("- 向量索引 id 漂移导致 wing/room 过滤检索不可用")
    lines.append("- 召回账尚无记录（每次 palace_recall 才开始累积）")

    digest = "\n".join(lines)

    # The one-line form exists because a runtime context block is injected on
    # EVERY model step: the full digest costs on the order of 500 tokens per
    # step, which no ambient reminder is worth. The line is ~50.
    lead = preview(top[0]["text"], 56) if top else "—"
    line = (
        f"Palace {total} 条 · 均分 {mean_score} · 未解决 {sum(d['unresolved'] for d in drawers)} · "
        f"噪声 {noise_share}% · 最新 {age} · 建议先读: {lead}"
    )

    stats = {
        "total": total,
        "mean_score": mean_score,
        "noise": len(noise),
        "noise_share": noise_share,
        "unresolved": sum(d["unresolved"] for d in drawers),
        "clusters": clusters,
        "cluster_members": cluster_members,
        "fresh_days": fresh_days,
        "policy": policy_version,
    }
    return digest, line, stats


# ------------------------------------------------------------ subcommands ---
def cmd_scan(args) -> int:
    home = Path(args.home)
    policy = load_policy(home, args.policy)
    started = now_iso()
    now = datetime.now()

    drawers, health = read_palace(args.palace_db)
    score_drawers(drawers, policy, now)
    clusters, cluster_members = near_duplicate_stats(drawers)
    digest, line, stats = build_digest(drawers, policy, health, clusters, cluster_members, now, policy["version"])

    if args.dry_run:
        print(json.dumps({"dry_run": True, **stats}, ensure_ascii=True))
        return 0

    con = connect_sidecar(home)
    con.executemany(
        "insert into drawer_scores(drawer_id, score, components, text_len, wing, room, source_file,"
        " filed_at, noise, unresolved, scored_at, policy) values(?,?,?,?,?,?,?,?,?,?,?,?)"
        " on conflict(drawer_id) do update set score=excluded.score, components=excluded.components,"
        " text_len=excluded.text_len, wing=excluded.wing, room=excluded.room,"
        " source_file=excluded.source_file, filed_at=excluded.filed_at, noise=excluded.noise,"
        " unresolved=excluded.unresolved, scored_at=excluded.scored_at, policy=excluded.policy",
        [
            (
                d["id"], d["score"], json.dumps(d["components"]), len(d["text"]), d["wing"], d["room"],
                d["source_file"], d["filed_at"], d["noise"], d["unresolved"], now_iso(), policy["version"],
            )
            for d in drawers
        ],
    )
    con.execute(
        "insert into runs(started_at, finished_at, drawers, mean_score, noise, unresolved, policy, notes)"
        " values(?,?,?,?,?,?,?,?)",
        (started, now_iso(), stats["total"], stats["mean_score"], stats["noise"],
         stats["unresolved"], policy["version"], args.note or ""),
    )
    run_id = con.execute("select last_insert_rowid()").fetchone()[0]
    con.execute(
        "insert into digest_snapshots(created_at, run_id, text, bytes, policy) values(?,?,?,?,?)",
        (now_iso(), run_id, digest, len(digest.encode("utf-8")), policy["version"]),
    )
    con.commit()
    con.close()

    (home / "digest.md").write_text(digest, encoding="utf-8")
    (home / "digest.json").write_text(
        json.dumps(
            {"generated_at": now_iso(), "run_id": run_id, "stats": stats, "line": line, "digest": digest},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # ASCII-only summary on stdout: the plugin parses this, and a pipe must not
    # be able to mangle it. The Chinese lives in the files above.
    print(json.dumps({"dry_run": False, "run_id": run_id, "digest_path": str(home / "digest.md"),
                      **stats}, ensure_ascii=True))
    return 0


def cmd_digest(args) -> int:
    path = Path(args.home) / "digest.md"
    if not path.exists():
        print("(no digest yet — run: palace_sleep.py scan)")
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_status(args) -> int:
    home = Path(args.home)
    db = home / "sleep.sqlite3"
    out = {"home": str(home), "sidecar": str(db), "exists": db.exists()}
    if db.exists():
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        for table, statement in SIDECAR_COUNTS:
            try:
                out[table] = con.execute(statement).fetchone()[0]
            except sqlite3.Error as exc:
                out[table] = f"error: {exc}"
        row = con.execute("select finished_at, drawers, mean_score, noise, unresolved from runs order by id desc limit 1").fetchone()
        out["last_run"] = list(row) if row else None
        con.close()
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0


def cmd_recall(args) -> int:
    home = Path(args.home)
    con = connect_sidecar(home)
    con.execute(
        "insert into recall_ledger(at, query, tool, returned, drawer_ids, hits, note) values(?,?,?,?,?,?,?)",
        (now_iso(), args.query, args.tool, args.returned, args.ids or "", args.hits, args.note or ""),
    )
    con.commit()
    total = con.execute("select count(*) from recall_ledger").fetchone()[0]
    con.close()
    print(json.dumps({"logged": True, "ledger_rows": total}, ensure_ascii=True))
    return 0


def cmd_init(args) -> int:
    home = Path(args.home)
    con = connect_sidecar(home)
    policy = load_policy(home, args.policy)
    con.close()
    print(json.dumps({"initialised": str(home), "policy": policy["version"]}, ensure_ascii=True))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="palace_sleep")
    ap.add_argument("--palace-db", default=DEFAULT_PALACE_DB)
    ap.add_argument("--home", default=DEFAULT_HOME)
    ap.add_argument("--policy", default=None)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("digest")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("recall")
    p.add_argument("--query", required=True)
    p.add_argument("--tool", default="palace_recall")
    p.add_argument("--ids", default="")
    p.add_argument("--hits", type=int, default=0)
    p.add_argument("--returned", type=int, default=0)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("init")
    p.set_defaults(func=cmd_init)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
