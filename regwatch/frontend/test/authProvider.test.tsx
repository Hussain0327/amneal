// AuthProvider's re-validation contract: a transport failure must NEVER be read
// as "signed out". refresh() runs on mount, on window focus (throttled), and on a
// cross-tab broadcast, so a single bad hop during a rolling deploy used to unmount
// the authed subtree and bounce a working analyst to /login with a live cookie --
// losing composer text and any unsaved white-paper cell on the way.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { User } from "@/lib/api";

// Only me() is stubbed; ApiError stays the real class the provider instanceof-checks.
const meMock = vi.fn<() => Promise<User>>();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, me: () => meMock() };
});

const routerReplace = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ({ replace: routerReplace }),
}));

import { AuthProvider, useAuth } from "@/components/AuthProvider";
import { ApiError } from "@/lib/api";

const USER: User = {
  id: 1,
  email: "analyst@example.test",
  display_name: "Analyst",
  role: "analyst",
};

// Rendered only while a user is set (pathname "/" is not a BARE_PATH), so its
// presence IS the assertion that the provider still considers us signed in. The
// button gives a deterministic re-validation lever -- no fake timers, no focus or
// BroadcastChannel plumbing.
function Probe() {
  const { user, refresh } = useAuth();
  return (
    <div>
      <span data-testid="who">{user ? user.email : "none"}</span>
      <button onClick={() => void refresh()}>revalidate</button>
    </div>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("AuthProvider re-validation", () => {
  it("keeps a signed-in analyst when /auth/me fails on transport, not auth", async () => {
    const user = userEvent.setup();
    meMock.mockResolvedValueOnce(USER);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    expect(await screen.findByText("analyst@example.test")).toBeInTheDocument();

    // An edge 502 mid-session: says nothing about the cookie.
    meMock.mockRejectedValueOnce(new ApiError(502, "upstream unavailable"));
    await user.click(screen.getByRole("button", { name: "revalidate" }));

    await waitFor(() => expect(meMock).toHaveBeenCalledTimes(2));
    // Still authed: children mounted, no QuietShell, no bounce.
    expect(screen.getByTestId("who")).toHaveTextContent("analyst@example.test");
    expect(screen.queryByText("verifying session")).not.toBeInTheDocument();
    expect(routerReplace).not.toHaveBeenCalled();
  });

  it("keeps a signed-in analyst through the 504 the client's own timeout produces", async () => {
    const user = userEvent.setup();
    meMock.mockResolvedValueOnce(USER);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    expect(await screen.findByText("analyst@example.test")).toBeInTheDocument();

    // fetchWithTimeout maps a fired timer to ApiError(504) -- NOT a TypeError, which
    // is why "is it an ApiError" would have been the wrong discriminator.
    meMock.mockRejectedValueOnce(new ApiError(504, "The request timed out"));
    await user.click(screen.getByRole("button", { name: "revalidate" }));

    await waitFor(() => expect(meMock).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("who")).toHaveTextContent("analyst@example.test");
    expect(routerReplace).not.toHaveBeenCalled();
  });

  it("still signs out on a genuine 401", async () => {
    const user = userEvent.setup();
    meMock.mockResolvedValueOnce(USER);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    expect(await screen.findByText("analyst@example.test")).toBeInTheDocument();

    // me() is stubbed, so handle()'s onUnauthorized hook never runs here -- this
    // pins the provider's OWN 401 branch, i.e. that we narrowed it rather than
    // deleting it.
    meMock.mockRejectedValueOnce(new ApiError(401, "authentication required"));
    await user.click(screen.getByRole("button", { name: "revalidate" }));

    await waitFor(() => expect(screen.queryByTestId("who")).not.toBeInTheDocument());
    expect(screen.getByText("verifying session")).toBeInTheDocument();
    // ?reason=expired, not a bare /login: this analyst HAD a session, so the
    // sign-in form owes them an explanation for why they are looking at it.
    await waitFor(() => expect(routerReplace).toHaveBeenCalledWith("/login?reason=expired"));
  });

  it("sends a never-authenticated visitor to a bare /login", async () => {
    // No prior success: hadUser stays false, so nothing claims a session ended.
    meMock.mockRejectedValueOnce(new ApiError(401, "authentication required"));

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() => expect(routerReplace).toHaveBeenCalledWith("/login"));
    expect(routerReplace).not.toHaveBeenCalledWith("/login?reason=expired");
  });
});
