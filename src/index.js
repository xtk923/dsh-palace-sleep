/**
 * palace-sleep — a read-only consolidation layer beside a MemPalace store.
 *
 * Architecture (deliberately a *layer*, never a fork):
 *
 *   trigger (this plugin)  →  ledger (sidecar SQLite, our own)
 *                          →  reader (palace, SQLite mode=ro)
 *                          →  digest (injected as runtime context)
 *
 * Hard rules:
 *   * MemPalace is opened read-only by `python/palace_sleep.py`; this plugin
 *     never imports it and never writes to it. Nothing here is a fork, and
 *     nothing here survives as a patch against the installed package.
 *   * Existing drawers are immutable. Salience and recall history live in the
 *     sidecar, keyed by drawer id.
 *   * Injection is ONE LINE by default: a runtime context block is assembled
 *     on every model step, so the full digest (~500 tokens) must be asked for,
 *     not imposed.
 *
 * @module dsh-palace-sleep
 */

import { spawn } from 'node:child_process';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'palace-sleep';

/** `tools` registers our tools and guard; `systemPrompt` carries the digest. */
export const inject = ['tools', 'systemPrompt'];

const PKG_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SCAN_SCRIPT = join(PKG_ROOT, 'python', 'palace_sleep.py');
const TICK_MS = 60_000;

const DEFAULTS = {
	palaceDb: '~/KnowledgeBase/mempalace_kb/chroma.sqlite3',
	home: '~/.dsh/palace-sleep',
	pythonPath: 'python',
	/** 12 h. The observation phase wants a fresh digest, not a busy loop. */
	scanIntervalMinutes: 720,
	scanOnStart: true,
	/** `line` (default) | `full` | `off` */
	injectMode: 'line',
	contextOrder: 500,
	digestMaxChars: 1200,
	timeoutMs: 300_000,
	memplc: 'memplc'
};

function expandHome(value) {
	const text = String(value ?? '').trim();
	if (text === '~') return homedir();
	if (text.startsWith('~/') || text.startsWith('~\\')) return join(homedir(), text.slice(2));
	return text;
}

export function resolveConfig(config) {
	const raw = config && typeof config === 'object' ? config : {};
	const pick = (key, fallback) => (raw[key] === undefined || raw[key] === null ? fallback : raw[key]);
	const mode = ['line', 'full', 'off'].includes(raw.injectMode) ? raw.injectMode : DEFAULTS.injectMode;
	return {
		palaceDb: resolve(expandHome(pick('palaceDb', DEFAULTS.palaceDb))),
		home: resolve(expandHome(pick('home', DEFAULTS.home))),
		pythonPath: String(pick('pythonPath', DEFAULTS.pythonPath)),
		memplc: String(pick('memplc', DEFAULTS.memplc)),
		scanIntervalMinutes: Number(pick('scanIntervalMinutes', DEFAULTS.scanIntervalMinutes)) || 0,
		scanOnStart: pick('scanOnStart', DEFAULTS.scanOnStart) !== false,
		injectMode: mode,
		contextOrder: Number(pick('contextOrder', DEFAULTS.contextOrder)) || 0,
		digestMaxChars: Number(pick('digestMaxChars', DEFAULTS.digestMaxChars)) || 0,
		timeoutMs: Number(pick('timeoutMs', DEFAULTS.timeoutMs)) || DEFAULTS.timeoutMs
	};
}

/** Spawn a process and collect its output as UTF-8 text. Never throws. */
function run(command, args, { timeoutMs, env = {} }) {
	return new Promise((settle) => {
		let child;
		try {
			child = spawn(command, args, {
				stdio: ['ignore', 'pipe', 'pipe'],
				windowsHide: true,
				env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', ...env }
			});
		} catch (error) {
			settle({ code: -1, stdout: '', stderr: String(error?.message ?? error) });
			return;
		}
		let stdout = '';
		let stderr = '';
		const timer = setTimeout(() => {
			try {
				child.kill();
			} catch {
				/* already gone */
			}
		}, timeoutMs);
		child.stdout?.setEncoding('utf8');
		child.stderr?.setEncoding('utf8');
		child.stdout?.on('data', (chunk) => {
			stdout += chunk;
		});
		child.stderr?.on('data', (chunk) => {
			stderr += chunk;
		});
		child.on('error', (error) => {
			clearTimeout(timer);
			settle({ code: -1, stdout, stderr: `${stderr}${String(error?.message ?? error)}` });
		});
		child.on('close', (code) => {
			clearTimeout(timer);
			settle({ code: code ?? -1, stdout, stderr });
		});
	});
}

