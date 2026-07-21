import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DocsScreen } from "./docs-screen";

afterEach(cleanup);

describe("Docs screen", () => {
  it("keeps operator and agent guidance available inside the application", () => {
    render(<DocsScreen />);

    expect(screen.getByRole("heading", { level: 1, name: "Docs" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Operator workflows" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Command line" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Agent contract" })).toBeInTheDocument();
    expect(screen.getByText("mfiche --json capabilities", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Run readiness checks" })).toHaveAttribute("href", "#/readiness");
    expect(screen.getByRole("link", { name: /CLI guide/ })).toHaveAttribute("href", "https://github.com/latentwill/modelfiche/blob/main/docs/CLI.md");
  });
});
