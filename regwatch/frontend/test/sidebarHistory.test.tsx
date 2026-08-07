// Sidebar history rows (D16) + the shared settings context (A4): message
// counts and absolute dates on rows, an inline never-silent delete failure,
// and the colophon reading from ONE GET /settings via SettingsProvider.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PublicSettings, SessionSummary } from "@/lib/api";

// The factories only CALL these at runtime (after vi.mock hoists) -- same
// partial-mock pattern as askPage.test.tsx. ApiError stays real.
const deleteSessionMock = vi.fn<(id: string) => Promise<void>>();
const getPublicSettingsMock = vi.fn<() => Promise<PublicSettings>>();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    deleteSession: (id: string) => deleteSessionMock(id),
    getPublicSettings: () => getPublicSettingsMock(),
  };
});

const SETTINGS: PublicSettings = {
  company_name: "Test Co",
  embedding_provider: "test-embed",
  llm_model: "test-llm",
  llm_provider: "test-provider",
  refusal_score_threshold: 0.3,
  retrieval_top_k: 8,
};

// Three rows: one fresh (relative time renders in minutes), one months old
// (its absolute title date is the deterministic part under test), and one
// whose timestamp does not parse (the count must render alone).
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

vi.mock("@/components/AuthProvider", () => ({
  useAuth: () => ({
    user: { id: 1, email: "analyst@example.test", display_name: "Analyst", role: "user" },
    loading: false,
    refresh: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
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

import { SettingsProvider } from "@/components/SettingsProvider";
import { Sidebar } from "@/components/Sidebar";
import { ApiError } from "@/lib/api";

function renderSidebar() {
  return render(
    <SettingsProvider>
      <Sidebar />
    </SettingsProvider>,
  );
}

beforeEach(() => {
  getPublicSettingsMock.mockResolvedValue(SETTINGS);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("Sidebar history rows -- counts and dates (D16)", () => {
  it("shows the message count beside the relative time, spelled out for AT", () => {
    const { container } = renderSidebar();
    const times = container.querySelectorAll(".hist__time");
    expect(times).toHaveLength(3);
    expect(times[0].textContent).toContain("5m");
    expect(times[0].textContent).toContain("\u00b7 7");
    // aria-label on a generic span is prohibited ARIA: the visual count is
    // aria-hidden and a sr-only tail spells it out instead.
    expect(times[0].getAttribute("aria-label")).toBeNull();
    expect(times[0].querySelector('[aria-hidden="true"]')?.textContent).toContain("7");
    expect(times[0].querySelector(".sr-only")?.textContent).toBe(" \u2014 7 messages");
    // The bare visible count is never opaque to a screen reader.
    expect(times[1].querySelector(".sr-only")?.textContent).toBe(" \u2014 2 messages");
  });

  it("renders the count alone when the timestamp does not parse (no orphaned separator)", () => {
    const { container } = renderSidebar();
    const broken = container.querySelectorAll(".hist__time")[2];
    expect(broken.textContent).not.toContain("\u00b7");
    expect(broken.querySelector('[aria-hidden="true"]')?.textContent).toBe("3");
    expect(broken.querySelector(".sr-only")?.textContent).toBe(" \u2014 3 messages");
  });

  it("titles each row with title, absolute date, and message count", () => {
    renderSidebar();
    expect(
      screen.getByTitle("metformin dissolution \u00b7 Jan 5, 2026 \u00b7 2 messages"),
    ).toBeInTheDocument();
  });
});

describe("Sidebar history delete failure -- never silent (D16)", () => {
  it("surfaces a failed delete inline, retry re-fires, dismiss clears", async () => {
    const user = userEvent.setup();
    deleteSessionMock.mockRejectedValue(new Error("boom"));
    renderSidebar();

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
    renderSidebar();

    await user.click(screen.getByLabelText('Delete conversation "metformin dissolution"'));
    await user.click(screen.getByText("yes"));

    await waitFor(() => expect(deleteSessionMock).toHaveBeenCalledWith("s2"));
    await waitFor(() => expect(refreshMock).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("Sidebar colophon via SettingsProvider (A4)", () => {
  it("renders the colophon from context with exactly one GET /settings", async () => {
    renderSidebar();
    expect(await screen.findByText(/test-embed/)).toBeInTheDocument();
    expect(screen.getByText("test-provider/test-llm")).toBeInTheDocument();
    // The refactor's point: the Sidebar no longer fires its own fetch, so the
    // whole tree costs one request. Two calls means the duplicate came back.
    expect(getPublicSettingsMock).toHaveBeenCalledTimes(1);
  });

  it("shows the unreachable notice on a transport failure (non-401)", async () => {
    getPublicSettingsMock.mockRejectedValue(new Error("network down"));
    renderSidebar();
    expect(await screen.findByText(/reach RegWatch/)).toBeInTheDocument();
  });

  it("treats a 401 as auth expiry, not unreachability", async () => {
    getPublicSettingsMock.mockRejectedValue(new ApiError(401, "unauthorized"));
    renderSidebar();
    // Stays on the quiet connecting state; the central handler owns the 401.
    expect(await screen.findByText(/connecting/)).toBeInTheDocument();
    expect(screen.queryByText(/reach RegWatch/)).toBeNull();
  });

  it("useSettings throws outside the provider (the useSessions contract)", () => {
    // The expected render throw is noisy: React logs via console.error and
    // jsdom re-reports it as an uncaught window error. Silence both channels
    // for this one assertion.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const onWindowError = (e: ErrorEvent): void => e.preventDefault();
    window.addEventListener("error", onWindowError);
    try {
      expect(() => render(<Sidebar />)).toThrow(
        "useSettings must be used inside <SettingsProvider>",
      );
    } finally {
      window.removeEventListener("error", onWindowError);
      spy.mockRestore();
    }
  });
});
