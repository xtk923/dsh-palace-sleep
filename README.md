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
                │                        ▲
┌─ reader ──────┴──────────────────────┐ │ ┌─ palace-cli ──────────────┐
│  python/palace_sleep.py — SQLite     │ └─┤  python/palace_cli.py     │
│  `mode=ro`, always                   │   │  runs in YOUR terminal:   │
└──────────────────────────────────────┘   │  writable, so it is the   │
                                           │  only writer of a real    │
                                           │  recall to the ledger     │
                                           └───────────────────────────┘
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
| `palace_recall` | Search MemPalace and report the top similarity. **Records nothing** — see below. |
| `palace_sleep_status` | Sidecar state, digest age, and exactly what the runtime context is contributing right now. |

## The ledger needs a terminal, so `palace-cli` exists

The recall ledger is the one thing no memory system tracks: *was this memory
ever actually used?* But a runtime context block is assembled inside a confined
session, and **a confined session cannot write the palace.** ChromaDB validates
its tenant and database on open, so it writes even for a pure search; when the
palace sits outside the session workspace, that write is denied:

```
chromadb.errors.InternalError: error returned from database:
  (code: 8) attempt to write a readonly database
```

Measured on Windows 2026-10-03, confined, with the link checked in both
directions:

| Confined operation | Result |
|---|---|
| write through a link inside the workspace → target **inside** | allowed |
| write through a link inside the workspace → target **outside** | denied |
| write direct to that outside target | denied |
| create a link to an outside target | denied |
| read through a link to an outside target | allowed |

and the same results when the link was created *unconfined*. **The sandbox
resolves a link to its real target**, so no symlink or junction makes an outside
palace writable from inside a session. That is why `palace_recall` reports a
failure but does not log one: nine such rows once filled the ledger with noise
and buried the question it exists to answer.

`python/palace_cli.py` is the writable client. Run it from your own terminal,
where there is no confinement:

```bash
palace-cli doctor                       # can this palace be written, and if not, why
palace-cli status                       # sidecar + palace + how many recalls ever succeeded
palace-cli link                         # stable path to the palace at <home>/palace
palace-cli recall "what did I decide"   # search, print hits, record the recall
palace-cli sync                         # re-scan through the link, rebuild the digest
palace-cli mirror --source <palace>     # copy a palace in for confined analysis
```

`doctor` asks the question by *attempting* it rather than guessing from the path,
because the same palace is writable from your terminal and not from a session.
`link` is a convenience path for you and other tools — it does not, and cannot,
widen the sandbox. `mirror` copies bytes rather than linking, because a confined
reader needs the bytes to live inside a workspace.

## Configuration

In [cordis.patch.yml](cordis.patch.yml) or a profile override:

```yaml
- id: palace-sleep
  config:
    palaceDb: '~/KnowledgeBase/mempalace_kb/chroma.sqlite3'   # 你自己的 MemPalace 库
    home: '~/.dsh/palace-sleep'
    injectMode: line            # line | full | off
    scanIntervalMinutes: 720    # 12 h; 0 disables the timer
    scanOnStart: true
    contextOrder: 500
    digestMaxChars: 1200
    pythonPath: python
    memplc: memplc
```

The CLI takes the same two paths as flags, so it needs no config file of its
own — `--palace-db` (a `chroma.sqlite3` or the palace directory; both are
accepted) and `--home` (the sidecar). It defaults to exactly the values above.

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

The CLI needs no installation to be useful. From a Windows checkout:

```bat
palace-cli.cmd doctor
```

or anywhere, without the wrapper:

```bash
python python/palace_cli.py doctor
```

To get `palace-cli` on `PATH` for every shell:

```bash
pip install -e .        # provides the palace-cli console script
```

`doctor` needs no arguments in the normal case. It reads the DSH profile that
already points this plugin at a palace, so you do not have to repeat the path —
and it says out loud which candidate it used, because silently answering about
a different palace than you asked about is worse than saying nothing:

```
[--] nothing at C:\Users\you\KnowledgeBase\...; using this one because
     the DSH profile names it (C:\Workspace\KnowledgeBase\...)
```

Pass `--no-discover` to disable that, or `--palace-db` to pin the path.

## The observation phase

This version deliberately does **not** write anything back to MemPalace. The
plan is to run it and let the data answer whether consolidation is worth
building at all:

- does the injected line ever change what gets recalled?
- does `recall_ledger` show queries whose top similarity is near zero (i.e.
  retrieval is broken) on a continuing basis?
- do the scored "priority" items match what you would have picked?

Only then is write-back, merging, or archiving worth building.

The third question is the one to be careful with: a `similarity` of `0.0` is not
proof that retrieval failed. MemPalace clamps it, and a query whose distances
all exceed 1.0 reports `0.0` while still returning the nearest drawers. Read the
results, not just the number.

## Tests

```bash
node test/plugin.test.mjs          # plugin surface, config, digest, JSON payloads
python test/test_palace_sleep.py   # scoring, sidecar, read-only proof
python test/test_palace_cli.py     # links, writability probe, recall parsing
```

`node --test test/` also works where the test runner is allowed to spawn. Each
Python file is a plain script and runs directly; the CLI tests build a throwaway
palace under the repository rather than touching a live one.

Note: `files` in `package.json` keeps `test/` out of the installed artifact —
the tests live in the repository checkout.
