"use client";

import { Suspense } from "react";

import { useAuth } from "@/components/AuthProvider";
import { CurrentProductProvider } from "@/components/CurrentProductProvider";
import { ProductScopeBar } from "@/components/ProductScopeBar";
import { SessionsProvider } from "@/components/SessionsProvider";
import { SettingsProvider } from "@/components/SettingsProvider";
import { SpineRail } from "@/components/SpineRail";

// The shared shell for the five product surfaces (Ask / Assemble / Watch /
// White Paper / Deficiency). One spine rail, one canvas, one scoped-product
// context — defined here once and applied to every route in this group. The
// bare routes (/login, /fixtures) sit outside the group and never see it.
//
// Auth is still gated upstream in <AuthProvider>, which renders this subtree
// only once /auth/me confirms a user — so `user` is non-null whenever this
// layout mounts. key={user.id} preserves the prior behavior: a different
// identity remounts the session-scoped subtree, so a stale tab can never keep
// another user's transcript or session list in component state.
export default function ShellLayout({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  return (
    <SessionsProvider key={user?.id ?? "anon"}>
      {/* Inside the keyed SessionsProvider: settings are global, but sharing
          the identity remount is harmless (one refetch) and keeps every
          authed fetch behind the same gate. */}
      <SettingsProvider>
        {/* useSearchParams (CurrentProductProvider + Sidebar) needs a Suspense
            boundary to prerender cleanly. */}
        <Suspense fallback={null}>
          <CurrentProductProvider>
            <div className="shell">
              <a href="#main" className="skip-link">
                Skip to content
              </a>
              <SpineRail />
              <main id="main" tabIndex={-1} className="canvas">
                <ProductScopeBar />
                {children}
              </main>
            </div>
          </CurrentProductProvider>
        </Suspense>
      </SettingsProvider>
    </SessionsProvider>
  );
}
