import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DocsScreen } from "./docs-screen";

afterEach(cleanup);

describe("Docs screen", () => {
  it("links operators to readiness and the CLI reference", () => {
    render(<DocsScreen />);

    expect(screen.getByRole("link", { name: "Run readiness checks" })).toHaveAttribute("href", "#/readiness");
    expect(screen.getByRole("link", { name: /CLI guide/ })).toHaveAttribute("href", "https://github.com/latentwill/modelfiche/blob/main/docs/CLI.md");
  });
});
