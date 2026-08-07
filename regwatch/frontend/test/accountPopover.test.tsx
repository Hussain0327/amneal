// The account popover (A4 successor): identity + sign-out, and the colophon
// reading from ONE GET /settings via SettingsProvider -- including the two
// honest degraded states (connecting vs unreachable, 401 excepted).
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PublicSettings } from "@/lib/api";

const getPublicSettingsMock = vi.fn<() => Promise<PublicSettings>>();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getPublicSettings: () => getPublicSettingsMock(),
  };
});

const logoutMock = vi.fn(async () => {});
vi.mock("@/components/AuthProvider", () => ({
  useAuth: () => ({
    user: { id: 1, email: "analyst@example.test", display_name: "Analyst", role: "user" },
    loading: false,
    refresh: vi.fn(async () => {}),
    logout: logoutMock,
  }),
}));

import { AccountPopover } from "@/components/AccountPopover";
import { SettingsProvider } from "@/components/SettingsProvider";
import { ApiError } from "@/lib/api";

const SETTINGS: PublicSettings = {
  company_name: "Test Co",
  embedding_provider: "test-embed",
  llm_model: "test-llm",
  llm_provider: "test-provider",
  refusal_score_threshold: 0.3,
  retrieval_top_k: 8,
};

function renderPopover(onClose: () => void = () => {}) {
  return render(
    <SettingsProvider>
      <AccountPopover onClose={onClose} />
    </SettingsProvider>,
  );
}

beforeEach(() => {
  getPublicSettingsMock.mockResolvedValue(SETTINGS);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("account popover -- identity and colophon (A4)", () => {
  it("names the analyst and renders the colophon with exactly one GET /settings", async () => {
    renderPopover();
    expect(screen.getByText("Analyst")).toBeInTheDocument();
    expect(screen.getByText("analyst@example.test")).toBeInTheDocument();
    expect(await screen.findByText(/test-embed/)).toBeInTheDocument();
    expect(screen.getByText("test-provider/test-llm")).toBeInTheDocument();
    // One shared fetch for the whole shell -- two calls means a duplicate.
    expect(getPublicSettingsMock).toHaveBeenCalledTimes(1);
  });

  it("signs out via the auth provider", async () => {
    const user = userEvent.setup();
    renderPopover();
    await user.click(screen.getByText("Sign out"));
    expect(logoutMock).toHaveBeenCalled();
  });

  it("closes on Escape and returns focus to the opener", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderPopover(onClose);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("shows the unreachable notice on a transport failure (non-401)", async () => {
    getPublicSettingsMock.mockRejectedValue(new Error("network down"));
    renderPopover();
    expect(await screen.findByText(/reach RegWatch/)).toBeInTheDocument();
  });

  it("treats a 401 as auth expiry, not unreachability", async () => {
    getPublicSettingsMock.mockRejectedValue(new ApiError(401, "unauthorized"));
    renderPopover();
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
      expect(() => render(<AccountPopover onClose={() => {}} />)).toThrow(
        "useSettings must be used inside <SettingsProvider>",
      );
    } finally {
      window.removeEventListener("error", onWindowError);
      spy.mockRestore();
    }
  });
});
