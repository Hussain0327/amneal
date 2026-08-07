// The spine rail: every surface reachable by name (aria-labels on numeral
// stops), Studio linked, scope carried on hrefs, and the history/account
// panels wired to their toggles. Nav structure was previously test-invisible;
// the rail's stops are load-bearing now, so they are pinned here.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const setActiveSessionIdMock = vi.fn();
vi.mock("@/components/SessionsProvider", () => ({
  useSessions: () => ({
    sessions: [],
    loaded: true,
    activeSessionId: null,
    setActiveSessionId: setActiveSessionIdMock,
    refresh: vi.fn(async () => {}),
  }),
}));

// Hook-level mock (not the real provider): the rail reads settings only for
// the reachability dot, and the popover only for the colophon states.
let reachable = true;
vi.mock("@/components/SettingsProvider", () => ({
  useSettings: () => ({ settings: null, reachable }),
}));

vi.mock("@/components/AuthProvider", () => ({
  useAuth: () => ({
    user: { id: 1, email: "analyst@example.test", display_name: "Raja Hussain", role: "user" },
    loading: false,
    refresh: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
  }),
}));

vi.mock("@/components/CurrentProductProvider", () => ({
  useCurrentProduct: () => ({
    referenceProductName: "albuterol sulfate",
    applicationNumber: "020503",
    hasProduct: true,
    setProduct: vi.fn(),
    clearProduct: vi.fn(),
    productParams: "rp=albuterol%20sulfate&appl=020503",
  }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/",
}));

vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: React.PropsWithChildren<{ href: string } & React.AnchorHTMLAttributes<HTMLAnchorElement>>) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

import { SpineRail } from "@/components/SpineRail";

afterEach(() => {
  vi.clearAllMocks();
  reachable = true;
});

describe("spine rail -- surfaces by name", () => {
  it("lists the five chapters with full accessible names and carries scope", () => {
    render(<SpineRail />);
    const nav = screen.getByRole("navigation", { name: "Surfaces" });
    expect(nav).toBeInTheDocument();
    const ask = screen.getByRole("link", { name: "01 Ask — Cited Q&A" });
    expect(ask).toHaveAttribute("href", "/?rp=albuterol%20sulfate&appl=020503");
    expect(ask).toHaveAttribute("aria-current", "page");
    expect(
      screen.getByRole("link", { name: "02 Assemble — Build a dossier" }),
    ).toHaveAttribute("href", "/assemble?rp=albuterol%20sulfate&appl=020503");
    expect(screen.getByRole("link", { name: "03 Watch — Change feed" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "04 White Paper — Populate & cite" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "05 Deficiency — Scan a draft" })).toBeInTheDocument();
  });

  it("links Studio as its own surface (no scope params -- it reads none)", () => {
    render(<SpineRail />);
    expect(screen.getByRole("link", { name: /Studio/ })).toHaveAttribute("href", "/studio");
  });

  it("opens and closes the history docket from its toggle", async () => {
    const user = userEvent.setup();
    render(<SpineRail />);
    const toggle = screen.getByRole("button", { name: "History" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(screen.getByRole("dialog", { name: "Conversation history" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "History" })).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Conversation history" })).toBeNull();
  });

  it("opens the account popover and flags unreachability on the stop itself", async () => {
    reachable = false;
    const user = userEvent.setup();
    render(<SpineRail />);
    const account = screen.getByRole("button", { name: /Raja Hussain.*unreachable/i });
    await user.click(account);
    expect(screen.getByRole("dialog", { name: "Account and colophon" })).toBeInTheDocument();
  });

  it("clears the active session when starting a new chat from the rail", async () => {
    const user = userEvent.setup();
    render(<SpineRail />);
    await user.click(screen.getByRole("link", { name: "New chat" }));
    expect(setActiveSessionIdMock).toHaveBeenCalledWith(null);
  });
});
