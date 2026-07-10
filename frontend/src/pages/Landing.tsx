import { useSearchParams } from "react-router-dom";
import "./Landing.css";

const ERROR_MESSAGES: Record<string, string> = {
  oauth_error: "Sign-in was cancelled or failed. Please try again.",
  github_api_error: "Could not retrieve your GitHub account details. Please try again.",
  account_error: "Account setup failed. Please try again or contact support.",
  no_user_info: "Could not retrieve your profile information. Please try again.",
  no_verified_email: "No verified email found on your GitHub account.",
};

type Capability = {
  title: string;
  description: string;
  links?: Array<{
    href: string;
    label: string;
  }>;
};

const CAPABILITIES: Capability[] = [
  {
    title: "Tenant isolation",
    description:
      "Each account runs in its own tenant boundary. Yinshi keeps per-user data separate and runs the sidecar in a dedicated container with dropped Linux capabilities, no-new-privileges, and resource limits. Private repos are supported through a GitHub App integration.",
    links: [
      {
        href: "/architecture.html#container-isolation",
        label: "Container isolation",
      },
      {
        href: "/architecture.html#github-app-integration",
        label: "GitHub App integration",
      },
    ],
  },
  {
    title: "AI agent sessions",
    description:
      "Converse with a coding agent that reads, writes, and refactors code inside your workspace. Every change is tracked on its own branch - review, merge, or discard.",
  },
  {
    title: "Mobile-first interface",
    description:
      "Work from anywhere. The responsive interface adapts from phone to desktop, keeping your agent sessions accessible on any device.",
  },
  {
    title: "Encrypted secrets",
    description:
      "Provider keys and connection secrets are encrypted at rest with AES-256-GCM. Per-user encryption keys are wrapped using HKDF-derived key encryption keys, so stored secrets are not kept as plaintext.",
    links: [
      {
        href: "/architecture.html#encryption-key-management",
        label: "Encryption and key management",
      },
    ],
  },
];

function GitHubIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 .3a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2.1c-3.3.7-4-1.4-4-1.4-.6-1.4-1.4-1.8-1.4-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2-.1-.3-.5-1.5.1-3.2 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0c2.3-1.5 3.3-1.2 3.3-1.2.7 1.7.3 2.9.1 3.2.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A12 12 0 0 0 12 .3Z" />
    </svg>
  );
}

function Navigation() {
  return (
    <header className="landing-header">
      <nav className="landing-nav" aria-label="Primary">
        <a className="landing-brand" href="/" aria-label="Yinshi home">
          <img
            src="/yinshi-scholar.jpg"
            alt="Yinshi Scholar"
            className="landing-brand-image"
            width={40}
            height={42}
          />
          Yinshi
        </a>
        <div className="landing-nav-actions">
          <a
            className="landing-github-link"
            href="https://github.com/codyborders/yinshi"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="GitHub repository"
          >
            <GitHubIcon />
          </a>
          <a className="landing-nav-link" href="/auth/login">
            Sign in
          </a>
        </div>
      </nav>
    </header>
  );
}

function WorkspacePreview() {
  return (
    <figure className="landing-preview" aria-label="Example Yinshi coding workspace">
      <div className="landing-preview-bar">
        <strong>codyborders/yinshi</strong>
        <span className="landing-preview-status">
          <span aria-hidden="true" />
          pi connected
        </span>
      </div>
      <div className="landing-preview-body">
        <aside className="landing-preview-sidebar" aria-label="Example worktree">
          <span aria-hidden="true" className="landing-preview-branch-mark" />
          <code>session/quiet-pine</code>
        </aside>
        <div className="landing-preview-chat">
          <div className="landing-preview-message landing-preview-message-user">
            <span aria-hidden="true">Y</span>
            <p>Add retry feedback when a repository import fails.</p>
          </div>
          <div className="landing-preview-message landing-preview-message-agent">
            <span aria-hidden="true">π</span>
            <code>6 focused tests passed</code>
          </div>
        </div>
      </div>
    </figure>
  );
}

