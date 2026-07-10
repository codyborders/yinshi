/* Covers landing content, actions, accessibility, and product evidence through DOM queries. */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect } from "vitest";
import Landing from "../Landing";

function renderLanding(initialEntry = "/") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Landing />
    </MemoryRouter>,
  );
}

describe("Landing", () => {
  it("renders the brand name in the nav", () => {
    renderLanding();
    expect(screen.getByText("Yinshi", { selector: ".landing-brand" })).toBeInTheDocument();
  });

  it("explains the repository workflow with concrete product evidence", () => {
    renderLanding();

    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Run coding agents against your repositories from any browser.",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Import a GitHub or allowed local repository. Yinshi creates an isolated git worktree, connects a pi agent, and streams the session to your browser.",
      ),
    ).toBeInTheDocument();

    const workspacePreview = screen.getByLabelText("Example Yinshi coding workspace");
    expect(within(workspacePreview).getByText("Web app")).toBeInTheDocument();
    expect(screen.queryByText(/codyborders/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "GitHub repository" })).not.toBeInTheDocument();
    expect(within(workspacePreview).getByText("session/quiet-pine")).toBeInTheDocument();
    expect(within(workspacePreview).getByText("pi connected")).toBeInTheDocument();
    expect(
      within(workspacePreview).getByText("Add retry feedback when a repository import fails."),
    ).toBeInTheDocument();
    expect(within(workspacePreview).getByText("6 focused tests passed")).toBeInTheDocument();
  });

  it("describes the repository-to-branch workflow", () => {
    renderLanding();

    const workflow = screen.getByRole("region", {
      name: "From repository to reviewable branch",
    });
    expect(within(workflow).getByText("Connect a repository")).toBeInTheDocument();
    expect(within(workflow).getByText("Give pi a task")).toBeInTheDocument();
    expect(within(workflow).getByText("Review the branch")).toBeInTheDocument();
  });

  it("explains each workflow step", () => {
    renderLanding();
    expect(
      screen.getByText("Choose a GitHub repository or an allowed local path available to the server."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Yinshi creates a named worktree and streams pi’s messages, tool calls, and file edits.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Inspect the result, then merge or discard the workspace through your existing Git tools.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps workspace evidence separate from the hero", () => {
    renderLanding();
    const hero = screen.getByRole("region", {
      name: "Run coding agents against your repositories from any browser.",
    });
    expect(
      within(hero).queryByLabelText("Example Yinshi coding workspace"),
    ).not.toBeInTheDocument();
  });

  it("states the technical foundation", () => {
    renderLanding();

    const foundation = screen.getByRole("region", { name: "Technical foundation" });
    expect(within(foundation).getByText("One git worktree per session")).toBeInTheDocument();
    expect(within(foundation).getByText("pi coding agent")).toBeInTheDocument();
    expect(
      within(foundation).getByText("GitHub App or allowed local path"),
    ).toBeInTheDocument();
  });

  it("provides a skip link to the main content", () => {
    renderLanding();

    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute(
      "href",
      "#landing-main",
    );
    expect(screen.getByRole("main")).toHaveAttribute("id", "landing-main");
  });

  it("announces sign-in errors", () => {
    renderLanding("/?error=oauth_error");

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Sign-in was cancelled or failed. Please try again.",
    );
  });

  it("renders the mascot image", () => {
    renderLanding();
    const img = screen.getByAltText(/yinshi scholar/i);
    expect(img).toBeInTheDocument();
    expect(img).toHaveAttribute("src", "/yinshi-scholar.jpg");
  });

  it("renders a prominent scholar logo", () => {
    renderLanding();
    const logo = screen.getByAltText(/yinshi scholar/i);
    expect(logo).toHaveAttribute("width", "180");
    expect(logo).toHaveAttribute("height", "180");
  });

  it("places the scholar logo in the hero composition", () => {
    renderLanding();
    const hero = screen.getByRole("region", {
      name: "Run coding agents against your repositories from any browser.",
    });
    expect(within(hero).getByAltText(/yinshi scholar/i)).toBeInTheDocument();
  });

  it("renders sign-in links", () => {
    renderLanding();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/auth/login",
    );
    const workspaceLinks = screen.getAllByRole("link", { name: "Start a workspace" });
    expect(workspaceLinks).toHaveLength(2);
    for (const workspaceLink of workspaceLinks) {
      expect(workspaceLink).toHaveAttribute("href", "/auth/login");
    }
  });

  it("renders the updated capabilities with architecture links", () => {
    renderLanding();

    expect(screen.getByRole("heading", { level: 3, name: "AI agent sessions" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Mobile-first interface" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Tenant isolation" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Encrypted secrets" })).toBeInTheDocument();

    expect(screen.queryByText(/Git Workspaces/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Branching by Default/)).not.toBeInTheDocument();

    expect(screen.getByRole("link", { name: /Container isolation/i })).toHaveAttribute(
      "href",
      "/architecture.html#container-isolation",
    );
    expect(screen.getByRole("link", { name: /GitHub App integration/i })).toHaveAttribute(
      "href",
      "/architecture.html#github-app-integration",
    );
    expect(screen.getByRole("link", { name: /Encryption and key management/i })).toHaveAttribute(
      "href",
      "/architecture.html#encryption-key-management",
    );

    const capabilityTitles = screen.getAllByRole("heading", { level: 3 });
    expect(capabilityTitles).toHaveLength(4);
  });
});
