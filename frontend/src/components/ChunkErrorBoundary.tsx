import React from "react";

interface State {
  hasError: boolean;
}

/**
 * Catches errors thrown by React.lazy() when a code-split chunk fails to
 * load (network error, deploy mismatch, etc.) and renders a retry prompt
 * instead of crashing the entire app to a white screen.
 */
export default class ChunkErrorBoundary extends React.Component<
  React.PropsWithChildren,
  State
> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  handleRetry = () => {
    this.setState({ hasError: false });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: "2rem", textAlign: "center" }}>
          <p>Something went wrong loading this page.</p>
          <button onClick={this.handleRetry} style={{ marginTop: "1rem" }}>
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
