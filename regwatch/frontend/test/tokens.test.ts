/**
 * @vitest-environment node
 *
 * Design-token drift guard, scoped: the palette's source of truth is the :root
 * block in app/globals.css, and this suite verifies (1) the tailwind color
 * MIRRORS listed below match their :root tokens and (2) the two NAMED hexes
 * (--paper-bright #fffdf8, --green-settled #3f7d54) appear only in :root and
 * are never hardcoded under components/. It does NOT catch every drift -- any
 * other raw hex in a component is outside this guard.
 *
 * app/studio/studio.css keeps its own local token copies by design and is
 * deliberately outside this guard.
 */
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import tailwindConfig from "../tailwind.config";

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const GLOBALS_CSS = readFileSync(path.join(FRONTEND_ROOT, "app", "globals.css"), "utf8");

/** Extracts the `--name: value` custom properties declared in the :root block. */
function parseRootTokens(css: string): Map<string, string> {
  const root = /:root\s*\{([\s\S]*?)\n\}/.exec(css);
  if (!root || root[1] === undefined) throw new Error("no :root block found in globals.css");
  const tokens = new Map<string, string>();
  for (const decl of root[1].matchAll(/--([\w-]+):\s*([^;]+);/g)) {
    tokens.set(`--${decl[1]}`, (decl[2] ?? "").trim());
  }
  return tokens;
}

/** Walks a nested tailwind color object to a string leaf, throwing on any miss. */
function tailwindColor(colors: unknown, keys: readonly string[]): string {
  let node: unknown = colors;
  for (const key of keys) {
    if (typeof node !== "object" || node === null || !(key in node)) {
      throw new Error(`tailwind colors missing key path ${keys.join(".")}`);
    }
    node = (node as Record<string, unknown>)[key];
  }
  if (typeof node !== "string") throw new Error(`tailwind color at ${keys.join(".")} is not a string`);
  return node;
}

/** Every file (absolute path) under dir, recursively. */
function walkFiles(dir: string): readonly string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) files.push(...walkFiles(full));
    else if (entry.isFile()) files.push(full);
  }
  return files;
}

/** Case-insensitive occurrence count, so an uppercased hex can't dodge the guard. */
function countOccurrences(haystack: string, needle: string): number {
  return haystack.toLowerCase().split(needle.toLowerCase()).length - 1;
}

const rootTokens = parseRootTokens(GLOBALS_CSS);
const twColors: unknown = tailwindConfig.theme?.extend?.colors;

describe("tailwind color mirrors match globals.css :root", () => {
  // tailwind key path -> the :root custom property it mirrors.
  const MIRRORS: ReadonlyArray<readonly [readonly string[], string]> = [
    [["paper", "DEFAULT"], "--paper"],
    [["paper", "deep"], "--paper-2"],
    [["paper", "edge"], "--edge"],
    [["ink", "DEFAULT"], "--ink"],
    [["ink", "soft"], "--ink-soft"],
    [["ink", "faint"], "--ink-faint"],
    [["gold", "DEFAULT"], "--gold"],
    [["gold", "deep"], "--gold-deep"],
    [["gold", "ink"], "--gold-ink"],
    [["oxblood"], "--oxblood"],
  ];

  it.each(MIRRORS.map(([keys, cssVar]) => ({ keys, cssVar, label: keys.join(".") })))(
    "$label mirrors $cssVar",
    ({ keys, cssVar }) => {
      const cssValue = rootTokens.get(cssVar);
      expect(cssValue, `${cssVar} is declared in :root`).toBeDefined();
      expect(tailwindColor(twColors, keys)).toBe(cssValue);
    },
  );
});

describe("raised-surface and settled-green hexes live only in :root", () => {
  it("declares --paper-bright and --green-settled in :root", () => {
    expect(rootTokens.get("--paper-bright")).toBe("#fffdf8");
    expect(rootTokens.get("--green-settled")).toBe("#3f7d54");
  });

  it.each(["#fffdf8", "#3f7d54"])("%s appears exactly once in globals.css", (hex) => {
    expect(countOccurrences(GLOBALS_CSS, hex)).toBe(1);
  });

  it("never hardcodes either hex under components/", () => {
    const offenders = walkFiles(path.join(FRONTEND_ROOT, "components")).filter((file) => {
      const text = readFileSync(file, "utf8");
      return countOccurrences(text, "#fffdf8") > 0 || countOccurrences(text, "#3f7d54") > 0;
    });
    expect(offenders).toEqual([]);
  });
});
