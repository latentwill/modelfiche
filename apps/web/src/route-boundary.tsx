import { Component, type ReactNode } from "react";
import { Page, Panel } from "./ui";

export class RouteBoundary extends Component<{ route: string; children: ReactNode }, { failed: boolean; route: string }> {
  state = { failed: false, route: this.props.route };

  static getDerivedStateFromError() { return { failed: true }; }
  static getDerivedStateFromProps(props: { route: string }, state: { route: string }) {
    return props.route !== state.route ? { failed: false, route: props.route } : null;
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <Page title="This view could not open"><Panel>
      <p role="alert">Modelfiche could not display this view. Reload it to try again, or return to the dashboard.</p>
      <div className="actions"><button type="button" onClick={() => location.reload()}>Reload view</button><a className="button" href="#/dashboard">Return to dashboard</a></div>
    </Panel></Page>;
  }
}
