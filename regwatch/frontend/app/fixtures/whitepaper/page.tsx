"use client";

import { notFound } from "next/navigation";
import { useRef, useState } from "react";

import { BlankPaper } from "@/components/whitepaper/BlankPaper";
import { FillMeter, StatusChip, ZoomControl } from "@/components/whitepaper/DocChrome";
import { useFitScale, useNarrow } from "@/components/whitepaper/PaperDoc";
import { RunCard } from "@/components/whitepaper/RunShelf";
import { type DocMeta, WhitePaperDocument } from "@/components/whitepaper/WhitePaperDocument";
import type {
  WhitepaperCell,
  WhitepaperEvidence,
  WhitepaperRunSummary,
  WhitepaperSectionData,
} from "@/lib/api";
import { FORM_TABLES, groupCells, tallyGroups } from "@/lib/whitepaper-form";

import "../../(shell)/whitepaper/whitepaper.css";

// Design fixture for surface 04: the populated white paper rendered from static
// data, so the printed-form layout can be exercised without auth, a backend, or
// a 60-second populate. Not part of the product -- production 404s unless
// NEXT_PUBLIC_FIXTURES=1.

const OB: WhitepaperEvidence = {
  source: "Orange Book",
  locator: "products.txt appl_no=020503",
  source_url: "https://www.fda.gov/drugs/drug-approvals-and-databases/orange-book-data-files",
  fetched_at: "2026-08-09T04:12:00Z",
  page: null,
  section: null,
  snippet: "ALBUTEROL SULFATE; AEROSOL, METERED; INHALATION; 0.09MG/INH; N020503; PROVENTIL HFA",
};
const LABEL: WhitepaperEvidence = {
  source: "Drugs@FDA approved labeling",
  locator: "NDA020503 approved label, page 3",
  source_url:
    "https://www.accessdata.fda.gov/drugsatfda_docs/label/2019/020503s052lbl.pdf",
  fetched_at: "2026-08-09T04:12:31Z",
  page: null,
  section: "INDICATIONS AND USAGE",
  snippet:
    "PROVENTIL HFA Inhalation Aerosol is indicated for the treatment or prevention of bronchospasm in patients 4 years of age and older with reversible obstructive airway disease.",
};
const PSG: WhitepaperEvidence = {
  source: "PSG",
  locator: "PSG_020503 (Rev. Mar 2023)",
  source_url: "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf",
  fetched_at: "2026-08-09T04:13:02Z",
  page: 2,
  section: null,
  snippet:
    "Two options are recommended: an in vitro only approach, or an in vivo PK BE study with clinical endpoint considerations.",
};

// id -> [value, evidence]. Anything not listed here is left to the analyst,
// which is the real ratio: 18 cited, 2 verifiably absent, 26 blanks.
const VALUES: Record<string, [string, WhitepaperEvidence[]]> = {
  product_name: ["ALBUTEROL SULFATE", [OB]],
  dosage_form: ["AEROSOL, METERED", [OB]],
  route: ["INHALATION", [OB]],
  strengths: ["0.09 MG/INH (90 mcg per actuation)", [OB]],
  priority_status: ["Standard - no priority designation on the application", [OB]],
  patents: ["PIII (2 unexpired), Section viii carve-out available", [OB]],
  rld: ["Yes - PROVENTIL HFA (N020503, product 001)", [OB]],
  rs: ["Yes - reference standard for the application", [OB]],
  proprietary_name: ["PROVENTIL HFA", [OB]],
  rld_strength: ["90 mcg base per actuation", [OB]],
  nda_number: ["NDA 020503", [OB]],
  nda_holder: ["Merck Sharp & Dohme Corp.", [OB]],
  indication: [
    "Treatment or prevention of bronchospasm in patients 4 years of age and older with reversible obstructive airway disease, and prevention of exercise-induced bronchospasm.",
    [LABEL],
  ],
  epc: ["beta2-Adrenergic Agonist [EPC]", [LABEL]],
  labeling_images: ["Labeling image review requires analyst confirmation", [LABEL]],
  packaging: ["6.7 g canister, 200 metered actuations", [LABEL]],
  be_guidance_available: ["Yes - PSG for albuterol sulfate inhalation aerosol, Rev. Mar 2023", [PSG]],
  requirements: [
    "In vitro option: single actuation content, aerodynamic particle size distribution, spray pattern, plume geometry, priming and repriming. In vivo option: PK BE study with the in vitro battery above.",
    [PSG],
  ],
};
const ABSENT = new Set(["rems", "drug_shortage"]);
const NOTES: Record<string, string> = {
  rd_center: "No public source carries the internal R&D center assignment.",
  usp_monograph: "USP-NF is paywalled; the monograph status cannot be verified from a public record.",
  salable_unit: "Sales and Marketing owns the salable unit; it is not in any FDA record.",
  emergency_use: "The EUA list is unstructured prose and cannot be joined deterministically.",
  first_to_market: "Depends on the ANDA filing posture, which is not a public fact.",
};

function makeSections(): WhitepaperSectionData[] {
  return FORM_TABLES.map((table) => ({
    title: table.title,
    cells: table.cells.map((slot): WhitepaperCell => {
      const hit = VALUES[slot.id];
      if (hit) {
        return {
          id: slot.id,
          label: slot.label,
          mode: "auto",
          status: "populated",
          value: hit[0],
          evidence: hit[1],
          note: null,
        };
      }
      if (ABSENT.has(slot.id)) {
        return {
          id: slot.id,
          label: slot.label,
          mode: "auto",
          status: "verified_absent",
          value: null,
          evidence: [slot.id === "rems" ? LABEL : OB],
          note: null,
        };
      }
      return {
        id: slot.id,
        label: slot.label,
        mode: "manual",
        status: "analyst_input_required",
        value: null,
        evidence: [],
        note: NOTES[slot.id] ?? null,
      };
    }),
  }));
}