/**
 * The last complete JSON object printed by a helper, or undefined.
 *
 * Balanced-brace scan, not a line scan: `memplc` is an in-process MemPalace
 * wrapper and pretty-prints with `indent=2`, so a search answer spans many
 * lines. The line-oriented version reported that as "no JSON" and every recall
 * was recorded as zero rows — a failure wearing the costume of an empty result.
 */
export function lastJson(text) {
	const input = String(text ?? '');
	const decoder = new JSONDecoder(input);
	let found;
	for (let index = 0; index < input.length; index += 1) {
		if (input[index] !== '{') continue;
		const value = decoder.decode(index);
		if (value !== undefined) {
			found = value;
			index = decoder.end - 1;
		}
	}
	return found;
}

/** Minimal balanced-object reader over a string, string-escape aware. */
class JSONDecoder {
	constructor(input) {
		this.input = input;
		this.end = 0;
	}

	decode(start) {
		let depth = 0;
		let inString = false;
		let escaped = false;
		for (let index = start; index < this.input.length; index += 1) {
			const char = this.input[index];
			if (inString) {
				if (escaped) escaped = false;
				else if (char === '\\') escaped = true;
				else if (char === '"') inString = false;
				continue;
			}
			if (char === '"') inString = true;
			else if (char === '{') depth += 1;
			else if (char === '}') {
				depth -= 1;
				if (depth === 0) {
					try {
						const value = JSON.parse(this.input.slice(start, index + 1));
						if (value && typeof value === 'object') {
							this.end = index + 1;
							return value;
						}
						return undefined;
					} catch {
						return undefined;
					}
				}
			}
		}
		return undefined;
	}
}

function objectSchema(properties, required = []) {
	return { type: 'object', properties, required, additionalProperties: false };
}
const str = (description) => ({ type: 'string', description });
const num = (description) => ({ type: 'number', description });
const textBlock = (text) => [{ type: 'text', text }];

/** Read the digest the scan wrote, re-reading only when the file changed. */
export function createDigestReader(config) {
	const jsonPath = join(config.home, 'digest.json');
	let cache = { mtime: -1, value: null };

	function load() {
		try {
			const info = statSync(jsonPath);
			if (info.mtimeMs !== cache.mtime) {
				cache = { mtime: info.mtimeMs, value: JSON.parse(readFileSync(jsonPath, 'utf8')) };
			}
		} catch {
			if (cache.mtime !== -1) cache = { mtime: -1, value: null };
		}
		return cache.value;
	}

	return {
		load,
		/** `{ line, digest, ageMinutes }`, or null before the first scan. */
		view() {
			const value = load();
			if (!value) return null;
			const at = value.generated_at ? Date.parse(value.generated_at) : NaN;
			return {
				line: String(value.line ?? ''),
				digest: String(value.digest ?? ''),
				generatedAt: value.generated_at,
				ageMinutes: Number.isFinite(at) ? Math.round((Date.now() - at) / 60_000) : null,
				stats: value.stats ?? {}
			};
		},
		path: jsonPath
	};
}

/**
 * Read one MemPalace search answer.
 *
 * MemPalace reports its own failures as `{"error": "..."}` on stdout, so a
 * successful parse is NOT a successful search. The first version conflated the
 * two: a broken search came back as "0 rows", `topSimilarity` became null, and
 * output validation rejected it — a real retrieval failure wearing the costume
 * of a schema bug. Hence a pure, testable classifier.
 */
export function parseSearch(stdout, code, stderr) {
	const parsed = lastJson(stdout);
	const rows = Array.isArray(parsed?.results) ? parsed.results : [];
	const sources = rows.map((row) => String(row?.source_file ?? '')).filter(Boolean);
	const similarities = rows.map((row) => Number(row?.similarity)).filter((value) => Number.isFinite(value));
	const top = similarities.length ? Math.max(...similarities) : null;
	const failure = !parsed
		? `memplc exit ${code}: ${String(stderr ?? '').slice(-300).trim()}`
		: typeof parsed.error === 'string'
			? parsed.error
			: undefined;
	return { parsed, rows, sources, similarities, top, error: failure };
}

