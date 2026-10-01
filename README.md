# dsh-palace-sleep

A read-only **consolidation layer** beside a [MemPalace](https://pypi.org/project/mempalace/) store:
salience scoring, a recall ledger, and a one-line digest injected as DSH runtime
context. It is a layer, not a fork — MemPalace keeps doing ingest and retrieval,
and nothing here patches the installed package.

```
┌─ trigger (this DSH plugin) ─────────────────────────────┐
│  interval + startup check · four tools                   │
│  injects ONE line of runtime context per model step       │
└───────────────┬─────────────────────────────────────────┘
                │
┌─ ledger (sidecar SQLite, ours) ─────────────────────────┐
│  drawer_scores · recall_ledger · archive_marks ·         │
│  digest_snapshots · runs                                 │
└───────────────┬─────────────────────────────────────────┘
                │
┌─ reader ──────┴─────────────────────────────────────────┐
│  python/palace_sleep.py — SQLite `mode=ro`, always       │
└──────────────────────────────────────────────────────────┘
```

## The two guarantees

**1. MemPalace is never written to.** `python/palace_sleep.py` opens
`chroma.sqlite3` with the SQLite `mode=ro` URI. It never imports the `chromadb`
client and never touches the HNSW index. Verified two ways:

- `test/test_palace_sleep.py` asserts the file's size *and* mtime are identical
  across a full read of the palace;
- the audit that produced this design recorded the same before/after signature.

Everything this project produces goes into the sidecar at `~/.dsh/palace-sleep/`.

**2. Existing drawers are immutable.** Scores, recall history and archive marks
are keyed by drawer id in the sidecar. Nothing rewrites stored text — a
consolidation pass that can hallucinate must never be able to corrupt evidence.

## Why not a fork

`mempalace` is pip-installed, ships its own migrations (`migrate.py`,
`repair.py`, `normalize.py`) and its storage carries a `normalize_version`
field — so a vendored patch would have to be re-applied on every upgrade. The
one thing the package does not expose is writing *new metadata keys onto
existing drawers* (`update_drawer` takes only content/wing/room), and that is
exactly what the sidecar is for. A recall ledger belongs outside MemPalace's
semantics anyway.

Should this ever need to write derived memory back, it can do so through the
supported surface — `mempalace_add_drawer`, `mempalace_kg_add` /
`kg_invalidate`, `mempalace_create_tunnel` — with no patching. That is a later
step, gated on the digest proving useful first.

## Injection is one line on purpose

`ctx.systemPrompt.context()` registers a runtime context block that is
**assembled on every model step**. The full digest is on the order of 500
tokens; the line is about 50. So:

| `injectMode` | What every step sees | Cost |
|---|---|---|
| `line` (default) | `Palace 记忆（N 分钟前）：1960 条 · 均分 34.2 · 未解决 198 · 噪声 32% · 最新 105 天前 · 建议先读: …` | ~50 tokens |
| `full` | the whole digest, capped by `digestMaxChars` | ~500+ tokens |
| `off` | nothing; use the tool instead | 0 |

The full digest is always available through `palace_sleep_digest`.

## Tools

| Tool | What it does |
|---|---|
| `palace_sleep_scan` | Re-read the palace (read-only), rescore, rebuild the digest. Writes only to the sidecar. |
| `palace_sleep_digest` | The full digest: statistics, highest-salience memories, unresolved items, open problems. |
| `palace_recall` | Search MemPalace **and record the query in the recall ledger**. Reports the top similarity, because near-zero similarity means retrieval returned unrelated memories. Prefer this over calling `memplc` directly. |
| `palace_sleep_status` | Sidecar state, digest age, and exactly what the runtime context is contributing right now. |

## Configuration

In [cordis.patch.yml](cordis.patch.yml) or a profile override:

```yaml
- id: palace-sleep
  config:
    palaceDb: '~/KnowledgeBase/mempalace_kb/chroma.sqlite3'
    home: '~/.dsh/palace-sleep'
    injectMode: line            # line | full | off
    scanIntervalMinutes: 720    # 12 h; 0 disables the timer
    scanOnStart: true
    contextOrder: 500
    digestMaxChars: 1200
    pythonPath: python
    memplc: memplc
```

The scoring rubric is **not** in the code path you'd have to edit: the first run
writes `~/.dsh/palace-sleep/policy.json`, and every weight, saturation scale and
threshold is read from there.

## Install

```bash
dsh plugin --profile desktop add github:xtk923/dsh-palace-sleep
```

then add `dsh-palace-sleep` to `dsh.profile.bundles` — `plugin_manager
install_bundle` performs both steps. Requires Python 3.9+ on `PATH` (MemPalace
itself is Python, so it already is).

## The observation phase

This version deliberately does **not** write anything back to MemPalace. The
plan is to run it and let the data answer whether consolidation is worth
building at all:

- does the injected line ever change what gets recalled?
- does `recall_ledger` show queries whose top similarity is near zero (i.e.
  retrieval is broken) on a continuing basis?
- do the scored "priority" items match what you would have picked?

Only then is write-back, merging, or archiving worth building.

## Tests

```bash
node --test test/          # plugin surface, config, digest reading, injection
python test/test_palace_sleep.py   # scoring, sidecar, read-only proof
```

Note: `files` in `package.json` keeps `test/` out of the installed artifact —
the tests live in the repository checkout.
