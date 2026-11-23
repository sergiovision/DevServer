#!/usr/bin/env node
/**
 * Feature-catalogue coverage report.
 *
 * Why it exists: `docs/feature-catalogue.md` lists every catalogued behaviour
 * as `F-<AREA>-<NN>`, and every test proving one carries a
 * `// Feature: F-…` (TS) or `# Feature: F-…` (Python) tag on the line above
 * it. Without a check the two drift: rows lose their tests, tests cite IDs
 * nobody catalogued. This script reports both directions:
 *
 *   - catalogue IDs with no test citation (only rows whose layer is not
 *     `docs` / `ui` are expected to have tests — those are listed separately
 *     as informational);
 *   - test citations whose ID has no catalogue row.
 *
 * Exit code: 0 when clean, 1 when a citation has no row or a testable row has
 * no test. CI runs it non-blocking (`continue-on-error`), so the report is
 * advisory until the catalogue settles.
 *
 * No dependencies: plain Node (>= 18) fs walking.
 *
 * Usage: node scripts/feature-coverage.mjs [--json]
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CATALOGUE = join(ROOT, 'docs', 'feature-catalogue.md');

/** Directories holding tests, and how to recognise a test file in each. */
const TEST_ROOTS = [
  { dir: join(ROOT, 'apps', 'web', 'src'), isTest: (name) => name.endsWith('.test.ts') },
  { dir: join(ROOT, 'apps', 'worker', 'tests'), isTest: (name) => /^test_.*\.py$/.test(name) },
  { dir: join(ROOT, 'apps', 'mcp-memory'), isTest: (name) => /^test_.*\.py$/.test(name) },
];

const SKIP_DIRS = new Set(['node_modules', '.next', '.venv', '__pycache__', '.git']);

/** Layers whose rows document behaviour no unit test can assert. */
const UNTESTED_LAYERS = new Set(['docs', 'ui', 'ci', 'db']);

const ID_PATTERN = /F-[A-Z]+-\d{2,}/g;
const CITATION_PATTERN = /^\s*(?:\/\/|#)\s*Feature:\s*(.+)$/;

function walk(dir, accept, out = []) {
  let entries;
  try {
    entries = readdirSync(dir);
  } catch {
    return out;
  }
  for (const name of entries) {
    if (SKIP_DIRS.has(name)) continue;
    const full = join(dir, name);
    const stat = statSync(full);
    if (stat.isDirectory()) walk(full, accept, out);
    else if (accept(name)) out.push(full);
  }
  return out;
}

/** Parse `| F-XXX-NN | behaviour | source | layer |` table rows. */
export function parseCatalogue(markdown) {
  const rows = new Map();
  for (const line of markdown.split('\n')) {
    const cells = line.split('|').map((cell) => cell.trim());
    if (cells.length < 5) continue;
    const id = cells[1];
    if (!/^F-[A-Z]+-\d{2,}$/.test(id)) continue;
    rows.set(id, { id, behaviour: cells[2], source: cells[3], layer: cells[4].toLowerCase() });
  }
  return rows;
}

/** Collect `Feature:` citations: id → list of `file:line`. */
export function collectCitations(files, readText = (file) => readFileSync(file, 'utf8')) {
  const citations = new Map();
  for (const file of files) {
    const lines = readText(file).split('\n');
    lines.forEach((line, index) => {
      const match = CITATION_PATTERN.exec(line);
      if (!match) return;
      for (const id of match[1].match(ID_PATTERN) ?? []) {
        const where = `${relative(ROOT, file)}:${index + 1}`;
        citations.set(id, [...(citations.get(id) ?? []), where]);
      }
    });
  }
  return citations;
}

export function compare(rows, citations) {
  const uncoveredTestable = [];
  const uncoveredInformational = [];
  for (const row of rows.values()) {
    if (citations.has(row.id)) continue;
    (UNTESTED_LAYERS.has(row.layer) ? uncoveredInformational : uncoveredTestable).push(row.id);
  }
  const orphanCitations = [...citations.keys()].filter((id) => !rows.has(id));
  return { uncoveredTestable, uncoveredInformational, orphanCitations };
}

function main() {
  const rows = parseCatalogue(readFileSync(CATALOGUE, 'utf8'));
  const files = TEST_ROOTS.flatMap(({ dir, isTest }) => walk(dir, isTest));
  const citations = collectCitations(files);
  const report = compare(rows, citations);

  if (process.argv.includes('--json')) {
    console.log(JSON.stringify({ rows: rows.size, cited: citations.size, ...report }, null, 2));
  } else {
    console.log(`Catalogue rows: ${rows.size}; IDs cited by tests: ${citations.size}`);
    const section = (title, ids) => {
      if (ids.length === 0) return;
      console.log(`\n${title} (${ids.length}):`);
      for (const id of ids) console.log(`  ${id}`);
    };
    section('Rows without tests', report.uncoveredTestable);
    section('Citations without a catalogue row', report.orphanCitations);
    section('Untested by design (docs/ui/ci/db layer)', report.uncoveredInformational);
    if (report.uncoveredTestable.length === 0 && report.orphanCitations.length === 0) {
      console.log('\nCoverage OK.');
    }
  }
  process.exitCode = report.uncoveredTestable.length || report.orphanCitations.length ? 1 : 0;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
