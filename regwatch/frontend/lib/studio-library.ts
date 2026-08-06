/**
 * Grouping for the Compliance Studio reference library. Pure: wire rows in,
 * letter -> drug -> document tree out. No I/O, no clock, no randomness, so the
 * whole module is unit-testable without a DOM.
 *
 * The server computes `stripped_name` (salt-collapsed drug key); this module
 * only consumes it. Porting the salt-token table to TypeScript would create a
 * second diverging normalizer.
 */

/** The fields of a wire PSG row this module reads. Structural on purpose, so
 * the generated schema type is assignable without coupling tests to codegen. */
export interface PsgWireDoc {
  id: number;
  active_ingredient: string;
  stripped_name?: string | null;
  dosage_form?: string | null;
  route?: string | null;
  psg_type?: string | null;
  recommended_date?: string | null;
  source_url?: string | null;
}

/** One PSG as the rail and the PDF pane consume it. */
export interface LibraryDoc {
  /** Stable and collision-free with fixture doc ids: "psg-{db id}". */
  id: string;
  /** Numeric DB id; feeds psgPdfPath(). */
  psgId: number;
  /** active_ingredient verbatim, e.g. "Albuterol Sulfate". */
  ingredient: string;
  /** Capitalized salt-stripped drug, repeated here so the TopBar crumb needs
   * no tree lookup. */
  drugLabel: string;
  /** "{dosage_form} ({route})", degrading to whichever side exists. */
  label: string;
  psgType: "draft" | "final";
  /** ISO date string as stored, or null. Display only. */
  recommendedDate: string | null;
  /** The fda.gov PDF URL for the link-out. Guarded by safeHref at render. */
  sourceUrl: string | null;
}

export interface LibraryDrug {
  /** "lib-d-" + sanitized drug key. Stable across loads. */
  id: string;
  /** Capitalized drug key, e.g. "Acetaminophen; Hydrocodone". */
  label: string;
  docs: LibraryDoc[];
}

export interface LibraryBucket {
  /** "A".."Z", or "#" for names not starting with a letter. */
  letter: string;
  /** "lib-b-A".."lib-b-Z", "lib-b-num". Stable. */
  id: string;
  drugs: LibraryDrug[];
}

/** Anything the checker does not positively call "final" reads as the weaker
 * claim -- an unrecognized value must never present as a final guidance. */
function narrowPsgType(raw: string | null | undefined): "draft" | "final" {
  return raw === "final" ? "final" : "draft";
}

function drugKey(doc: PsgWireDoc): string {
  const stripped = (doc.stripped_name ?? "").trim();
  return (stripped || doc.active_ingredient.toLowerCase()).trim();
}

function capitalizeDrug(key: string): string {
  // Combo keys are "; "-joined; capitalize each ingredient independently.
  return key
    .split("; ")
    .map((part) => (part ? part.charAt(0).toUpperCase() + part.slice(1) : part))
    .join("; ");
}

function docLabel(doc: PsgWireDoc): string {
  const form = (doc.dosage_form ?? "").trim();
  const route = (doc.route ?? "").trim();
  if (form && route) return `${form} (${route})`;
  if (form) return form;
  if (route) return route;
  // Never an empty row, never an invented form.
  return "Form not stated";
}

function toLibraryDoc(doc: PsgWireDoc, drugLabel: string): LibraryDoc {
  return {
    id: `psg-${doc.id}`,
    psgId: doc.id,
    ingredient: doc.active_ingredient,
    drugLabel,
    label: docLabel(doc),
    psgType: narrowPsgType(doc.psg_type),
    recommendedDate: doc.recommended_date ?? null,
    sourceUrl: doc.source_url ?? null,
  };
}

function compareDocs(a: LibraryDoc, b: LibraryDoc): number {
  const byLabel = a.label.toLowerCase().localeCompare(b.label.toLowerCase(), "en");
  if (byLabel !== 0) return byLabel;
  // The current guidance outranks its superseded draft on an otherwise-tied row.
  if (a.psgType !== b.psgType) return a.psgType === "final" ? -1 : 1;
  return a.psgId - b.psgId;
}

function bucketLetter(drugLabel: string): string {
  const first = drugLabel.charAt(0).toUpperCase();
  return /[A-Z]/.test(first) ? first : "#";
}

/** Letter buckets -> salt-collapsed drugs -> PSGs, fully sorted and with
 * stable ids. Deterministic regardless of wire order. */
export function buildLibraryTree(docs: readonly PsgWireDoc[]): LibraryBucket[] {
  const drugsByKey = new Map<string, LibraryDrug>();
  for (const doc of docs) {
    const key = drugKey(doc);
    let drug = drugsByKey.get(key);
    if (!drug) {
      drug = {
        id: `lib-d-${key.replace(/[^a-z0-9]+/g, "-")}`,
        label: capitalizeDrug(key),
        docs: [],
      };
      drugsByKey.set(key, drug);
    }
    drug.docs.push(toLibraryDoc(doc, drug.label));
  }

  const buckets = new Map<string, LibraryBucket>();
  for (const drug of drugsByKey.values()) {
    drug.docs.sort(compareDocs);
    const letter = bucketLetter(drug.label);
    let bucket = buckets.get(letter);
    if (!bucket) {
      bucket = { letter, id: `lib-b-${letter === "#" ? "num" : letter}`, drugs: [] };
      buckets.set(letter, bucket);
    }
    bucket.drugs.push(drug);
  }

  const out = [...buckets.values()];
  for (const bucket of out) {
    bucket.drugs.sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase(), "en"));
  }
  // A-Z, with the non-letter bucket last.
  out.sort((a, b) => {
    if (a.letter === "#") return 1;
    if (b.letter === "#") return -1;
    return a.letter.localeCompare(b.letter, "en");
  });
  return out;
}

export function countLibraryDocs(buckets: readonly LibraryBucket[]): number {
  return buckets.reduce(
    (total, bucket) => total + bucket.drugs.reduce((n, drug) => n + drug.docs.length, 0),
    0,
  );
}

/**
 * Same semantics as the working tree's filterTree: a drug whose label matches
 * keeps ALL its docs (the path an analyst recognises), otherwise keep docs
 * whose label OR raw ingredient matches (so "sulfate" still finds the salt
 * row the drug label stripped). A bucket survives only when a drug under it
 * survived. `needle` arrives pre-trimmed and lowercased, same as today.
 */
export function filterLibrary(
  buckets: readonly LibraryBucket[],
  needle: string,
): LibraryBucket[] {
  if (!needle) return [...buckets];
  const out: LibraryBucket[] = [];
  for (const bucket of buckets) {
    const drugs: LibraryDrug[] = [];
    for (const drug of bucket.drugs) {
      if (drug.label.toLowerCase().includes(needle)) {
        drugs.push(drug);
        continue;
      }
      const docs = drug.docs.filter(
        (doc) =>
          doc.label.toLowerCase().includes(needle) ||
          doc.ingredient.toLowerCase().includes(needle),
      );
      if (docs.length > 0) drugs.push({ ...drug, docs });
    }
    if (drugs.length > 0) out.push({ ...bucket, drugs });
  }
  return out;
}
