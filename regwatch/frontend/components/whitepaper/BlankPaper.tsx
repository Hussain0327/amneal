"use client";

import { useMemo } from "react";

import { type FlowItem, PaperDoc } from "@/components/whitepaper/PaperDoc";
import { RUNNING_HEAD } from "@/components/whitepaper/WhitePaperDocument";
import { blankGroups } from "@/lib/whitepaper-form";

/**
 * The form before anybody has filled it: the same seven tables, ruled and
 * empty, with the two things the populate needs typed onto the paper itself.
 *
 * The labels here are the printed template's own (see lib/whitepaper-form), so
 * an analyst sees exactly which 46 cells are about to be attempted before
 * committing to a run that takes a minute.
 */
export function BlankPaper({
  rld,
  applNo,
  onRld,
  onApplNo,
  onSubmit,
  loading,
  scale,
  paged,
  onLayout,
}: {
  rld: string;
  applNo: string;
  onRld: (v: string) => void;
  onApplNo: (v: string) => void;
  onSubmit: (e: React.FormEvent) => void;
  loading: boolean;
  scale: number;
  paged: boolean;
  onLayout?: (pages: number) => void;
}) {
  const items = useMemo(() => {
    const out: FlowItem[] = [
      {
        key: "intake",
        node: (
          <header className="wp-mast wp-mast--intake">
            {/* The sheet's running head already names the document on every page;
                printing it again here would be the same words twice. */}
            <form className="wp-intake" onSubmit={onSubmit}>
              <label className="wp-intake__field">
                <span>Reference product name</span>
                <input
                  className="wp-intake__input"
                  value={rld}
                  onChange={(e) => onRld(e.target.value)}
                  placeholder="albuterol sulfate"
                />
              </label>
              <label className="wp-intake__field">
                <span>Application number</span>
                <input
                  className="wp-intake__input"
                  value={applNo}
                  onChange={(e) => onApplNo(e.target.value)}
                  placeholder={"NDA 020503 \u00b7 020503 \u00b7 N020503"}
                />
              </label>
              <button
                className="wp-btn wp-btn--ink wp-intake__go"
                type="submit"
                disabled={loading || !rld.trim() || !applNo.trim()}
              >
                {loading ? "Populating..." : "Populate white paper"}
              </button>
            </form>
            <p className="wp-mast__asof">
              Every cell below is attempted against a public FDA record. What a source verifies gets
              filled and cited; what needs judgment stays blank and comes to you. Nothing is guessed.
            </p>
          </header>
        ),
      },
    ];

    for (const [gi, group] of blankGroups().entries()) {
      out.push({
        key: `band-${gi}`,
        keepWithNext: true,
        node: (
          <h2 className="wp-band">
            <span>{group.title}</span>
            <span className="wp-band__count">{group.labels.length}</span>
          </h2>
        ),
      });
      for (const label of group.labels) {
        out.push({
          key: `ghost-${gi}-${label}`,
          node: (
            <div className="wp-row wp-row--ghost">
              <div className="wp-row__label">
                <span>{label}</span>
              </div>
              <div className="wp-row__value">
                <span className="wp-blank__rule" aria-hidden />
              </div>
            </div>
          ),
        });
      }
    }
    return out;
  }, [applNo, loading, onApplNo, onRld, onSubmit, rld]);

  return (
    <div className={loading ? "wp-blankdoc wp-blankdoc--working" : "wp-blankdoc"}>
      <PaperDoc
        items={items}
        runningHead={RUNNING_HEAD}
        scale={scale}
        paged={paged}
        onLayout={onLayout}
      />
    </div>
  );
}
