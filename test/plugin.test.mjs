/**
 * Exercises the plugin without Cordis: config resolution, digest reading, the
 * injected text, and the real `apply(ctx, config)` surface driven by a fake
 * registry. Nothing here spawns Python — the live wiring is checked separately.
 */
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { apply, createDigestReader, lastJson, resolveConfig } from '../src/index.js';

function fakeContext() {
	const tools = new Map();
	const contexts = [];
	return {
		tools,
		contexts,
		logger: { info: () => {} },
		tools_registry: null,
		ctx: {
			logger: { info: () => {} },
			tools: {
				register: (definition) => {
					tools.set(definition.name, definition);
					return () => tools.delete(definition.name);
				}
			},
			systemPrompt: {
				context: (entry) => {
					contexts.push(entry);
					return () => contexts.splice(contexts.indexOf(entry), 1);
				}
			}
		}
	};
}

function writeDigest(home, { line, digest, generatedAt }) {
	writeFileSync(
		join(home, 'digest.json'),
		JSON.stringify({ generated_at: generatedAt, line, digest, stats: { total: 1960 } }),
		'utf8'
	);
}

test('config defaults are the documented ones', () => {
	const resolved = resolveConfig({});
	assert.equal(resolved.injectMode, 'line');
	assert.equal(resolved.scanIntervalMinutes, 720);
	assert.equal(resolved.palaceDb.endsWith(join('mempalace_kb', 'chroma.sqlite3')), true);
	assert.equal(resolved.home.includes('.dsh'), true);
	assert.equal(resolved.home.startsWith('~'), false, 'home must be expanded');
});

test('config overrides win, and an unknown injectMode falls back to line', () => {
	const resolved = resolveConfig({ injectMode: 'nonsense', scanIntervalMinutes: 15, scanOnStart: false });
	assert.equal(resolved.injectMode, 'line');
	assert.equal(resolved.scanIntervalMinutes, 15);
	assert.equal(resolved.scanOnStart, false);
});

test('lastJson picks the final JSON line out of noisy output', () => {
	const text = 'Loading model...\n{"a":1}\nnoise\n{"total":1960}\n';
	assert.deepEqual(lastJson(text), { total: 1960 });
	assert.equal(lastJson('nothing here'), undefined);
});

test('the digest reader caches on mtime and reports age', () => {
	const home = mkdtempSync(join(tmpdir(), 'palace-sleep-'));
	try {
		const generated = new Date(Date.now() - 90 * 60_000).toISOString();
		writeDigest(home, { line: 'one line', digest: '# full', generatedAt: generated });
		const reader = createDigestReader({ home });
		const first = reader.view();
		assert.equal(first.line, 'one line');
		assert.equal(first.ageMinutes, 90);
		writeDigest(home, { line: 'updated', digest: '# full', generatedAt: generated });
		assert.equal(reader.view().line, 'updated', 'a changed mtime must be picked up');
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test('an empty home yields no digest and does not throw', () => {
	const home = mkdtempSync(join(tmpdir(), 'palace-sleep-'));
	try {
		assert.equal(createDigestReader({ home }).view(), null);
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test('apply registers four tools and injects the one-line digest', async () => {
	const home = mkdtempSync(join(tmpdir(), 'palace-sleep-'));
	try {
		writeDigest(home, {
			line: 'Palace 1960 条 · 均分 34.2',
			digest: '# full digest',
			generatedAt: new Date().toISOString()
		});
		const { ctx, tools, contexts } = fakeContext();
		apply(ctx, { home, injectMode: 'line', scanOnStart: false, scanIntervalMinutes: 0 });

		assert.deepEqual([...tools.keys()].sort(), [
			'palace_recall',
			'palace_sleep_digest',
			'palace_sleep_scan',
			'palace_sleep_status'
		]);
		assert.equal(contexts.length, 1);
		assert.equal(contexts[0].name, 'palace-sleep');
		assert.equal(typeof contexts[0].order, 'number');
		const injected = contexts[0].text();
		assert.match(injected, /Palace 记忆/);
		assert.match(injected, /1960/);
		assert.ok(injected.length < 200, 'the default injection must stay tiny');
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test('injectMode full and off behave as documented', async () => {
	const home = mkdtempSync(join(tmpdir(), 'palace-sleep-'));
	try {
		writeDigest(home, {
			line: 'the line',
			digest: 'x'.repeat(5000),
			generatedAt: new Date().toISOString()
		});

		const full = fakeContext();
		apply(full.ctx, { home, injectMode: 'full', scanOnStart: false, scanIntervalMinutes: 0, digestMaxChars: 100 });
		const fullText = full.contexts[0].text();
		assert.match(fullText, /Palace 睡眠摘要/);
		assert.ok(fullText.length < 200, 'digestMaxChars must cap the full form');

		const off = fakeContext();
		apply(off.ctx, { home, injectMode: 'off', scanOnStart: false, scanIntervalMinutes: 0 });
		assert.equal(off.contexts.length, 0, 'off must not register a context');
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test('before the first scan the injection says so instead of staying silent', () => {
	const home = mkdtempSync(join(tmpdir(), 'palace-sleep-'));
	try {
		const { ctx, contexts } = fakeContext();
		apply(ctx, { home, injectMode: 'line', scanOnStart: false, scanIntervalMinutes: 0 });
		assert.match(contexts[0].text(), /尚未扫描/);
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test('the digest tool reads the file and needs no subprocess', async () => {
	const home = mkdtempSync(join(tmpdir(), 'palace-sleep-'));
	try {
		writeDigest(home, { line: 'l', digest: '# the full digest', generatedAt: new Date().toISOString() });
		const { ctx, tools } = fakeContext();
		apply(ctx, { home, injectMode: 'off', scanOnStart: false, scanIntervalMinutes: 0 });
		const value = await tools.get('palace_sleep_digest').execute({});
		assert.equal(value.text, '# the full digest');
		assert.equal(typeof value.ageMinutes, 'number');
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test('palace_recall refuses an empty query', async () => {
	const home = mkdtempSync(join(tmpdir(), 'palace-sleep-'));
	try {
		const { ctx, tools } = fakeContext();
		apply(ctx, { home, injectMode: 'off', scanOnStart: false, scanIntervalMinutes: 0 });
		await assert.rejects(() => tools.get('palace_recall').execute({ query: '   ' }), /non-empty query/);
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

test('every registered tool has a schema, a renderer and an executor', () => {
	const home = mkdtempSync(join(tmpdir(), 'palace-sleep-'));
	try {
		const { ctx, tools } = fakeContext();
		apply(ctx, { home, injectMode: 'off', scanOnStart: false, scanIntervalMinutes: 0 });
		for (const [toolName, definition] of tools) {
			assert.equal(typeof definition.description, 'string', `${toolName} needs a description`);
			assert.equal(definition.parameters.type, 'object', `${toolName} needs an object schema`);
			assert.equal(typeof definition.execute, 'function', `${toolName} needs execute`);
			assert.equal(typeof definition.output?.render, 'function', `${toolName} needs output.render`);
			assert.equal(definition.output?.schema?.type, 'object', `${toolName} needs an output schema`);
		}
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});
