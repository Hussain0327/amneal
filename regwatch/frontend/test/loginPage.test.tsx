// The sign-in form's failure contract. Every branch here corresponds to a way
// the page used to fail an analyst in production:
//   - a blank field returned silently, so the button did nothing at all;
//   - anything that was not a 401 or 429 printed err.message verbatim, which
//     rendered the proxy's own "Internal Server Error" inside the form;
//   - an expired session dropped the analyst on a page identical to a cold visit.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { User } from "@/lib/api";

// login() and me() are stubbed; ApiError stays the real class the page
// instanceof-checks, so a broken narrowing fails these tests rather than
// passing on a look-alike.
const meMock = vi.fn<() => Promise<User>>();
const loginMock = vi.fn<(email: string, password: string) => Promise<User>>();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    me: () => meMock(),
    login: (email: string, password: string) => loginMock(email, password),
  };
});

const routerReplace = vi.fn();
let queryString = "";
vi.mock("next/navigation", () => ({
  usePathname: () => "/login",
  useRouter: () => ({ replace: routerReplace }),
  useSearchParams: () => new URLSearchParams(queryString),
}));

import { AuthProvider } from "@/components/AuthProvider";
import { ApiError } from "@/lib/api";
import LoginPage from "@/app/login/page";

const USER: User = {
  id: 1,
  email: "analyst@example.test",
  display_name: "Analyst",
  role: "analyst",
};

const GENERIC_FAILURE = "Sign-in failed. Try again in a moment.";

// Renders the page signed out and waits for the form, so every test starts from
// the state a real visitor sees rather than from the QuietShell.
async function renderSignedOut(): Promise<ReturnType<typeof userEvent.setup>> {
  const user = userEvent.setup();
  meMock.mockRejectedValueOnce(new ApiError(401, "authentication required"));
  render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
  await screen.findByRole("button", { name: "Sign in" });
  return user;
}

async function submitWith(user: ReturnType<typeof userEvent.setup>, email: string, password: string): Promise<void> {
  if (email) await user.type(screen.getByLabelText("Email"), email);
  if (password) await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  queryString = "";
});

describe("sign-in form validation", () => {
  it("names the missing email instead of doing nothing", async () => {
    const user = await renderSignedOut();

    await submitWith(user, "", "hunter2hunter2");

    expect(screen.getByText("Enter your email.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    expect(loginMock).not.toHaveBeenCalled();
  });

  it("names the missing password and moves the caret to it", async () => {
    const user = await renderSignedOut();

    await submitWith(user, "analyst@amneal.com", "");

    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveFocus();
    expect(loginMock).not.toHaveBeenCalled();
  });

  it("clears a field's error as soon as the analyst types into it", async () => {
    const user = await renderSignedOut();
    await submitWith(user, "", "hunter2hunter2");
    expect(screen.getByText("Enter your email.")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Email"), "a");

    expect(screen.queryByText("Enter your email.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "false");
  });
});

describe("sign-in failure copy", () => {
  it("reads a 401 as bad credentials", async () => {
    const user = await renderSignedOut();
    loginMock.mockRejectedValueOnce(new ApiError(401, "invalid email or password"));

    await submitWith(user, "analyst@amneal.com", "wrong");

    expect(await screen.findByText("Invalid email or password.")).toBeInTheDocument();
  });

  it("reads a 429 as the rate limit", async () => {
    const user = await renderSignedOut();
    loginMock.mockRejectedValueOnce(new ApiError(429, "rate limit exceeded"));

    await submitWith(user, "analyst@amneal.com", "hunter2hunter2");

    expect(await screen.findByText("Too many attempts. Wait a minute, then try again.")).toBeInTheDocument();
  });

  it("reads a bad hop as unreachable", async () => {
    const user = await renderSignedOut();
    loginMock.mockRejectedValueOnce(new ApiError(504, "The request timed out"));

    await submitWith(user, "analyst@amneal.com", "hunter2hunter2");

    expect(
      await screen.findByText("Can't reach RegWatch right now. Try again in a moment."),
    ).toBeInTheDocument();
  });

  it("never prints the transport's own words", async () => {
    const user = await renderSignedOut();
    loginMock.mockRejectedValueOnce(new ApiError(500, "Internal Server Error"));

    await submitWith(user, "analyst@amneal.com", "hunter2hunter2");

    expect(await screen.findByText(GENERIC_FAILURE)).toBeInTheDocument();
    expect(screen.queryByText(/Internal Server Error/)).not.toBeInTheDocument();
  });

  it("survives a dead network, which rejects with no ApiError at all", async () => {
    const user = await renderSignedOut();
    loginMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    await submitWith(user, "analyst@amneal.com", "hunter2hunter2");

    expect(await screen.findByText(GENERIC_FAILURE)).toBeInTheDocument();
    expect(screen.queryByText(/Failed to fetch/)).not.toBeInTheDocument();
  });

  it("re-enables the button so a failed attempt can be retried", async () => {
    const user = await renderSignedOut();
    loginMock.mockRejectedValueOnce(new ApiError(401, "invalid email or password"));

    await submitWith(user, "analyst@amneal.com", "wrong");

    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled());
  });
});

describe("sign-in success", () => {
  it("trims the email and leaves for the app", async () => {
    const user = await renderSignedOut();
    loginMock.mockResolvedValueOnce(USER);
    meMock.mockResolvedValueOnce(USER);

    await submitWith(user, "  analyst@amneal.com  ", "hunter2hunter2");

    await waitFor(() => expect(loginMock).toHaveBeenCalledWith("analyst@amneal.com", "hunter2hunter2"));
    await waitFor(() => expect(routerReplace).toHaveBeenCalledWith("/"));
  });
});

describe("expired-session notice", () => {
  it("explains itself when the gate bounced a live session", async () => {
    queryString = "reason=expired";
    await renderSignedOut();

    expect(screen.getByText("Your session ended. Sign in to pick up where you left off.")).toBeInTheDocument();
  });

  it("stays quiet on a cold visit", async () => {
    await renderSignedOut();

    expect(screen.queryByText(/Your session ended/)).not.toBeInTheDocument();
  });
});
