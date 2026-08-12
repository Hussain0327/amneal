// The record drawer's domain rules.
//
// Every case here pins a claim the drawer makes about the record, and each one
// fails if its rule is removed: that grounding never follows a turn that did
// not answer (INV-2), that the drawer's [n] is the prose's [n], that an absent
// fact stays absent rather than defaulting to a weaker claim, and that a
// bounded list reports the true size of what it bounded.
import { describe, expect, it } from "vitest";

import type { Citation, PsgLibraryDoc } from "@/lib/api";
import {
  countSources,
  fileAuthorities,
  fileHistory,
  searchCorpus,
  studioScope,
  turnKey,
} from "@/lib/research-record";
import { authoritiesFrom } from "@/lib/research-types";
import type { Turn } from "@/lib/turns";

function citation(shortName: string, page: number, extra: Partial<Citation> = {}): Citation {
  return {
    chunk_id: `${shortName}-${page}`,
    doc_id: 1,
    page,
    short_name: shortName,
    snippet: `${shortName} p.${page} snippet`,
    source_url: `https://fda.test/${shortName}`,
    version_id: 1,
    ...extra,
  };
}

function assistant(overrides: Partial<Turn> = {}): Turn {
  return {
    role: "assistant",
    content: "an answer",
    status: "answer",
    refused: false,
    citations: [],
    clarify: [],
    related: [],
    interpretation: null,
    reason: null,
    live: false,
    meta: null,
    createdAt: "2026-08-12T14:32:00",
    id: null,
    statusLog: [],
    streamFellBack: false,
    draftWithdrawn: null,
    ...overrides,
  };
}

function ask(content: string): Turn {
  return assistant({ role: "user", content, status: null, citations: [] });
}

function psg(id: number, over: Partial<PsgLibraryDoc> = {}): PsgLibraryDoc {
  return {
    id,
    active_ingredient: "Albuterol Sulfate",
    appl_no: "020503",
    dosage_form: "Aerosol, Metered",
    normalized_name: "albuterol sulfate",
    psg_type: "final",
    recommended_date: "2024-11-01",
    route: "Inhalation",
    source_url: "https://fda.test/psg",
    stripped_name: "albuterol",
    ...over,
  };
}

describe("fileAuthorities", () => {
  it("files each answer's sources under the question that fetched them", () => {
    const filings = fileAuthorities([
      ask("what BE study is required?"),
      assistant({ citations: [citation("PSG Albuterol", 4)] }),
      ask("and the dissolution method?"),
      assistant({ citations: [citation("PSG Albuterol", 7)] }),
    ]);

    expect(filings.map((f) => f.question)).toEqual([
      "what BE study is required?",
      "and the dissolution method?",
    ]);
    expect(filings[0].authorities[0].page).toBe(4);
  });

  // INV-2. A refusal renders no citation surface in the transcript, so the
  // drawer must not hand grounding back on its behalf -- even when the wire
  // carried passages for the turn.
  it("files nothing for a turn that did not answer, even with citations on it", () => {
    for (const status of ["refused", "clarify", "error", "scope_warning", "meta"] as const) {
      const filings = fileAuthorities([
        ask("q"),
        assistant({ status, refused: status === "refused", citations: [citation("PSG A", 1)] }),
      ]);
      expect(filings, `status ${status} must file nothing`).toEqual([]);
    }
  });

  it("files nothing for an answer that cited nothing, rather than an empty group", () => {
    expect(fileAuthorities([ask("q"), assistant({ citations: [] })])).toEqual([]);
  });

  // The whole reason the drawer calls authoritiesFrom instead of numbering its
  // own list: citationIndex counts position in the RAW array and lets the first
  // occurrence win, so [A, A, B] numbers B as 3 and the prose stamps a 3. A
  // drawer that numbered the deduped array would call the same source 2 and
  // point every stamp past the first duplicate at the wrong authority.
  it("numbers by the same call the prose stamps with, gaps included", () => {
    const citations = [citation("PSG A", 1), citation("PSG A", 1), citation("PSG B", 2)];
    const [filing] = fileAuthorities([ask("q"), assistant({ citations })]);

    expect(filing.authorities.map((a) => a.n)).toEqual([1, 3]);
    expect(filing.authorities.map((a) => a.n)).toEqual(authoritiesFrom(citations).map((a) => a.n));
  });

  it("joins the source url and standing back onto each authority", () => {
    const [filing] = fileAuthorities([
      ask("q"),
      assistant({
        citations: [
          citation("PSG A", 1, { source_url: "https://fda.test/a", psg_type: "final" }),
          citation("PSG B", 2, { source_url: "https://fda.test/b", psg_type: "draft" }),
        ],
      }),
    ]);

    expect(filing.authorities.map((a) => a.sourceUrl)).toEqual([
      "https://fda.test/a",
      "https://fda.test/b",
    ]);
    expect(filing.authorities.map((a) => a.psgType)).toEqual(["final", "draft"]);
  });

  // "We were not told" and "draft" are different claims about an FDA document,
  // and only one of them may be printed on the row.
  it("leaves standing null when the wire did not state one", () => {
    const [filing] = fileAuthorities([
      ask("q"),
      assistant({ citations: [citation("PSG A", 1), citation("PSG B", 2, { psg_type: "" })] }),
    ]);
    expect(filing.authorities.map((a) => a.psgType)).toEqual([null, null]);
  });

  it("degrades an unrecognized standing to the weaker claim", () => {
    const [filing] = fileAuthorities([
      ask("q"),
      assistant({ citations: [citation("PSG A", 1, { psg_type: "revised" })] }),
    ]);
    expect(filing.authorities[0].psgType).toBe("draft");
  });

  it("names the turn with the key the transcript renders", () => {
    const turn = assistant({ citations: [citation("PSG A", 1)], id: "row-9" });
    const [filing] = fileAuthorities([ask("q"), turn]);
    expect(filing.key).toBe(turnKey(turn, 1));
    expect(filing.key).toBe("assistant-row-9");
  });

  it("says the question is missing rather than attributing the previous one", () => {
    // A rehydrated transcript that lost its user row: the answer must not be
    // filed under whatever question happened to come before it.
    const [filing] = fileAuthorities([assistant({ citations: [citation("PSG A", 1)] })]);
    expect(filing.question).toBe("");
  });
});

