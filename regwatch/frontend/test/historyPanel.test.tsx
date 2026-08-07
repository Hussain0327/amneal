// The history docket (D16 successor): day-bucketed groups, title filtering,
// quiet rows (visible time only; the count reaches AT via a sr-only tail and
// sighted users via the hover title), and the never-silent inline delete flow
// carried over from the old sidebar.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SessionSummary } from "@/lib/api";

// The factories only CALL these at runtime (after vi.mock hoists) -- same
// partial-mock pattern as askPage.test.tsx.
const deleteSessionMock = vi.fn<(id: string) => Promise<void>>();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    deleteSession: (id: string) => deleteSessionMock(id),
  };
});

// Three rows: one fresh (buckets under Today, relative time in minutes), one
// months old (deterministic absolute title date + a month-label group), and
// one whose timestamp does not parse (buckets under Earlier, no time shown).
const FRESH_ISO = new Date(Date.now() - 5 * 60000).toISOString();
const SESSIONS: SessionSummary[] = [
  {
    id: "s1",
    title: "albuterol BE questions",
    message_count: 7,
    created_at: FRESH_ISO,
    updated_at: FRESH_ISO,
  },
  {
    id: "s2",
    title: "metformin dissolution",
    message_count: 2,
    created_at: "2026-01-05T12:00:00Z",
    updated_at: "2026-01-05T12:00:00Z",
  },
  {
    id: "s3",
    title: "broken clock",
    message_count: 3,
    created_at: "not-a-date",
    updated_at: "not-a-date",
  },
];

const refreshMock = vi.fn(async () => {});
const setActiveSessionIdMock = vi.fn();
vi.mock("@/components/SessionsProvider", () => ({
  useSessions: () => ({
    sessions: SESSIONS,
    loaded: true,
    activeSessionId: null,
    setActiveSessionId: setActiveSessionIdMock,
    refresh: refreshMock,
  }),
}));

vi.mock("@/components/CurrentProductProvider", () => ({
  useCurrentProduct: () => ({
    referenceProductName: "",
    applicationNumber: "",
    hasProduct: false,
    setProduct: vi.fn(),
    clearProduct: vi.fn(),
    productParams: "",
  }),
}));

const routerReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace }),
  usePathname: () => "/",
}));

// Plain anchor stand-in: navigation is not under test, only the row markup.
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

import { HistoryPanel } from "@/components/HistoryPanel";

function renderPanel(onClose: () => void = () => {}) {
  return render(<HistoryPanel onClose={onClose} />);
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("history rows -- quiet time, spelled-out counts", () => {
  it("shows only the relative time visibly, with the count in a sr-only tail", () => {
    const { baseElement } = renderPanel();
    const times = baseElement.querySelectorAll(".hist__time");
    expect(times).toHaveLength(3);
    expect(times[0].textContent).toContain("5m");
    // The visible row stays quiet: no message-count clutter beside the time.
    expect(times[0].childNodes[0]?.textContent).not.toContain("7");
    expect(times[0].querySelector(".sr-only")?.textContent).toBe(" — 7 messages");
    expect(times[1].querySelector(".sr-only")?.textContent).toBe(" — 2 messages");
  });

  it("renders no time at all when the timestamp does not parse", () => {
    const { baseElement } = renderPanel();
    const broken = baseElement.querySelectorAll(".hist__time")[2];
    expect(broken.querySelector(".sr-only")?.textContent).toBe(" — 3 messages");
    // Everything visible in the cell is the sr-only tail; nothing dangles.
    expect(broken.textContent).toBe(" — 3 messages");
  });

  it("titles each row with title, absolute date, and message count", () => {
    renderPanel();
    expect(
      screen.getByTitle("metformin dissolution · Jan 5, 2026 · 2 messages"),
    ).toBeInTheDocument();
  });
});

describe("day buckets and filtering", () => {
  it("groups rows under local-calendar day labels", () => {
    const { baseElement } = renderPanel();
    const labels = Array.from(baseElement.querySelectorAll(".histpanel__day")).map(
      (el) => el.textContent,
    );
    expect(labels[0]).toBe("Today");
    expect(labels).toContain("January 2026");
    // An unparseable timestamp still files somewhere reachable.
    expect(labels).toContain("Earlier");
  });

  it("filters by title and states an empty match honestly", async () => {
    const user = userEvent.setup();
    const { baseElement } = renderPanel();
    const search = screen.getByLabelText("Filter conversations");
    await user.type(search, "metformin");
    expect(baseElement.querySelectorAll(".hist")).toHaveLength(1);
    expect(screen.getByText("metformin dissolution")).toBeInTheDocument();
    await user.clear(search);
    await user.type(search, "zzz");
    expect(baseElement.querySelectorAll(".hist")).toHaveLength(0);
    expect(screen.getByText("No conversations match.")).toBeInTheDocument();
  });
});

describe("panel chrome", () => {
  it("closes on Escape and on the close button", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderPanel(onClose);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    await user.click(screen.getByLabelText("Close history"));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("starts a new chat: clears the active session and closes", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderPanel(onClose);
    await user.click(screen.getByText("+ New chat"));
    expect(setActiveSessionIdMock).toHaveBeenCalledWith(null);
    expect(onClose).toHaveBeenCalled();
  });

  it("selecting a conversation activates it and closes the panel", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderPanel(onClose);
    await user.click(screen.getByText("albuterol BE questions"));
    expect(setActiveSessionIdMock).toHaveBeenCalledWith("s1");
    expect(onClose).toHaveBeenCalled();
  });
});

describe("history delete failure -- never silent (D16)", () => {
  it("surfaces a failed delete inline, retry re-fires, dismiss clears", async () => {
    const user = userEvent.setup();
    deleteSessionMock.mockRejectedValue(new Error("boom"));
    renderPanel();

    await user.click(screen.getByLabelText('Delete conversation "albuterol BE questions"'));
    await user.click(screen.getByText("yes"));

    const failure = await screen.findByRole("alert");
    expect(failure.textContent).toContain("couldn't delete");
    expect(deleteSessionMock).toHaveBeenCalledTimes(1);
    expect(deleteSessionMock).toHaveBeenCalledWith("s1");

    // Retry re-fires the same delete; still failing, so the row returns.
    await user.click(screen.getByText("retry"));
    await waitFor(() => expect(deleteSessionMock).toHaveBeenCalledTimes(2));
    expect(deleteSessionMock).toHaveBeenLastCalledWith("s1");
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    // Dismiss stands down: the failure row clears and the normal row is back.
    await user.click(screen.getByText("dismiss"));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("albuterol BE questions")).toBeInTheDocument();
  });

  it("still refreshes the session list after a successful delete", async () => {
    const user = userEvent.setup();
    deleteSessionMock.mockResolvedValue(undefined);
    renderPanel();

    await user.click(screen.getByLabelText('Delete conversation "metformin dissolution"'));
    await user.click(screen.getByText("yes"));

    await waitFor(() => expect(deleteSessionMock).toHaveBeenCalledWith("s2"));
    await waitFor(() => expect(refreshMock).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
