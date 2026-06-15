"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { createContext, useCallback, useContext, useMemo } from "react";

// The reference product the four surfaces are scoped to, mirrored into the URL
// query so the scope survives reloads and is shareable. The keys sit alongside
// the Ask page's existing `session` param without colliding.
const RP_PARAM = "rp"; // reference product name
const APPL_PARAM = "appl"; // application number

export interface CurrentProduct {
  referenceProductName: string;
  applicationNumber: string;
}

interface CurrentProductState extends CurrentProduct {
  /** True when either field is set. */
  hasProduct: boolean;
  /** Set the product on the current URL, preserving every other query param. */
  setProduct: (product: CurrentProduct) => void;
  /** Clear the product from the current URL, preserving every other param. */
  clearProduct: () => void;
  /**
   * The scope as an encoded query fragment ("rp=…&appl=…"), or "" when unset —
   * for building hrefs that carry the scope to another route.
   */
  productParams: string;
}

const CurrentProductContext = createContext<CurrentProductState | null>(null);

export function useCurrentProduct(): CurrentProductState {
  const ctx = useContext(CurrentProductContext);
  if (!ctx) throw new Error("useCurrentProduct must be used inside <CurrentProductProvider>");
  return ctx;
}

// Holds the scoped product for the whole shell. The URL is the state of record:
// the four routes read it here, and any surface that sets it rewrites only the
// rp/appl params — never the rest (notably the Ask page's `session`), so
// scoping a product never drops an open conversation.
export function CurrentProductProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const referenceProductName = searchParams.get(RP_PARAM) ?? "";
  const applicationNumber = searchParams.get(APPL_PARAM) ?? "";

  const writeParams = useCallback(
    (mutate: (params: URLSearchParams) => void) => {
      const params = new URLSearchParams(searchParams.toString());
      mutate(params);
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname, searchParams],
  );

  const setProduct = useCallback(
    (product: CurrentProduct) => {
      writeParams((params) => {
        const rp = product.referenceProductName.trim();
        const appl = product.applicationNumber.trim();
        if (rp) params.set(RP_PARAM, rp);
        else params.delete(RP_PARAM);
        if (appl) params.set(APPL_PARAM, appl);
        else params.delete(APPL_PARAM);
      });
    },
    [writeParams],
  );

  const clearProduct = useCallback(() => {
    writeParams((params) => {
      params.delete(RP_PARAM);
      params.delete(APPL_PARAM);
    });
  }, [writeParams]);

  const value = useMemo<CurrentProductState>(() => {
    const carry = new URLSearchParams();
    if (referenceProductName) carry.set(RP_PARAM, referenceProductName);
    if (applicationNumber) carry.set(APPL_PARAM, applicationNumber);
    return {
      referenceProductName,
      applicationNumber,
      hasProduct: Boolean(referenceProductName || applicationNumber),
      setProduct,
      clearProduct,
      productParams: carry.toString(),
    };
  }, [referenceProductName, applicationNumber, setProduct, clearProduct]);

  return <CurrentProductContext.Provider value={value}>{children}</CurrentProductContext.Provider>;
}