describe("countSources", () => {
  it("counts one guidance passage once however many answers leaned on it", () => {
    const shared = citation("PSG A", 1);
    const filings = fileAuthorities([
      ask("first"),
      assistant({ citations: [shared] }),
      ask("second"),
      assistant({ citations: [shared, citation("PSG B", 2)] }),
    ]);
    expect(filings).toHaveLength(2);
    expect(countSources(filings)).toBe(2);
  });
});

describe("fileHistory", () => {
  // The trail's whole reason to exist: the turns that produced nothing are the
  // ones somebody reconstructing a filing needs to find.
  it("keeps refusals and errors alongside answers", () => {
    const trail = fileHistory([
      ask("a"),
      assistant({ status: "answer", citations: [citation("PSG A", 1)] }),
      ask("b"),
      assistant({ status: "refused", refused: true }),
      ask("c"),
      assistant({ status: "error", refused: true }),
      ask("d"),
      assistant({ status: "clarify" }),
      ask("e"),
      assistant({ status: "meta" }),
    ]);

    expect(trail.map((e) => e.outcome)).toEqual([
      "Answered",
      "Evidence gap",
      "Answer unavailable",
      "Clarification",
      "Conversational",
    ]);
    expect(trail.map((e) => e.tone)).toEqual([
      "answer",
      "declined",
      "declined",
      "clarify",
      "plain",
    ]);
  });

  // Counting sources on a turn that owed none would state that grounding was
  // expected and missing -- the INV-2 boundary drawn in the wrong place.
  it("reports no source count for a turn that could not carry sources", () => {
    const trail = fileHistory([
      ask("a"),
      assistant({ status: "refused", refused: true, citations: [citation("PSG A", 1)] }),
      ask("b"),
      assistant({ status: "answer", citations: [citation("PSG A", 1), citation("PSG B", 2)] }),
    ]);
    expect(trail[0].sourceCount).toBe(0);
    expect(trail[1].sourceCount).toBe(2);
  });

  it("counts the sources the prose stamped, not the raw wire array", () => {
    const trail = fileHistory([
      ask("a"),
      assistant({ citations: [citation("PSG A", 1), citation("PSG A", 1)] }),
    ]);
    expect(trail[0].sourceCount).toBe(1);
  });

  // turnFromMessage builds meta from a persisted audit_id and fills model_name
  // with "", so a rehydrated turn would otherwise render a provenance line
  // naming nothing.
  it("treats an empty model name as no model name", () => {
    const trail = fileHistory([
      ask("a"),
      assistant({ meta: { model_name: "", audit_id: 41, turn_id: "t1" } }),
      ask("b"),
      assistant({ meta: { model_name: "gpt-oss-120b", audit_id: 42, turn_id: "t2" } }),
    ]);
    expect(trail[0].modelName).toBeNull();
    expect(trail[0].auditId).toBe(41);
    expect(trail[1].modelName).toBe("gpt-oss-120b");
  });

  it("carries the two things that happened to the answer rather than in it", () => {
    const trail = fileHistory([
      ask("a"),
      assistant({ streamFellBack: true, draftWithdrawn: "partial" }),
    ]);
    expect(trail[0].fellBack).toBe(true);
    expect(trail[0].draftWithdrawn).toBe("partial");
  });
});

