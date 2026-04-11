import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect } from "vitest";
import Landing from "../Landing";

function renderLanding() {
  return render(
    <MemoryRouter>
      <Landing />
    </MemoryRouter>,
  );
}

describe("Landing", () => {
  it("renders the brand name in the nav", () => {
    renderLanding();
    expect(screen.getByText("Yinshi", { selector: ".landing-brand" })).toBeInTheDocument();
  });

  it("renders the tagline", () => {
    renderLanding();
    expect(screen.getByText(/coding agents in your browser/i)).toBeInTheDocument();
  });

  it("renders the mascot image", () => {
    renderLanding();
    const img = screen.getByAltText(/yinshi scholar/i);
    expect(img).toBeInTheDocument();
    expect(img).toHaveAttribute("src", "/yinshi-scholar.jpg");
  });

  it("renders sign-in links", () => {
    renderLanding();
    const links = screen.getAllByRole("link", { name: /sign in|get started|enter/i });
    expect(links.length).toBeGreaterThanOrEqual(1);
    expect(links[0]).toHaveAttribute("href", "/auth/login");
  });

  it("renders the updated capabilities with architecture links", () => {
    renderLanding();

    expect(screen.getByText(/AI Agent Sessions/)).toBeInTheDocument();
    expect(screen.getByText(/Mobile-First Interface/)).toBeInTheDocument();
    expect(screen.getByText(/Tenant Isolation/)).toBeInTheDocument();
    expect(screen.getByText(/Encrypted Secrets/)).toBeInTheDocument();

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
