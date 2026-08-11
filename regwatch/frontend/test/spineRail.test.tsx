// The spine rail: two studios reachable by name (aria-labels on lettered
// marks), scope carried on the href that reads it, the active stop resolved by
// prefix so a kind query or a nested artifact path still lights its room, and
// the history/account panels wired to their toggles. Nav structure was
// previously test-invisible; the rail's stops are load-bearing now, so they are
// pinned here -- including what is NOT on the rail any more.
import { render, screen, within } from "@testing-library/react";
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

// Mutable so a case can put the rail on a nested path; usePathname never sees
// the query string, which is exactly why the active check has to be a prefix.
let pathname = "/";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => pathname,
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

const RESEARCH = "Research Studio - Ask, assemble, watch, publish";
const COMPLIANCE = "Compliance Studio - Review and check our drafts";

afterEach(() => {
  vi.clearAllMocks();
  reachable = true;
  pathname = "/";
});

describe("spine rail -- two studios by name", () => {
  it("lists exactly the two studios with full accessible names", () => {
    render(<SpineRail />);
    const nav = screen.getByRole("navigation", { name: "Surfaces" });
    expect(nav).toBeInTheDocument();
    expect(screen.getByRole("link", { name: RESEARCH })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COMPLIANCE })).toBeInTheDocument();
    expect(within(nav).getAllByRole("link")).toHaveLength(2);
  });

  it("carries scope to Research, which reads it", () => {
    render(<SpineRail />);
    expect(screen.getByRole("link", { name: RESEARCH })).toHaveAttribute(
      "href",
      "/research?rp=albuterol%20sulfate&appl=020503",
    );
  });

  it("links the Compliance Studio as its own surface (no scope params -- it reads none)", () => {
    render(<SpineRail />);
    expect(screen.getByRole("link", { name: COMPLIANCE })).toHaveAttribute("href", "/studio");
  });

  it("drops the deficiency stop and the chapter numerals with it", () => {
    render(<SpineRail />);
    expect(screen.queryByRole("link", { name: /deficiency/i })).toBeNull();
    // 01-05 encoded a sequence that no longer exists; no stop may be numbered.
    expect(screen.queryByRole("link", { name: /^0\d\b/ })).toBeNull();
  });
});

describe("spine rail -- which room am I in", () => {
  it("marks Research active at the root, where Ask still lives", () => {
    render(<SpineRail />);
    expect(screen.getByRole("link", { name: RESEARCH })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: COMPLIANCE })).not.toHaveAttribute("aria-current");
  });

  it("keeps Research active on a nested research path", () => {
    pathname = "/research/thread-42";
    render(<SpineRail />);
    expect(screen.getByRole("link", { name: RESEARCH })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: COMPLIANCE })).not.toHaveAttribute("aria-current");
  });

  it("marks only Compliance active inside the studio", () => {
    pathname = "/studio/psg-0042";
    render(<SpineRail />);
    expect(screen.getByRole("link", { name: COMPLIANCE })).toHaveAttribute("aria-current", "page");
    // The root belongs to Research, but "/" is a prefix of every path and must
    // never be allowed to light a second stop.
    expect(screen.getByRole("link", { name: RESEARCH })).not.toHaveAttribute("aria-current");
  });
});

describe("spine rail -- the analyst's own things", () => {
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