describe("studioScope", () => {
  // A thread that moved from one ingredient to another is about the second one.
  // Scoping a new question to the first would silently answer about the wrong
  // drug, which is the failure this rule exists to prevent.
  it("reads the newest sourced turn, not the first", () => {
    expect(
      studioScope([
        ask("a"),
        assistant({
          citations: [citation("PSG A", 1, { product_name: "metformin hydrochloride" })],
        }),
        ask("b"),
        assistant({
          citations: [
            citation("PSG B", 2, {
              product_name: "albuterol sulfate",
              dosage_form: "Aerosol, Metered",
            }),
          ],
        }),
      ]),
    ).toEqual({ normalizedName: "albuterol sulfate", dosageForm: "Aerosol, Metered" });
  });

  it("takes the dosage form only from citations that share the product", () => {
    expect(
      studioScope([
        ask("a"),
        assistant({
          citations: [
            citation("PSG A", 1, { product_name: "albuterol sulfate", dosage_form: "Solution" }),
            citation("PSG A", 2, { product_name: "albuterol sulfate", dosage_form: "Solution" }),
            citation("PSG B", 3, { product_name: "metformin hydrochloride", dosage_form: "Tablet" }),
          ],
        }),
      ]),
    ).toEqual({ normalizedName: "albuterol sulfate", dosageForm: "Solution" });
  });

  it("has no scope when nothing has been cited, rather than guessing one", () => {
    expect(studioScope([])).toBeNull();
    expect(studioScope([ask("albuterol sulfate inhalation")])).toBeNull();
    expect(
      studioScope([ask("a"), assistant({ status: "refused", refused: true })]),
    ).toBeNull();
    // Cited, but the wire named no product.
    expect(studioScope([ask("a"), assistant({ citations: [citation("PSG A", 1)] })])).toBeNull();
  });

  it("has no dosage form when the citations do not agree on one being present", () => {
    expect(
      studioScope([
        ask("a"),
        assistant({ citations: [citation("PSG A", 1, { product_name: "albuterol sulfate" })] }),
      ]),
    ).toEqual({ normalizedName: "albuterol sulfate", dosageForm: null });
  });
});

describe("searchCorpus", () => {
  const CATALOG: PsgLibraryDoc[] = [
    psg(1, { dosage_form: "Aerosol, Metered" }),
    psg(2, { dosage_form: "Solution", psg_type: "draft" }),
    psg(3, {
      active_ingredient: "Metformin Hydrochloride",
      appl_no: "020357",
      normalized_name: "metformin hydrochloride",
      stripped_name: "metformin",
      dosage_form: "Tablet",
      route: "Oral",
    }),
  ];

  // A filter, not a ranker: every term has to hold, or "albuterol aerosol"
  // widens to everything mentioning either word instead of narrowing.
  it("requires every term to match", () => {
    expect(searchCorpus(CATALOG, "albuterol aerosol", 10).hits.map((h) => h.id)).toEqual([1]);
    expect(searchCorpus(CATALOG, "albuterol", 10).matched).toBe(2);
    expect(searchCorpus(CATALOG, "albuterol metformin", 10).matched).toBe(0);
  });

  it("matches inside a word, and across the fields a row is known by", () => {
    expect(searchCorpus(CATALOG, "sulfate", 10).matched).toBe(2);
    // route, and the salt-stripped key the server derives
    expect(searchCorpus(CATALOG, "oral", 10).hits.map((h) => h.id)).toEqual([3]);
    expect(searchCorpus(CATALOG, "metformin", 10).hits.map((h) => h.id)).toEqual([3]);
    // application number
    expect(searchCorpus(CATALOG, "020503", 10).matched).toBe(2);
  });

  // The panel does not fetch until it is asked, and a blank field resolving to
  // the whole catalog would render a wall nobody requested.
  it("matches nothing on an empty query", () => {
    expect(searchCorpus(CATALOG, "", 10)).toEqual({ hits: [], matched: 0 });
    expect(searchCorpus(CATALOG, "   ", 10)).toEqual({ hits: [], matched: 0 });
  });

  // A capped list rendered as the whole answer is the same lie an unreachable
  // list rendered as empty would be.
  it("reports the true match count when the list is capped", () => {
    const result = searchCorpus(CATALOG, "albuterol", 1);
    expect(result.hits).toHaveLength(1);
    expect(result.matched).toBe(2);
  });

  it("labels the form from whichever side the row has", () => {
    expect(searchCorpus(CATALOG, "aerosol", 10).hits[0].form).toBe("Aerosol, Metered (Inhalation)");
    expect(
      searchCorpus([psg(9, { dosage_form: null, route: null })], "albuterol", 10).hits[0].form,
    ).toBe("Form not stated");
  });

  it("never presents an unrecognized standing as final", () => {
    expect(searchCorpus([psg(9, { psg_type: "revised" })], "albuterol", 10).hits[0].psgType).toBe(
      "draft",
    );
  });
});

describe("turnKey", () => {
  // Live turns carry meta.turn_id, rehydrated ones the server row id, and only
  // then the index -- the order the transcript uses.
  it("prefers the live turn id, then the row id, then the position", () => {
    expect(turnKey(assistant({ meta: { model_name: "m", audit_id: 1, turn_id: "live" } }), 3)).toBe(
      "assistant-live",
    );
    expect(turnKey(assistant({ id: "row" }), 3)).toBe("assistant-row");
    expect(turnKey(assistant(), 3)).toBe("assistant-3");
  });
});
