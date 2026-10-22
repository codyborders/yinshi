import { readFile } from "node:fs/promises";
import path from "node:path";
import postcss, { type Root } from "postcss";
import tailwindcss from "@tailwindcss/postcss";
import { beforeAll, describe, expect, it } from "vitest";

let stylesheet: Root;

function declarations(selector: string): Record<string, string> {
  const values: Record<string, string> = {};
  stylesheet.walkRules(selector, (rule) => {
    rule.each((node) => {
      if (node.type === "decl") values[node.prop] = node.value;
    });
  });
  return values;
}

beforeAll(async () => {
  const from = path.join(import.meta.dirname, "index.css");
  const result = await postcss([tailwindcss()]).process(
    await readFile(from, "utf8"),
    { from },
  );
  stylesheet = result.root;
});

describe("compiled theme styles", () => {
  it("retains the dynamic theme palette and safe-area padding", () => {
    expect(declarations("body")).toMatchObject({
      "background-color": "rgb(var(--gray-900) / 1)",
      color: "rgb(var(--gray-100) / 1)",
      "padding-top": "env(safe-area-inset-top)",
      "padding-left": "env(safe-area-inset-left)",
      "padding-right": "env(safe-area-inset-right)",
    });
    expect(declarations(".dark")["--gray-900"]).toBe("26 20 16");
    expect(declarations(":root")["--gray-900"]).toBe("240 230 211");
  });

  it("retains primary and secondary button colors and touch targets", () => {
    expect(declarations(".btn-primary")).toMatchObject({
      "background-color": "#c23b22",
      "min-height": "44px",
      "min-width": "44px",
    });
    expect(declarations(".btn-secondary")).toMatchObject({
      "background-color": "rgb(var(--gray-800) / 1)",
      "min-height": "44px",
      "min-width": "44px",
    });
    expect(declarations(".text-red-400").color).toBe("#f87171");
  });

  it("uses the selected dark class rather than the device color scheme", () => {
    expect(declarations(".dark\\:text-amber-200:is(.dark *)").color).toBe(
      "#fde68a",
    );
    expect(stylesheet.toString()).not.toContain("prefers-color-scheme");
  });

  it("retains forced-color focus outlines after the utility rename", () => {
    let forcedColorOutline = false;
    stylesheet.walkRules(".outline-hidden", (rule) => {
      rule.walkAtRules("media", (media) => {
        if (media.params !== "(forced-colors: active)") return;
        media.walkDecls("outline", (declaration) => {
          forcedColorOutline = declaration.value === "2px solid transparent";
        });
      });
    });
    expect(forcedColorOutline).toBe(true);
  });
});
