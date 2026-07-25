import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

/**
 * Catches render-time crashes so one broken view cannot blank the whole app.
 *
 * This is the last line — recoverable failures (a rejected request, a missing
 * node) belong in the view's own error handling and the toast surface. Only an
 * unhandled render error should reach here.
 */

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Shown instead of the default panel. Receives the caught error. */
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // No telemetry in a local single-user app; the console is the log.
    console.error("Unhandled render error", error, info.componentStack);
  }

  /** Clear the caught error and try rendering the children again. */
  private reset = (): void => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error, this.reset);

    return (
      <div className="nd-crash" role="alert">
        <h2 className="nd-crash__title">This view stopped rendering</h2>
        <p>
          The error below came from the UI, not from your data — nothing was written. Try again, or
          reload the page.
        </p>
        <pre className="nd-crash__detail">{error.stack ?? error.message}</pre>
        <div>
          <button type="button" className="nd-button" onClick={this.reset}>
            Try again
          </button>
        </div>
      </div>
    );
  }
}
