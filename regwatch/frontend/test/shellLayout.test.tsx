// The shell layout must MOUNT SettingsProvider: the rail's account popover
// colophon (and the Ask confidence legend) read useSettings, which throws
// outside the provider. This renders the REAL app/(shell)/layout.tsx with the
// established mocks from askPage.test.tsx / historyPanel.test.tsx and fails if
// the <SettingsProvider> mount is ever removed from the layout.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PublicSettings } from "@/lib/api";

// The factories only CALL these at runtime (after vi.mock hoists) -- same
// partial-mock pattern as askPage.test.tsx. SettingsProvider itself stays
// REAL: mocking it would defeat the point of this suite.
const getPublicSettingsMock = vi.fn<() => Promise<PublicSettings>>();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getPublicSettings: () => getPublicSettingsMock(),
    // The real SessionsProvider mounts inside the layout; keep its fetch inert.
    listSessions: async () => ({ sessions: [] }),
  };
});

vi.mock("@/components/AuthProvider", () => ({
  useAuth: () => ({
    user: { id: 1, email: "analyst@example.test", display_name: "Analyst", role: "user" },
    loading: false,
    refresh: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
  }),
}));

const routerReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

// Plain anchor stand-in: navigation is not under test.
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

import ShellLayout from "@/app/(shell)/layout";

const SETTINGS: PublicSettings = {
  company_name: "Test Co",
  embedding_provider: "test-embed",
  llm_model: "test-llm",
  llm_provider: "test-provider",
  refusal_score_threshold: 0.3,
  retrieval_top_k: 8,
};

beforeEach(() => {
  getPublicSettingsMock.mockResolvedValue(SETTINGS);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("shell layout mounts SettingsProvider (A4)", () => {
  it("serves the account-popover colophon through context without a useSettings throw", async () => {
    const user = userEvent.setup();
    render(
      <ShellLayout>
        <div data-testid="page-child" />
      </ShellLayout>,
    );
    // The rail itself reads useSettings (reachability dot): if the
    // <SettingsProvider> mount left the layout this render would throw.
    // The colophon now lives behind the account stop.
    await user.click(screen.getByRole("button", { name: /Account/ }));
    expect(await screen.findByText(/test-embed/)).toBeInTheDocument();
    expect(screen.getByText("test-provider/test-llm")).toBeInTheDocument();
    // The page subtree still renders inside the shell's <main>.
    expect(screen.getByTestId("page-child")).toBeInTheDocument();
    // One shared GET /settings for the whole shell.
    expect(getPublicSettingsMock).toHaveBeenCalledTimes(1);
  });
});
