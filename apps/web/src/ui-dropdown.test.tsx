import type { FormEvent } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Dropdown, Pagination, SelectRecords } from "./ui";

afterEach(() => cleanup());

const options = [
  { value: "alpha", label: "Alpha", description: "First archive" },
  { value: "blocked", label: "Blocked", description: "Unavailable archive", disabled: true },
  { value: "omega", label: "Omega", description: "Last archive" },
];

describe("Dropdown", () => {
  it("supports an uncontrolled default and submits the current value through its hidden input", () => {
    const onChange = vi.fn();
    const view = render(<form><Dropdown name="archive" required defaultValue="alpha" aria-label="Choose archive" options={options} onChange={onChange} /></form>);
    const button = screen.getByRole("button", { name: "Choose archive" });

    expect(button).toHaveTextContent("Alpha");
    expect(button).toHaveAttribute("aria-required", "true");
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("option", { name: "Alpha" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("option", { name: "Blocked" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("Unavailable archive")).toBeVisible();

    fireEvent.pointerDown(screen.getByRole("option", { name: "Omega" }));
    fireEvent.click(screen.getByRole("option", { name: "Omega" }));
    expect(onChange).toHaveBeenCalledWith("omega");
    expect(button).toHaveTextContent("Omega");
    expect(button).toHaveFocus();
    expect(new FormData(view.container.querySelector("form")!).get("archive")).toBe("omega");
    fireEvent.reset(view.container.querySelector("form")!);
    expect(button).toHaveTextContent("Alpha");
    expect(new FormData(view.container.querySelector("form")!).get("archive")).toBe("alpha");
  });

  it("blocks required form submission until an option is selected", () => {
    const onSubmit = vi.fn((event: FormEvent) => event.preventDefault());
    const view = render(<form onSubmit={onSubmit}><Dropdown name="archive" required aria-label="Required archive" options={options} /></form>);
    const form = view.container.querySelector("form")!;
    const button = screen.getByRole("button", { name: "Required archive" });

    fireEvent.submit(form);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(button).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent("Select an option.");
    expect(button).toHaveFocus();

    fireEvent.click(button);
    fireEvent.click(screen.getByRole("option", { name: "Alpha" }));
    fireEvent.submit(form);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(button).not.toHaveAttribute("aria-invalid");
  });

  it("keeps controlled values owner-driven and ignores disabled option selection", () => {
    const onChange = vi.fn();
    const view = render(<Dropdown value="alpha" aria-label="Controlled archive" options={options} onChange={onChange} />);
    const button = screen.getByRole("button", { name: "Controlled archive" });

    fireEvent.click(button);
    fireEvent.click(screen.getByRole("option", { name: "Blocked" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(button).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("option", { name: "Omega" }));
    expect(onChange).toHaveBeenCalledWith("omega");
    expect(button).toHaveTextContent("Alpha");
    view.rerender(<Dropdown value="omega" aria-label="Controlled archive" options={options} onChange={onChange} />);
    expect(button).toHaveTextContent("Omega");
  });

  it("disables the control and omits its named value from form data", () => {
    const view = render(<form><Dropdown name="archive" disabled defaultValue="alpha" aria-label="Disabled archive" options={options} /></form>);
    expect(screen.getByRole("button", { name: "Disabled archive" })).toBeDisabled();
    expect(new FormData(view.container.querySelector("form")!).get("archive")).toBeNull();
  });

  it("navigates enabled options and returns focus with keyboard controls", () => {
    render(<Dropdown aria-label="Keyboard archive" placeholder="Choose one" options={options} />);
    const button = screen.getByRole("button", { name: "Keyboard archive" });
    button.focus();

    fireEvent.keyDown(button, { key: "ArrowDown" });
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button.getAttribute("aria-activedescendant")).toMatch(/option-0$/);
    fireEvent.keyDown(button, { key: "ArrowDown" });
    expect(button.getAttribute("aria-activedescendant")).toMatch(/option-2$/);
    fireEvent.keyDown(button, { key: "Enter" });
    expect(button).toHaveTextContent("Omega");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveFocus();

    fireEvent.keyDown(button, { key: "Home" });
    expect(button.getAttribute("aria-activedescendant")).toMatch(/option-0$/);
    fireEvent.keyDown(button, { key: "End" });
    expect(button.getAttribute("aria-activedescendant")).toMatch(/option-2$/);
    fireEvent.keyDown(button, { key: "Escape" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveFocus();

    fireEvent.keyDown(button, { key: " " });
    expect(button).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(button, { key: "ArrowUp" });
    expect(button.getAttribute("aria-activedescendant")).toMatch(/option-0$/);
    fireEvent.keyDown(button, { key: " " });
    expect(button).toHaveTextContent("Alpha");
    expect(button).toHaveFocus();
  });

  it("closes on outside pointer interaction and when focus leaves", () => {
    render(<><Dropdown aria-label="Closable archive" options={options} /><button type="button">After dropdown</button></>);
    const button = screen.getByRole("button", { name: "Closable archive" });
    fireEvent.click(button);
    fireEvent.pointerDown(document.body);
    expect(button).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(button);
    fireEvent.blur(button, { relatedTarget: screen.getByRole("button", { name: "After dropdown" }) });
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("uses the shared popup control for records and page size without native single selects", () => {
    const onPageSize = vi.fn();
    const view = render(<><SelectRecords name="record" records={[{ id: "one", name: "Record one" }]} required /><Pagination page={1} pageSize={50} total={120} onPage={() => undefined} onPageSize={onPageSize} /></>);

    expect(view.container.querySelector("select")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Items per page" }));
    fireEvent.click(screen.getByRole("option", { name: "100" }));
    expect(onPageSize).toHaveBeenCalledWith(100);
  });
});
