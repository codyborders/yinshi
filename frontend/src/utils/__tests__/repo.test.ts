import { describe, expect, it } from "vitest";
import {
  deriveRepoName,
  isGithubShorthand,
  isGitUrl,
  isLocalPath,
} from "../repo";

describe("repo utils", () => {
  it("accepts GitHub owner/repo shorthand", () => {
    expect(isGithubShorthand("openai/gpt-5")).toBe(true);
    expect(isGithubShorthand("openai/gpt-5.git")).toBe(true);
  });

  it("rejects non-GitHub shorthand inputs", () => {
    expect(isGithubShorthand("https://github.com/openai/gpt-5")).toBe(false);
    expect(isGithubShorthand("./openai/gpt-5")).toBe(false);
    expect(isGithubShorthand("openai/gpt-5/issues")).toBe(false);
  });

  it("keeps existing URL and path detection behavior", () => {
    expect(isGitUrl("https://github.com/openai/gpt-5")).toBe(true);
    expect(isLocalPath("/tmp/repo")).toBe(true);
    expect(deriveRepoName("https://github.com/openai/gpt-5.git")).toBe("gpt-5");
  });
});