function install(ctx, config) {
	const disposers = [];
	const digest = createDigestReader(config);
	let scanning = false;

	const log = (...args) => {
		try {
			ctx.logger?.info?.(...args);
		} catch {
			/* logging is never worth a failure */
		}
	};

	async function python(subArgs) {
		return run(config.pythonPath, [SCAN_SCRIPT, '--home', config.home, '--palace-db', config.palaceDb, ...subArgs], {
			timeoutMs: config.timeoutMs
		});
	}

	async function scan(note) {
		if (scanning) return { skipped: 'already-running' };
		scanning = true;
		try {
			const args = ['scan'];
			if (note) args.push('--note', note);
			const result = await python(args);
			const parsed = lastJson(result.stdout);
			if (!parsed) {
				log(`palace-sleep scan failed (exit ${result.code}): ${result.stderr.slice(-400)}`);
				return { ok: false, code: result.code, error: result.stderr.slice(-400) || 'no JSON summary' };
			}
			log(`palace-sleep scan ok: ${parsed.total} drawers, mean ${parsed.mean_score}`);
			return { ok: true, ...parsed };
		} finally {
			scanning = false;
		}
	}

	async function recall(query, limit, note) {
		const result = await run(config.memplc, ['search', query, '--limit', String(limit)], {
			timeoutMs: config.timeoutMs
		});
		const answer = parseSearch(result.stdout, result.code, result.stderr);

		// Only a recall that actually reached MemPalace is recorded.
		//
		// A confined session cannot write the palace: ChromaDB validates its
		// tenant/database on open, and the palace normally lives outside the
		// session workspace, so `memplc` fails here every single time. Those
		// failures are still reported to the caller below, but they are NOT
		// ledger entries — nine of them once filled the ledger with noise and
		// buried the one question it exists to answer. The ledger stays a record
		// of real retrievals, written by python/palace_cli.py from an
		// unrestricted terminal.
		if (!answer.error) {
			void python([
				'recall',
				'--query',
				query,
				'--tool',
				'palace_recall',
				'--returned',
				String(answer.rows.length),
				'--hits',
				String(answer.rows.length),
				'--ids',
				answer.sources.join(','),
				'--note',
				[`top_similarity=${answer.top ?? 'n/a'}`, `${answer.rows.length} rows`, note ?? '']
					.filter(Boolean)
					.join(' ')
			]).catch(() => undefined);
		}

		return { query, ...answer };
	}

	// --- tools ---------------------------------------------------------------
	disposers.push(
		ctx.tools.register({
			name: 'palace_sleep_scan',
			description:
				'Re-scan the MemPalace store (read-only), refresh the salience sidecar and rebuild the digest. ' +
				'Safe to call any time; it writes only to the sidecar and never to MemPalace.',
			parameters: objectSchema({ note: str('Optional note recorded with this run.') }),
			output: {
				schema: objectSchema(
					{
						ok: { type: 'boolean' },
						total: num('Drawers scanned.'),
						mean_score: num('Mean salience.'),
						noise: num('Drawers that look like machine debris.'),
						unresolved: num('Drawers flagged unresolved/risky.'),
						clusters: num('Near-duplicate clusters.'),
						cluster_members: num('Drawers inside those clusters.'),
						fresh_days: num('Days since the newest pre-today memory.'),
						error: str('Present when the scan failed.')
					},
					['ok']
				),
				render: (_args, value) =>
					value.ok
						? textBlock(
								`Scanned ${value.total} drawers · mean ${value.mean_score} · unresolved ${value.unresolved} · ` +
									`noise ${value.noise} · clusters ${value.clusters}/${value.cluster_members} · ` +
									`newest pre-today ${value.fresh_days}d`
							)
						: textBlock(`Scan failed: ${value.error ?? 'unknown'}`)
			},
			async execute(args) {
				const note = args && typeof args === 'object' && typeof args.note === 'string' ? args.note : undefined;
				return scan(note);
			}
		})
	);

	disposers.push(
		ctx.tools.register({
			name: 'palace_sleep_digest',
			description:
				'Return the full consolidation digest: statistics, the highest-salience memories worth re-reading, ' +
				'unresolved/risky items, and the open problems the sidecar is tracking.',
			parameters: objectSchema({}),
			output: {
				schema: objectSchema({ text: str('The digest.'), ageMinutes: num('Minutes since it was generated.') }, [
					'text'
				]),
				render: (_args, value) => textBlock(value.text)
			},
			async execute() {
				const view = digest.view();
				if (!view) return { text: '(no digest yet — call palace_sleep_scan first)' };
				return { text: view.digest, ageMinutes: view.ageMinutes, generatedAt: view.generatedAt };
			}
		})
	);

	disposers.push(
		ctx.tools.register({
			name: 'palace_recall',
			description:
				'Search MemPalace and record the query in the recall ledger. Prefer this over calling memplc directly: ' +
				'the ledger is what makes "was this memory ever used?" answerable later. Reports the top similarity, ' +
				'because near-zero similarity means retrieval returned unrelated memories.',
			parameters: objectSchema(
				{
					query: str('What to search for.'),
					limit: num('How many results. Defaults to 5.'),
					note: str('Optional note recorded with this recall.')
				},
				['query']
			),
			output: {
				schema: objectSchema(
					{
						query: str('The query.'),
						rows: num('Rows returned.'),
						topSimilarity: num('Highest similarity reported by MemPalace.'),
						sources: { type: 'array', items: { type: 'string' }, description: 'Source files returned.' },
						error: str('Present when MemPalace did not answer.')
					},
					['query', 'rows']
				),
				render: (_args, value) => {
					if (value.error) return textBlock(`MemPalace search failed: ${value.error}`);
					const head =
						`${value.rows} result(s), top similarity ${value.topSimilarity ?? 'n/a'}` +
						(value.topSimilarity !== null && value.topSimilarity < 0.3
							? ' — that is near-random, retrieval is likely still broken'
							: '');
					const lines = value.sources.slice(0, 5).map((source) => `- ${source}`);
					return textBlock([head, ...lines, '(recorded in the recall ledger)'].join('\n'));
				}
			},
			async execute(args) {
				const input = args && typeof args === 'object' ? args : {};
				if (typeof input.query !== 'string' || input.query.trim() === '') {
					throw new Error('palace_recall requires a non-empty query');
				}
				const limit = Number.isFinite(Number(input.limit)) && Number(input.limit) > 0 ? Number(input.limit) : 5;
				const result = await recall(input.query, limit, input.note);
				return {
					query: result.query,
					rows: result.rows.length,
					// Omitted rather than nulled: the declared schema says number,
					// and the registry validates what a tool returns.
					...(Number.isFinite(result.top) ? { topSimilarity: result.top } : {}),
					sources: result.sources,
					...(result.error ? { error: result.error } : {})
				};
			}
		})
	);

	disposers.push(
		ctx.tools.register({
			name: 'palace_sleep_status',
			description:
				'Report the sidecar state: when the last scan ran, how many drawers are scored, how many recalls are ' +
				'logged, and what the prompt injection is currently contributing.',
			parameters: objectSchema({}),
			output: {
				schema: objectSchema(
					{
						home: str('Sidecar directory.'),
						injectMode: str('line | full | off'),
						digestAgeMinutes: num('Minutes since the digest was generated.'),
						injected: str('Exactly what the runtime context contributes right now.'),
						sidecar: str('Raw status output from the helper.')
					},
					['home', 'injectMode']
				),
				render: (_args, value) =>
					textBlock(
						`sidecar ${value.home}\ninject ${value.injectMode} · digest age ${value.digestAgeMinutes ?? 'n/a'} min\n` +
							`context now: ${value.injected || '(empty)'}`
					)
			},
			async execute() {
				const result = await python(['status']);
				return {
					home: config.home,
					injectMode: config.injectMode,
					digestAgeMinutes: digest.view()?.ageMinutes ?? null,
					injected: contextText(),
					sidecar: result.stdout.trim() || result.stderr.slice(-400)
				};
			}
		})
	);

	// --- prompt injection ----------------------------------------------------
	/**
	 * What every model step sees. `line` is the default because a runtime
	 * context block is assembled per step: the full digest is roughly 500
	 * tokens and the line roughly 50.
	 */
	function contextText() {
		if (config.injectMode === 'off') return '';
		const view = digest.view();
		if (!view) return 'Palace 记忆：尚未扫描（可调用 palace_sleep_scan 生成睡眠摘要）。';
		const age = view.ageMinutes === null ? '' : `（${view.ageMinutes} 分钟前）`;
		if (config.injectMode === 'line') return `Palace 记忆${age}：${view.line}`;
		const body = view.digest.slice(0, config.digestMaxChars);
		return `Palace 睡眠摘要${age}\n${body}`;
	}

	if (config.injectMode !== 'off' && typeof ctx.systemPrompt?.context === 'function') {
		disposers.push(
			ctx.systemPrompt.context({
				name: 'palace-sleep',
				order: config.contextOrder,
				text: () => contextText()
			})
		);
	}

	// --- trigger -------------------------------------------------------------
	async function tick() {
		if (config.scanIntervalMinutes <= 0) return;
		const view = digest.view();
		if (view && (view.ageMinutes ?? 0) < config.scanIntervalMinutes) return;
		await scan('interval');
	}

	const timer = setInterval(() => {
		void tick().catch(() => undefined);
	}, TICK_MS);
	timer.unref?.();
	disposers.push(() => clearInterval(timer));

	if (config.scanOnStart) {
		const view = digest.view();
		const stale =
			!view || (config.scanIntervalMinutes > 0 && (view.ageMinutes ?? 0) >= config.scanIntervalMinutes);
		if (stale) void scan('startup').catch(() => undefined);
	}

	log(`palace-sleep active (inject=${config.injectMode}, sidecar=${config.home})`);

	return () => {
		for (const dispose of disposers.reverse()) {
			try {
				dispose();
			} catch {
				/* a throwing disposer must not stop the rest */
			}
		}
	};
}

/** Cordis entry point. */
export function apply(ctx, config) {
	const resolved = resolveConfig(config);
	if (typeof ctx.effect === 'function') {
		ctx.effect(() => install(ctx, resolved));
		return;
	}
	install(ctx, resolved);
}

export { existsSync };
