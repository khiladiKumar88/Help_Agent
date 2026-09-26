import { Component, type ReactNode } from "react";

/** Fail-safe UI: a crashing widget shows a message instead of blanking the whole app. */
export class ErrorBoundary extends Component<{ name: string; children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };

  static getDerivedStateFromError(e: unknown) {
    return { error: e instanceof Error ? e.message : String(e) };
  }

  render() {
    if (this.state.error) {
      return (
        <div role="alert" className="rounded-lg border border-loss/40 bg-loss/10 p-4 text-sm text-loss">
          {this.props.name} failed to render: {this.state.error}
          <button className="ml-3 underline" onClick={() => this.setState({ error: null })}>
            retry
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