const SECTIONS = makeSections();

const META: DocMeta = {
  spine: {
    application_number: "020503",
    application_type: "NDA",
    ingredient: "ALBUTEROL SULFATE",
    normalized_name: "albuterol sulfate hfa",
    product_numbers: ["001"],
    setid: null,
    spl_candidates: [],
    approved_label_document_id: "drugs_at_fda:NDA020503:approved_label:2019-02-15",
    approved_label_source_url:
      "https://www.accessdata.fda.gov/drugsatfda_docs/label/2019/020503s052lbl.pdf",
    approved_label_updated_at: "2019-02-15",
    warnings: [],
  },
  warnings: [],
  auditId: 4217,
  runId: 12,
  status: "draft",
  preparedBy: "Raja Hussain",
  preparedAt: "2026-08-09T04:12:00Z",
  finalizedAt: null,
  finalizedBy: null,
};


// Four saved runs at different stages of being filled, so the shelf's sheet
// previews can be read against each other.
function run(over: Partial<WhitepaperRunSummary>): WhitepaperRunSummary {
  return {
    id: 1,
    rld_name_input: "albuterol sulfate",
    application_number: "020503",
    application_type: "NDA",
    ingredient: "ALBUTEROL SULFATE",
    normalized_name: "albuterol sulfate hfa",
    status: "draft",
    populated_count: 18,
    analyst_input_count: 26,
    verified_absent_count: 2,
    inputs_count: 0,
    created_by: "Raja Hussain",
    created_at: "2026-08-05T10:00:00Z",
    updated_at: "2026-08-05T10:00:00Z",
    ...over,
  };
}

const RUNS: WhitepaperRunSummary[] = [
  run({ id: 12 }),
  run({ id: 11, inputs_count: 19, updated_at: "2026-08-10T18:00:00Z" }),
  run({
    id: 9,
    ingredient: "TACROLIMUS",
    application_number: "050708",
    status: "final",
    populated_count: 24,
    analyst_input_count: 20,
    inputs_count: 20,
    created_by: "Hana Analyst",
    updated_at: "2026-07-28T09:00:00Z",
  }),
  run({
    id: 4,
    ingredient: "ESOMEPRAZOLE MAGNESIUM",
    application_number: "021957",
    populated_count: 11,
    verified_absent_count: 5,
    analyst_input_count: 30,
    inputs_count: 6,
    updated_at: "2026-07-02T09:00:00Z",
  }),
];

export default function WhitepaperFixture() {
  if (process.env.NODE_ENV === "production" && process.env.NEXT_PUBLIC_FIXTURES !== "1") {
    notFound();
  }
  return <Fixture />;
}

function Fixture() {
  const stageRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState<number | null>(null);
  const [blank, setBlank] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [pages, setPages] = useState(1);
  const narrow = useNarrow();
  const scale = useFitScale(stageRef, narrow ? 1 : zoom);
  const tally = tallyGroups(groupCells(SECTIONS));
  const noop = async () => {};

  return (
    <main className="canvas" style={{ minHeight: "100vh" }}>
      <div className="wp">
        <div className="wp-bar">
          <div className="wp-bar__id">
            <span className="wp-bar__no">04</span>
            <span className="wp-bar__where">White Paper / fixture</span>
          </div>
          <div className="wp-bar__doc">
            <span className="wp-bar__name">ALBUTEROL SULFATE</span>
            <span className="wp-bar__appl">NDA 020503</span>
            <StatusChip status="draft" />
          </div>
          <FillMeter tally={tally} filled={0} />
          <div className="wp-bar__tools">
            <ZoomControl zoom={zoom} scale={scale} onZoom={setZoom} />
            <span className="wp-bar__pages">
              {pages} {pages === 1 ? "page" : "pages"}
            </span>
          </div>
          <div className="wp-bar__actions">
            <button className="wp-btn" type="button" onClick={() => setBlank((b) => !b)}>
              {blank ? "Show filled" : "Show blank"}
            </button>
            <button
              className="wp-btn"
              type="button"
              onClick={() => {
                setReveal(true);
                setTimeout(() => setReveal(false), 1600);
              }}
            >
              Replay fill
            </button>
          </div>
        </div>

        <div className="wp-viewport" ref={stageRef}>
          {blank ? (
            <BlankPaper
              rld=""
              applNo=""
              onRld={() => {}}
              onApplNo={() => {}}
              onSubmit={(e) => e.preventDefault()}
              loading={false}
              scale={scale}
              paged={!narrow}
            />
          ) : (
            <WhitePaperDocument
              meta={META}
              sections={SECTIONS}
              workflow={{ frozen: false, inputs: {}, onSave: noop, onClear: noop }}
              scale={scale}
              paged={!narrow}
              reveal={reveal}
              onLayout={setPages}
            />
          )}
        </div>
        <section className="wp-shelf">
          <div className="wp-shelf__head">
            <h2>Saved runs</h2>
            <span className="wp-shelf__count">4 runs</span>
            <button className="wp-btn wp-btn--quiet" type="button">
              Refresh
            </button>
          </div>
          <div className="wp-shelf__grid">
            {RUNS.map((r, i) => (
              <RunCard
                key={r.id}
                run={r}
                open={i === 0}
                onOpen={() => {}}
                deleteConfirm={i === 3}
                deleteBusy={false}
                deleteError={null}
                onDeleteAsk={() => {}}
                onDeleteCancel={() => {}}
                onDeleteConfirm={() => {}}
              />
            ))}
          </div>
        </section>
      </div>
    </main>
  );
}
