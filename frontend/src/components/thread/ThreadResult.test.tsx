import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ThreadResult from "./ThreadResult";

describe("ThreadResult", () => {
  it("renders loading state", () => {
    render(<ThreadResult loading />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading thread result...");
  });

  it("shows load errors in an alert", () => {
    render(<ThreadResult error="Unable to load result." />);
    expect(screen.getByRole("alert")).toHaveTextContent("Unable to load result.");
  });

  it("renders warnings for a sealed result", () => {
    render(
      <ThreadResult
        result={{
          delegation_id: "delegation-1",
          version: 1,
          source: "reported",
          sealed: true,
          summary: "Done.",
          tests: [],
          warnings: ["Generated file was skipped."],
          base_commit: null,
          result_commit: null,
          result_ref: null,
          changed_files: [],
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:01:00Z",
          sealed_at: "2026-01-01T00:01:00Z",
        }}
      />,
    );
    expect(screen.getByText("Generated file was skipped.")).toBeInTheDocument();
  });

  it("renders changed-file paths from backend records without coercion", () => {
    render(
      <ThreadResult
        result={{
          delegation_id: "delegation-1",
          version: 1,
          source: "reported",
          sealed: true,
          summary: "Done.",
          tests: [],
          warnings: [],
          base_commit: null,
          result_commit: null,
          result_ref: null,
          changed_files: [{ path: "src/parser.ts", status: "M" }, { status: "?" }],
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:01:00Z",
          sealed_at: "2026-01-01T00:01:00Z",
        }}
      />,
    );
    expect(screen.getByText("src/parser.ts")).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });

  it("renders result commit and ref metadata", () => {
    render(
      <ThreadResult
        result={{
          delegation_id: "delegation-1",
          version: 2,
          source: "reported",
          sealed: true,
          summary: "Done.",
          tests: [],
          warnings: [],
          base_commit: "abc123",
          result_commit: "def456",
          result_ref: "refs/yinshi/results/delegation-1",
          changed_files: [],
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:01:00Z",
          sealed_at: "2026-01-01T00:01:00Z",
        }}
      />,
    );
    expect(screen.getByText("Result commit")).toBeInTheDocument();
    expect(screen.getByText("def456")).toBeInTheDocument();
    expect(screen.getByText("refs/yinshi/results/delegation-1")).toBeInTheDocument();
  });

  it("keeps long result content wrapped within its panel", () => {
    render(
      <ThreadResult
        className="result-slot"
        result={{
          delegation_id: "delegation-1",
          version: 1,
          source: "reported",
          sealed: true,
          summary: "x".repeat(500),
          tests: [],
          warnings: [],
          base_commit: null,
          result_commit: null,
          result_ref: null,
          changed_files: [{ path: "src/" + "x".repeat(300) }],
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:01:00Z",
          sealed_at: "2026-01-01T00:01:00Z",
        }}
      />,
    );
    const section = screen.getByTestId("thread-result");
    expect(section).toHaveClass("min-w-0", "max-w-full", "overflow-hidden", "result-slot");
    expect(screen.getByText("src/" + "x".repeat(300))).toHaveClass("break-all");
    expect(screen.getByText("x".repeat(500))).toHaveClass("break-words");
  });

  it("shows reported test commands for a sealed result", () => {
    render(
      <ThreadResult
        result={{
          delegation_id: "delegation-1",
          version: 1,
          source: "reported",
          sealed: true,
          summary: "Parser checked.",
          tests: [{ command: "pytest parser", status: "passed", summary: "12 passed" }],
          warnings: [],
          base_commit: "abc123",
          result_commit: "def456",
          result_ref: "refs/yinshi/results/delegation-1",
          changed_files: [],
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:01:00Z",
          sealed_at: "2026-01-01T00:01:00Z",
        }}
      />,
    );
    expect(screen.getByText("pytest parser")).toBeInTheDocument();
  });
});
