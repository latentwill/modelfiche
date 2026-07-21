import { useRef, useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DataTable, Notice, useFocusWorkspace } from "./ui";

afterEach(() => cleanup());

function FocusWorkspaceHarness() {
  const [active, setActive] = useState(false);
  const containerRef = useRef<HTMLElement>(null);
  const initialFocusRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  useFocusWorkspace({
    active,
    onDismiss: () => setActive(false),
    containerRef,
    initialFocusRef,
    restoreFocusRef: openerRef,
  });

  return <div>
    <section data-testid="background">
      <button ref={openerRef} type="button" onClick={() => setActive(true)}>Open review</button>
      <button type="button">Background action</button>
    </section>
    {active && <section ref={containerRef} role="dialog" aria-modal="true" aria-labelledby="review-title" aria-describedby="review-description">
      <h2 id="review-title">Image review</h2>
      <p id="review-description">Inspect the selected image.</p>
      <button type="button" style={{ display: "none" }}>Hidden leading action</button>
      <button ref={initialFocusRef} type="button">First review action</button>
      <button type="button">Last review action</button>
      <button type="button" style={{ display: "none" }}>Hidden trailing action</button>
    </section>}
  </div>;
}

describe("shared accessibility primitives", () => {
  it("inerts the background, contains Tab, closes on Escape, and restores the opener", async () => {
    render(<FocusWorkspaceHarness />);
    const opener = screen.getByRole("button", { name: "Open review" });
    opener.focus();
    fireEvent.click(opener);

    const dialog = await screen.findByRole("dialog", { name: "Image review" });
    const background = screen.getByTestId("background") as HTMLElement & { inert?: boolean };
    const first = screen.getByRole("button", { name: "First review action" });
    const last = screen.getByRole("button", { name: "Last review action" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleDescription("Inspect the selected image.");
    expect(background.inert).toBe(true);
    expect(background).toHaveAttribute("aria-hidden", "true");
    expect(first).toHaveFocus();

    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();

    fireEvent.keyDown(last, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(background.inert).toBe(false);
    expect(background).not.toHaveAttribute("aria-hidden");
    expect(opener).toHaveFocus();
  });

  it("prioritizes one alert and distinguishes loading from an announced empty state", () => {
    const view = render(<Notice error="Request failed" loading empty />);
    expect(screen.getByRole("alert")).toHaveTextContent("Request failed");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    view.rerender(<Notice loading empty />);
    const loading = screen.getByRole("status", { name: "Loading" });
    expect(loading).toHaveAttribute("aria-live", "polite");
    expect(loading).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("No records yet")).not.toBeInTheDocument();

    view.rerender(<Notice empty emptyText="No matching records" emptyHint="Change a filter to continue." />);
    const empty = screen.getByRole("status");
    expect(empty).toHaveAttribute("aria-live", "polite");
    expect(empty).not.toHaveAttribute("aria-busy");
    expect(empty).toHaveTextContent("No matching records");
    expect(empty).toHaveTextContent("Change a filter to continue.");
  });

  it("uses a native row action without making table rows synthetic links", () => {
    const row = { id: "record-1", name: "Archive one", status: "ready" };
    const onRow = vi.fn();
    render(<DataTable rows={[row]} columns={[["name", "Name"], ["status", "Status"]]} onRow={onRow} />);

    expect(screen.getByRole("region", { name: "Scrollable data table" })).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    const action = screen.getByRole("button", { name: "Archive one" });
    fireEvent.click(action);
    expect(onRow).toHaveBeenCalledWith(row);
    expect(action.closest("tr")).not.toHaveAttribute("tabindex");
  });
});
