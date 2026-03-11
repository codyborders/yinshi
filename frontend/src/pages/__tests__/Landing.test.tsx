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
    expect(screen.getByText(/hidden scholar/i)).toBeInTheDocument();
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

  it("renders capability sections", () => {
    renderLanding();
    expect(screen.getByText(/Git Workspaces/)).toBeInTheDocument();
    expect(screen.getByText(/AI Agent Sessions/)).toBeInTheDocument();
  });
});
