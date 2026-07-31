// Wire types for the Deficiency surface (POST /deficiency/analyze, GET
// /deficiency/runs, GET /deficiency/runs/{id}).
//
// These are HAND-MIRRORED from the Pydantic source, field for field, because
// the deficiency routes are not in the generated OpenAPI snapshot yet:
//   src/regwatch/deficiency/schemas/faults.py  (Fault, FaultReport, Tier,
//                                               EvidenceClass)
//   src/regwatch/deficiency/schemas/flaws.py   (FlawCategory, Severity,
//                                               SimilarDeficiency)
//   src/regwatch/deficiency/schemas/llm.py     (ParseFailed)
// Every field is REQUIRED here on purpose: the Pydantic models give each field
// a default, so the serialized model always carries the key. When these routes
// join the OpenAPI export, replace this file with the generated types.

// What kind of check stands behind a finding -- surfaced so an analyst never
// mistakes a model opinion for a code-verified fact (faults.py EvidenceClass).
export type EvidenceClass = "code_verified" | "checklist" | "quote_anchored" | "model_judgment";

// Confidence tier. Recall lives in "advisory" -- nothing is hidden, only
// ranked (faults.py Tier).
export type Tier = "verified" | "corroborated" | "advisory";

// flaws.py Severity.
export type Severity = "high" | "medium" | "low";

// flaws.py FlawCategory. Mirrored in full so a typo in a category label is a
// compile error; renderers still fall back to the raw string, so a backend that
// adds a member degrades to "unlabelled category", never to a crash.
export type FlawCategory =
  | "spec_incomplete"
  | "spec_mismatch"
  | "spec_limits_missing"
  | "method_not_validated"
  | "method_specificity"
  | "method_accuracy"
  | "method_linearity"
  | "method_robustness"
  | "method_lod_loq"
  | "impurity_limits"
  | "impurity_qualification"
  | "impurity_identification"
  | "stability_design"
  | "stability_data_insufficient"
  | "stability_out_of_trend"
  | "container_closure_inadequate"
  | "container_extractables"
  | "batch_data_missing"
  | "batch_inconsistency"
  | "process_validation"
  | "process_controls"
  | "reference_standard"
  | "coa_discrepancy"
  | "dissolution_method"
  | "dissolution_profile"
  | "excipient_compatibility"
  | "polymorphic_form"
  | "particle_size"
  | "elemental_impurities"
  | "residual_solvents"
  | "commitment_missing"
  | "coverage_gap"
  | "general_cmc";

// A historical ANDA deficiency retrieved from the KB as precedent
// (flaws.py SimilarDeficiency).
export interface SimilarDeficiency {
  anda_number: string;
  product_name: string;
  deficiency_text: string;
  similarity_score: number;
}

// Typed sentinel from the structured-output path (llm.py ParseFailed): the
// frontend renders it as a needs-human-review card instead of a raw LLM dump.
export interface ParseFailed {
  layer: string;
  reason: string;
  raw_output: string;
  validation_error: string;
  requires_human_review: boolean;
}

// One candidate deficiency (faults.py Fault).
export interface Fault {
  title: string;
  detail: string;
  category: FlawCategory;
  severity: Severity;

  tier: Tier;
  evidence_class: EvidenceClass;
  confidence: number;

  evidence: string;
  section: string;
  page: number;
  table_ref: string;

  source: string;
  guidance_refs: string[];
  precedents: SimilarDeficiency[];

  novel: boolean;
  out_of_distribution: boolean;
  challenge_note: string;
}

// The detection layer's output (faults.py FaultReport).
export interface FaultReport {
  job_id: string;
  faults: Fault[];
  faults_found: boolean;
  domains_checked: string[];
  parse_failures: ParseFailed[];
  analysis_seconds: number;
}

export type DeficiencyRunStatus = "pending" | "running" | "complete" | "failed";

export interface DeficiencyRunSummary {
  id: number;
  filename: string;
  status: DeficiencyRunStatus;
  created_at: string;
  completed_at: string | null;
  page_count: number | null;
  fault_count: number | null;
  error: string | null;
}

// report is non-null ONLY when status === "complete"; every other status must
// render as progress or as the run's error, never as an empty report.
export interface DeficiencyRunDetail extends DeficiencyRunSummary {
  report: FaultReport | null;
}

export interface DeficiencyRunList {
  runs: DeficiencyRunSummary[];
}

// 202 body of POST /deficiency/analyze: the run is queued, nothing is analyzed
// yet -- the caller polls GET /deficiency/runs/{run_id} from here.
export interface DeficiencyAnalyzeAccepted {
  run_id: number;
  status: DeficiencyRunStatus;
}