function Hero() {
  return (
    <section className="landing-hero" aria-labelledby="landing-title">
      <div className="landing-hero-text">
        <p className="landing-subtitle">Browser-based coding workspace</p>
        <h1 id="landing-title" className="landing-title">
          Run coding agents against your repositories from any browser.
        </h1>
        <p className="landing-desc">
          Import a GitHub or allowed local repository. Yinshi creates an isolated git worktree,
          connects a pi agent, and streams the session to your browser.
        </p>
        <div className="landing-cta-group">
          <a href="/auth/login" className="landing-cta">
            Start a workspace
          </a>
          <a href="/architecture.html" className="landing-cta landing-cta-secondary">
            Read the architecture
          </a>
        </div>
      </div>
      <WorkspacePreview />
    </section>
  );
}

function TechnicalFoundation() {
  return (
    <section className="landing-foundation" aria-label="Technical foundation">
      <ul>
        <li>
          <span>Workspace isolation</span>
          <strong>One git worktree per session</strong>
        </li>
        <li>
          <span>Agent runtime</span>
          <strong>pi coding agent</strong>
        </li>
        <li>
          <span>Repository access</span>
          <strong>GitHub App or allowed local path</strong>
        </li>
      </ul>
    </section>
  );
}

function Workflow() {
  return (
    <section className="landing-workflow" aria-labelledby="workflow-title">
      <h2 id="workflow-title">From repository to reviewable branch</h2>
      <ol>
        <li>
          <strong>Connect a repository</strong>
          <p>Choose a GitHub repository or an allowed local path available to the server.</p>
        </li>
        <li>
          <strong>Give pi a task</strong>
          <p>Yinshi creates a named worktree and streams pi’s messages, tool calls, and file edits.</p>
        </li>
        <li>
          <strong>Review the branch</strong>
          <p>Inspect the result, then merge or discard the workspace through your existing Git tools.</p>
        </li>
      </ol>
    </section>
  );
}

function Capabilities() {
  return (
    <section className="landing-capabilities" aria-labelledby="capabilities-title">
      <h2 id="capabilities-title" className="landing-section-title">
        Capabilities
      </h2>
      <div className="landing-cap-grid">
        {CAPABILITIES.map((capability) => (
          <article key={capability.title} className="landing-cap-card">
            <h3 className="landing-cap-title">{capability.title}</h3>
            <p className="landing-cap-desc">{capability.description}</p>
            {capability.links ? (
              <div className="landing-cap-links">
                {capability.links.map((link) => (
                  <a key={link.href} href={link.href} className="landing-cap-link">
                    {link.label}
                  </a>
                ))}
              </div>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function FinalAction() {
  return (
    <section className="landing-final">
      <p className="landing-final-text">
        No IDE or app required. Fire up your browser, import your repos and pi configs, and get to
        work.
      </p>
      <a href="/auth/login" className="landing-cta">
        Start a workspace
      </a>
    </section>
  );
}

function Footer() {
  return (
    <footer className="landing-footer">
      <span>Yinshi</span>
      <span className="landing-footer-sep">·</span>
      <span>Code with AI agents, anywhere.</span>
    </footer>
  );
}

export default function Landing() {
  const [searchParams] = useSearchParams();
  const errorCode = searchParams.get("error");
  const errorMessage = errorCode
    ? ERROR_MESSAGES[errorCode] ?? "Something went wrong. Please try again."
    : null;

  return (
    <div className="landing-page">
      <a className="landing-skip-link" href="#landing-main">
        Skip to main content
      </a>
      <Navigation />
      <main id="landing-main" tabIndex={-1}>
        {errorMessage ? (
          <div className="landing-alert" role="alert">
            {errorMessage}
          </div>
        ) : null}
        <Hero />
        <TechnicalFoundation />
        <Workflow />
        <Capabilities />
        <FinalAction />
      </main>
      <Footer />
    </div>
  );
}
