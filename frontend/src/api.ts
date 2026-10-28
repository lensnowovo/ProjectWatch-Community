export type Importance = "major" | "notable" | "routine";
export type Category = "company" | "competitor" | "industry" | "policy" | "paper" | "trial" | "patent";

export interface Ref {
  url?: string;
  read_scope?: string;
  marker?: string;
  source_id: string;
  source_name: string;
  locator: string;
}
export interface Point {
  text: string;
  refs: Ref[];
  date?: string;
  status?: string;
}
export interface Profile {
  origin?: { kind: "agent"; producer: string; received_at: string; batch_id: string };
  one_liner: string;
  industry: string;
  stage: string;
  summary: string;
  sections: { title: string; points: Point[] }[];
  highlights: Point[];
  risks: Point[];
  milestones: Point[];
  questions: string[];
  documents: number;
  stale_removed?: number;
  updated_at: string;
}
export interface BriefRef {
  item_id: string;
  title: string;
  url: string;
}
export interface Brief {
  origin?: Profile["origin"];
  dependency?: { status: "current" | "stale"; stale_reason?: string } | null;
  headline: string;
  tone: "positive" | "neutral" | "watch" | "negative";
  summary: string;
  changes: { text: string; refs: BriefRef[] }[];
  watch: { text: string; refs: BriefRef[] }[];
  stale_removed?: number;
  outlook?: { opportunities?: BriefEntry[]; threats?: BriefEntry[]; verify?: BriefEntry[] };
  updated_at: string;
}
export interface BriefEntry { text: string; refs: BriefRef[] }
export interface Project {
  execution_mode?: "api" | "agent";
  id: string;
  name: string;
  legal_name: string | null;
  short_name: string | null;
  credit_code: string | null;
  website: string | null;
  founded: string | null;
  location: string | null;
  industry: string | null;
  description: string | null;
  stage: string | null;
  aliases: string[];
  note: string | null;
  status: string | null;
  activated: boolean;
  config_version?: number;
  entity_source: string | null;
  last_internal_at: string | null;
  last_external_at: string | null;
  unread?: number;
  major_recent?: number;
  documents?: number;
  last_event?: string | null;
  last_event_at?: string | null;
  job_status?: string | null;
  job_runtime?: JobRuntime;
  nav_alerts?: string[];
  worker_state?: WorkerState;
  worker_health?: string;
  headline?: string;
  tone?: string;
  finance?: KeyNumbers;
}
export interface NumberCell { value: number; unit: string; period: string; refs: Ref[] }
export interface Runway {
  cash: number; cash_period: string; unit: string; refs: Ref[];
  months: number | null; status: "ok" | "short" | "positive" | "unknown"; burn?: number; text: string;
  method?: RunwayMethod;
  alternatives?: { method: RunwayMethod; label: string; text: string; burn: number; months: number | null; refs: Ref[] }[];
}
export type RunwayMethod = "cash_change" | "operating_cash_flow" | "net_profit";
export interface KeyNumbers {
  revenue: NumberCell | null; revenue_growth: number | null; net_profit: NumberCell | null;
  cash: NumberCell | null; runway: Runway | null;
}
export interface RatioRow {
  name: string; unit: string;
  cells: Record<string, { value: number; basis: "actual" | "forecast" | "unknown"; formula: string; refs: Ref[] }>;
}
export interface FinanceView {
  sections: Record<string, "income" | "balance" | "cashflow" | "ratio" | "financial_other" | "operating">;
  ratios: RatioRow[];
  key_numbers: KeyNumbers;
  layout?: StatementLayout[];
}
export interface StatementLayout {
  key: "income" | "balance" | "cashflow"; title: string;
  groups: { title: string; items: { name: string; label: string; total: boolean; key?: boolean }[] }[];
}
export interface EvidenceRef { id: string; title: string; url: string; source: string; date: string; quote?: string; source_id?: string; source_name?: string; locator?: string }
export interface LandscapeCell { text: string; refs: EvidenceRef[]; from_news?: boolean }
export interface Landscape {
  generated_at: string; updated_at: string; with_search: boolean; evidence_count: number; errors: string[];
  competition?: {
    summary: string; summary_refs?: EvidenceRef[]; dimensions: string[];
    companies: { name: string; is_self: boolean; cells: Record<string, LandscapeCell>; threat?: "high" | "medium" | "low" | ""; threat_reason?: string; refs?: EvidenceRef[]; from_plan?: boolean; move_count?: number }[];
    candidates?: { name: string; review_status: "pending"; refs: EvidenceRef[] }[];
    profiles?: { name: string; cells: Record<string, LandscapeCell> }[];
    projection_version?: string;
    claims: { claim: string; verdict: "supported" | "questioned" | "challenged" | "unknown"; text: string; refs: EvidenceRef[] }[];
  };
  industry?: {
    industry: string; heat: "升温" | "中性" | "平稳" | "降温" | "不确定"; heat_reason: string;
    heat_index?: { label: "升温" | "中性" | "降温"; note: string; window_days: number; baseline_days: number;
      signals: { key: string; name: string; recent: number; baseline: number; trend: "up" | "flat" | "down" | "few" }[] }; overview: string; heat_refs?: EvidenceRef[]; overview_refs?: EvidenceRef[];
    facts: { kind: string; text: string; refs: EvidenceRef[] }[];
    metrics: { name: string; period: string; value: number; unit: string; basis?: "forecast" | "reported"; refs: EvidenceRef[] }[];
    funding: { company: string; round: string; amount: string; date: string; refs: EvidenceRef[] }[];
    implications: { text: string; refs: EvidenceRef[] }[];
  };
  moves: { id: string; title: string; url: string; source: string; date: string; company: string; type: string; importance: Importance; impact: string;
    topic?: string; mentioned_companies?: string[]; attribution_status?: "pending" }[];
}
export interface Metric {
  id: string;
  source_id: string;
  source_name: string | null;
  name: string;
  category: "financial" | "operating";
  period: string;
  value: number | null;
  unit: string;
  basis: "actual" | "forecast" | "budget" | "unknown";
  scope: string;
  locator: string;
  quote: string;
  std_name?: string;
  period_key?: string;
  value_norm?: number | null;
  norm_unit?: string;
  currency?: string;
  source_created_at?: string | null;
  created_at?: string;
}
export interface MetricRef extends Ref {
  value?: number | null;
  unit?: string;
  period?: string;
}
export interface MetricIssue {
  kind: "conflict" | "sanity";
  severity: "high" | "medium";
  text: string;
  refs: MetricRef[];
}
export interface DocChangeEntry {
  kind: string;
  text: string;
  refs: MetricRef[];
  direction?: "up" | "down";
}
export interface DocChange {
  id: string;
  source_ids: string[];
  created_at: string;
  documents: string[];
  metrics: DocChangeEntry[];
  texts: DocChangeEntry[];
}
export interface Item {
  id: string;
  project_id: string;
  project_name: string;
  category: Category;
  title: string;
  url: string;
  source: string;
  snippet: string;
  published_at: string | null;
  collected_at: string;
  importance: Importance;
  summary: string;
  impact: string;
  status: "new" | "read" | "starred" | "dismissed";
  extra: Record<string, unknown>;
  /** Reports from different outlets grouped into this event. */
  sources?: EventSource[];
  source_count?: number;
}
export interface EventSource {
  extra?: { agent_result?: Profile["origin"]; read_scope?: string };
  id: string;
  title: string;
  url: string;
  source: string;
  published_at: string | null;
  collected_at: string;
  body_status: string;
}
interface EventDTO {
  id: string;
  project_id: string;
  project_name: string;
  category: Category;
  title: string;
  url: string;
  source: string;
  summary: string;
  impact: string;
  importance: Importance;
  status: Item["status"];
  event_at: string | null;
  created_at: string;
  sources: EventSource[];
  source_count: number;
}
const eventToItem = (e: EventDTO): Item => ({
  id: e.id, project_id: e.project_id, project_name: e.project_name, category: e.category,
  title: e.title, url: e.url, source: e.source, snippet: "", published_at: e.event_at ?? (e.sources[0]?.extra?.agent_result ? e.sources[0]?.published_at : null),
  collected_at: e.created_at, importance: e.importance, summary: e.summary, impact: e.impact,
  status: e.status, extra: { ...e.sources[0]?.extra, date_basis: e.event_at ? "event" : "reported" }, sources: e.sources, source_count: e.source_count,
});
export type WorkerState = "running" | "stopping" | "stopped";

/** Why one event matters: excerpts → plan points it hit → question assessments and AI judgment points citing it. */
export interface EventContext {
  event: { id: string; title: string; summary: string; impact: string; category: Category; importance: Importance; event_at: string | null; created_at: string };
  excerpts: { title: string; url: string; source: string; date: string; text: string }[];
  hits: { id: string; text: string; section: string; section_label: string; ring: string; ring_label: string;
    objects: string[]; reason: string; hop: string; parent: string; status: string }[];
  assessments: { watch_id: string; question: string; status: string; status_label: string; note: string; actor: string; created_at: string }[];
  judgments: { part: string; label: string; verdict: string; verdict_label: string; sign: "+" | "−"; text: string }[];
}
/** 深度分析: AI judgments over numbered sources; every point carries at least one reference. */
export interface InsightRef {
  read_scope?: "summary" | "partial" | "full";
  code: string; kind: "internal" | "public"; label: string; text: string; date?: string;
  url?: string; source?: string; source_id?: string; source_name?: string; locator?: string;
}
export interface InsightPoint { text: string; refs: InsightRef[] }
export type Confidence = "high" | "medium" | "low";
export interface InsightAnalysis {
  headline: string;
  position: { verdict: string; confidence: Confidence; summary: string; peers: string[]; for: InsightPoint[]; against: InsightPoint[]; change_if: string[] };
  technology: { route: string; maturity: string; consensus: string; confidence: Confidence; summary: string;
    academic: InsightPoint[]; industry: InsightPoint[]; risks: InsightPoint[] };
  industry: { verdict: string; confidence: Confidence; summary: string; drivers: InsightPoint[]; headwinds: InsightPoint[] };
  /** 公司本体 and 产业链 judgments; absent on analyses made before they were added. */
  operations?: RingJudgment;
  chain?: RingJudgment;
  gaps: string[];
  cited: InsightRef[];
}
export interface RingJudgment { verdict: string; confidence: Confidence; summary: string; for: InsightPoint[]; against: InsightPoint[]; change_if: string[] }
export interface InsightView {
  generation_available?: boolean;
  record: { id?: string; status?: string; dependency?: { status: string }; analysis: InsightAnalysis; generated_at: string; counts: Record<string, number>; total: number; model: string; origin?: Profile["origin"] } | null;
  history: { generated_at: string; headline: string; position: string; maturity: string; consensus: string; industry: string }[];
  job: { id: string; status: Job["status"]; error: string | null; created_at: string } | null;
  model_ready: boolean;
  labels: Record<"position" | "maturity" | "consensus" | "industry" | "operations" | "chain", Record<string, string>>;
  sources: Record<string, string>;
}
/** 监控方案 item: a tracked question with its scope, reasons, cadence and status. */
export interface PlanItem {
  lifecycle_version?: string; visibility?: string;
  id: string; project_id: string; text: string; label: string; queries: string[]; criteria: string; alert: boolean;
  axis: string; objects: string[]; reason: string; current_state: string; sources: string; cadence: string;
  trigger_rule: string; exit_rule: string; tier: string; status: string; plan_origin: string; origin: string;
  created_at: string; updated_at: string; last_run_at: string; last_hit_at: string | null; hits: number;
  /** Framework section this item grows from. */
  section: string;
  /** Relation chain: 0 = the company, 1 = directly related, 2 = second order; parent is the point it extends. */
  hop?: string; parent_id?: string;
  scope_kind?: "" | "relation" | "question"; relation_type?: string; relation_target?: string; relation_aliases?: string[];
  /** Display module and its parameters (see plan_templates.MODULES). */
  view: string; view_params: Record<string, unknown>;
}
export interface TopicResearchView {
  job: Pick<Job, 'id' | 'status' | 'created_at' | 'finished_at'> | null;
  question: { text: string; exit_rule: string; lifecycle_version: string } | null;
  scope_changed: boolean;
  result: { status: 'partial' | 'complete'; collection_status: 'partial' | 'complete' | null;
    answer_status: 'answered' | 'insufficient'; text: string; gaps: string[];
    sources: { ref: string; title: string; url: string; date: string; context: string; context_kind: string }[] } | null;
}
export interface ModuleHit { id?: string; title: string; url?: string; date: string; importance?: string; source?: string; planned?: boolean }
export type PlanModule =
  | { view: "evidence"; items: ModuleHit[]; total: number }
  | { view: "timeline"; points: ModuleHit[] }
  | { view: "peer_table"; rows: { name: string; latest: { title: string; category: string; at: string } | null; recent_90d: number }[] }
  | { view: "metric_series"; metric: string; unit: string; target?: number | null; target_label?: string; source_hint?: string;
      points: { id: string; period: string; value: number; unit: string; note: string; url: string; actor: string }[] }
  | { view: "commitments"; rows: { claim: string; source: string; due: string; progress: string; evidence: ModuleHit | null }[] };
export interface PlanTemplate { key: string; label: string; method: string; sections: Record<string, string>;
  focus: { section: string; text: string; view: string; cadence: string }[] }
export interface PlanSection { key: string; label: string; default_baseline: string; baseline: string; known: string; coverage: string;
  /** Which of the four rings (公司本体 / 竞争 / 产业链 / 环境) this dimension belongs to. */
  ring?: string }
export interface PlanMeta { understanding?: string; method?: string; in_scope?: string[]; out_of_scope?: string[]; updated_at?: string; templates?: string[]; review?: "pending" | "done" }
export interface PlanChange {
  id: string; watch_id: string; item: string; action: "add" | "modify" | "reduce" | "retire" | "restore" | "meta" | "regroup";
  actor: "system" | "human" | "agent"; reason: string; before: Partial<PlanItem>; after: Partial<PlanItem>;
  created_at: string; reverted_at: string | null;
  state?: "applied" | "proposed" | "accepted" | "rejected";
  /** What a system change rests on: numbered events (E), material changes (M) or analysis gaps (G). */
  evidence?: { ref: string; kind?: string; title?: string; text?: string }[];
}
export type SourceSelection = 'enabled' | 'disabled' | 'not_applicable';
export interface CollectionSourceDecision {
  id: string; adapter: string; version: number; selection: SourceSelection; reason: string;
  actor: 'human'; basis: 'human_confirmation'; created_at: string;
}
export interface CollectionSourceChoices {
  adapters: string[]; decisions: Record<string, CollectionSourceDecision>;
}

export interface MonitoringPlanView {
  collection_report?: CollectionReport | null;
  tree_signals?: Record<string, { count: number; recorded_at: string; fresh: boolean;
    title: string; url: string; basis: string }>;
  last_round?: { job_id: string; completed_at: string; coverage: "partial" | "complete";
    assessed_events: number; evidence_only: number; object_changes: number;
    judgment_change: "first" | "changed" | "unchanged" | "unavailable"; headline: string;
    stopped: number; active: number; waiting: number; standing: number; unfulfilled: number; pending: number; gaps: string[];
    stop_reason?: string; stop_details?: { reason: string; count: number }[];
    outputs?: { saved_entries: number; revision_applied: number; revision_added: number;
      revision_proposed: number; revision_dropped: number } } | null;
  relation_updates?: Record<string, { count: number; items: {
    event_id: string; title: string; url: string; source: string; date: string;
    basis: "summary" | "full_body" | "partial_body" | "search_summary"; matched_term: string; excerpt: string;
  }[] }>;
  explorations?: Record<string, { state: string; reason: string; label: string }>;
  items: PlanItem[]; changes: PlanChange[]; proposals: PlanChange[]; summary: Record<string, number>; max_items: number;
  framework: PlanSection[]; meta: PlanMeta;
  rings?: { key: string; label: string }[];
  state?: { phase: "empty" | "growing" | "stable"; label: string; revisions: number;
    /** The last revision round: what triggered it and what it changed. */
    last?: { at: string; trigger: string; added: number; noted: number; applied: number; proposed: number; dropped: number } };
  /** One line per focus point: current understanding, else newest on-topic hit, else search status. */
  latest?: Record<string, { kind: "note" | "hit" | "none"; text: string; date: string; url: string; count: number; fresh: boolean }>;
  week?: { new_items: number; fresh: number };
  /** 常规采集 per section: standing channels with their recent volume and state. */
  coverage?: Record<string, { key: string; label: string; count: number | null; unit: string; state: "on" | "off" | "error" | "pending"; window: string;
    execution?: string; capabilities?: string[]; due_count?: number; query_count?: number; round_id?: string; planned_at?: string;
    active?: boolean; job_status?: string; observed_at?: string }[]>;
  limits?: { max_hop: number; per_section: number };
  modules: Record<string, PlanModule>; module_labels: Record<string, string>; templates: PlanTemplate[];
  health?: { items: number; ran_14d: number; hit_30d: number; never_hit: number; system_added: number; system_reverted: number;
    system_notes: number; proposals: Record<"proposed" | "accepted" | "rejected", number> };
  labels: Record<"axis" | "cadence" | "tier" | "status" | "origin" | "coverage", Record<string, string>>;
}

export interface JobRuntime {
  activity?: {
    records_saved?: number | null;
    model_requests: number; model_completed: number; search_completed: number;
    pending_kind: string | null; pending_seconds: number | null;
    validation_attempts: number; validation_reasons: string[]; usage_missing_calls: number;
    input_tokens: number | null; output_tokens: number | null;
    cache_read_tokens: number | null; cache_creation_tokens: number | null;
  };
  state: string; label: string; phase?: "collect" | "read" | "judge" | "revise" | null;
  progress_at: string | null; heartbeat_at: string | null;
  progress_age_seconds: number | null; yield_count: number; recovery_attempts: number;
}
export interface TaskJob {
  runtime?: JobRuntime;
  id: string; project_id: string; project_name: string; kind: Job["kind"]; label: string; status: Job["status"];
  stage: string; error: string; created_at: string; started_at: string | null; finished_at: string | null;
  seconds: number | null; calls: number; unknown: number; model_failure?: number; position?: number;
  /** Failed or partial tasks: what happened, what to do, and which button does it. */
  advice?: { why: string; todo: string; action: "recover" | "rerun" | "settings" | "none" };
  dismissed?: boolean;
}
export interface TaskDocument {
  id: string; project_id: string; project_name: string; name: string; created_at: string; fragments: number;
  steps: { key: string; label: string; state: "done" | "running" | "waiting" | "failed" | "blocked" | "partial"; note: string }[];
}
export interface TaskBoard {
  runtime?: { state: string; label: string; restart_required: boolean };
  worker: WorkerState; model_ready: boolean;
  summary: { running: number; queued: number; failed_24h: number; done_24h: number; partial_24h?: number };
  jobs: TaskJob[]; documents: TaskDocument[];
}
export interface Job {
  runtime?: JobRuntime;
  dismissed?: boolean;
  id: string;
  kind: "internal" | "external" | "external_followup" | "brief" | "public_profile" | "landscape" | "business" | "competition" | "details" | "commercial" | "insight" | "scope";
  status: "queued" | "running" | "done" | "failed";
  stage: string;
  error: string;
  created_at: string;
  finished_at: string | null;
}
export interface RecoveryPreview {
  job_id: string;
  kind: Job['kind'];
  version: string;
  can_recover: boolean;
  reason: string;
  requests: Record<string, number>;
  requires_duplicate_cost_consent: boolean;
  job_status: string;
  mode: 'resume' | 'checkpoint_continuation' | 'model_continuation';
  notice: string;
  continuation_job_id?: string | null;
}
export interface Plan {
  company_queries: string[];
  people: string[];
  competitors: { name: string; reason: string }[];
  industry_queries: string[];
  policy_queries: string[];
  paper_queries: string[];
  affiliation_names: string[];
  is_biomed: boolean;
  trial_sponsors: string[];
  patent_assignees: string[];
  has_search?: boolean;
  has_registry?: boolean;
  last_report?: Record<string, { checked: number; added: number; errors: string[]; execution?: string; count_kind?: string }>;
  round_id?: string;
  collection_report?: { schema_version: number; job_id: string; mode: string | null; planned_at: string | null; status: string } | null;
  updated_at: string;
}
export interface DocumentInfo {
  id: string;
  name: string;
  created_at: string;
  status: string;
  error: string | null;
  fragments?: number;
}
// Registry overlaps are source record comparisons, never competition verdicts.
export type RegistryTrial = { nct: string; url: string; product_name: string; phase: string; status: string;
  conditions: string[]; indication_basis: "study" | "arm" | "unattributed" };
export type RegistryOverlap = {
  product_id: string; name: string; aliases?: string[]; level: "same_target_and_modality" | "same_modality" | "same_target";
  shared_targets: string[]; shared_modalities: string[]; cell_source: "same" | "different" | "unknown";
  indication_overlap?: "same" | "different" | "unknown"; shared_indication_families?: string[]; indication_families?: string[];
  indications: string[]; trials?: RegistryTrial[];
  phases: string[]; statuses: string[]; nct_ids: string[]; urls: string[]; evidence_ids: string[];
};
export type RegistryResearch = {
  status: "completed" | "partial" | "failed" | "not_applicable"; study_count: number;
  excluded: { non_industry: number; self: number; user_removed: number; own_product?: number; grouped_alternatives?: number };
  target_products?: { product_id: string; product_name: string; targets: Record<string, string>; modalities: Record<string, string>;
    basis: "cited_facts" | "own_registry_record"; anchor_nct_ids: string[]; indication_families?: string[];
    indication_basis?: ("cited_text" | "own_registry_record")[] }[];
  peers: { id: string; name: string; products: RegistryOverlap[] }[];
};

export interface CollectionReport {
  schema_version: number; job_id: string; job_status: string; observed_at: string; active: boolean;
  status: string; mode: string | null; planned_at: string | null;
  view_state: 'awaiting_plan' | 'unavailable' | 'receipts';
  channels: Record<string, {execution: string; checked: number; added: number; errors: string[]}>;
}

export interface ProjectDetail {
  collection_report?: CollectionReport | null;
  /** Read-only saved content revisions, not progress percentages or task outcomes. */
  dossier_saved?: { version: 1; relations: { revision: string; count: number }; judgment: { revision: string; count: number } };
  /** Frontend-only: last successful snapshot retained after a read failure. */
  status_stale?: boolean;
  project: Project;
  capabilities?: { insight_generation: boolean; insight_projections: boolean };
  execution?: { analysis: WorkerState; parsing: WorkerState; health?: { state: string; label: string; restart_required: boolean } };
  monitoring_progress?: MonitoringProgress;
  profile: Profile | null;
  brief: Brief | null;
  plan: Plan | null;
  metrics: Metric[];
  counts: Partial<Record<Category, number>>;
  jobs: Job[];
  documents: DocumentInfo[];
  model_ready: boolean;
  search_ready?: boolean;
  registry_ready?: boolean;
  pending_documents: number;
  issues?: MetricIssue[];
  doc_changes?: DocChange[];
  body_coverage?: BodyCoverage;
  ocr?: { available: boolean; engine: string };
  public_profile?: PublicProfile | null;
  trials?: Trial[];
  finance?: FinanceView;
  landscape?: Landscape | null;
  relationships?: { items: ScopedRelationship[]; exclusions: ScopedRelationship[] };
  detail_summary?: { status: "extracted" | "partial" | "superseded"; fact_count: number; rejected: number; unquantified: number } | null;
  competition_research?: { status: string; candidate_count: number; researched_count: number; coverage_complete: boolean;
    candidates?: { id: string; name: string; origin?: "search" | "registry"; basis?: string }[]; registry?: RegistryResearch;
    web_peer_research?: string } | null;
  observations?: { items: { fact_id: string; kind: "business_relation" | "policy" | "event" | "milestone" | "funding_fact"; peer_name?: string; name?: string;
    role?: string; stage?: string; state?: string; occurred_at?: string; support?: string; applicability?: string; missing: string[];
    measure?: string; raw_value?: string | null; raw_unit?: string; currency?: string; period?: string; consolidation_scope?: string;
    basis?: string; milestone_name?: string | null; phase?: string;
    refs: { evidence_version_id: string; part_index: number; start: number; end: number; quote: string }[] }[]; truncated: boolean };
  activity?: { days: number; major: number; notable: number; routine: number };
  event_states?: { items: { subject_id: string; channel: string; name: string; period: string; state: string | null;
    support: string; effective_at: string | null; history: { fact_id: string; state: string; occurred_at: string | null; support: string; refs?: LocatedEvidence[] }[];
    history_truncated: boolean }[]; truncated: boolean };
  milestones?: { items: { subject_id: string; name: string; phase: string | null; support: string;
    original_dates: string[]; adjusted_dates: string[]; actual_dates: string[];
    history: { fact_id: string; phase: string; occurred_at: string | null; support: string; refs: LocatedEvidence[] }[];
    history_truncated: boolean }[]; truncated: boolean };
  concerns?: { items: { id: string; title: string; status: string; refs: LocatedEvidence[];
    feedback_history: { status: string; reason?: string; at: string }[] }[]; truncated: boolean };
}
export interface LocatedEvidence { evidence_version_id: string; part_index: number; start: number; end: number }
export interface ScopedRelationship {
  scope_key: string;
  subject_id?: string;
  peer_id?: string;
  subject_product_id?: string | null;
  peer_product_id?: string | null;
  peer_name: string;
  subject_product_name: string;
  peer_product_name: string;
  origin: "user_override" | "auto_research";
  support: "user_specified" | "pending" | "supported" | "disputed";
  relation_type: "direct" | "substitute" | "adjacent" | "unknown" | "unrelated";
  reason?: string;
  status?: string;
  scope_identity_changed?: boolean;
  rationale?: string;
  missing?: string[];
  company_wide?: boolean;
  peer_names?: string[];
  created_at?: string;
  basis_facts?: { fact_id: string; subject_id: string; predicate: string; value: string; basis: string; refs: LocatedEvidence[] }[];
  basis_facts_truncated?: boolean;
}
export interface Trial {
  id: string; title: string; url: string; phase: string; status: string; conditions: string[]; sponsor: string; updated_at: string | null;
}
export interface PublicFact {
  kind: "business" | "product" | "person" | "founded" | "location" | "financing" | "milestone";
  label: string;
  text: string;
  date: string;
  refs: { url: string; title: string; source: string; quote: string; published_at: string | null }[];
}
export interface PublicProfile {
  facts: PublicFact[];
  sources: number;
  updated_at: string;
  errors: string[];
}
export interface BodyCoverage {
  total: number;
  article: number;
  twin: number;
  summary: number;
  none: number;
  declined?: number;
  outlets: { host: string; read: number; total: number }[];
}
export interface Candidate {
  legal_name: string;
  short_name: string;
  credit_code: string;
  website: string;
  founded: string;
  location: string;
  industry: string;
  description: string;
  stage: string;
  aliases: string[];
  confidence: number;
  reason: string;
  evidence: string[];
  registry?: boolean;
}
export interface ResolveResult {
  query: string;
  method: "registry" | "ai_search" | "manual";
  candidates: Candidate[];
  notes: string;
}
export interface Preset {
  id: string;
  label: string;
  protocol: "openai" | "anthropic";
  base_url: string;
  model: string;
  hint?: string;
}
export interface SettingsView {
  active_model_profile?: string;
  model_profiles?: (SettingsView['model'] & { id: string })[];
  preferred_mode?: "api" | "agent";
  model: { protocol: string; base_url: string; model: string; api_key: string; max_output_tokens?: number; ready: boolean };
  search: { provider: string; api_key: string; providers?: string[]; keys?: Record<string, string>; ready?: boolean; mode?: string; google_news?: boolean };
  registry: { provider: string; api_key: string };
  commercial?: { providers: string[]; keys: Record<string, string>; ready: string[]; daily_limit: number; model_use?: boolean;
    presets: { id: string; label: string; description: string; endpoint: string }[] };
  notify: NotifyView;
  proxy_url: string;
  refresh_hours: number;
  collection_overlap_days?: number;
  collection_baseline_days?: number;
  presets: Preset[];
}
export interface CommercialView {
  investment_status: "watching" | "invested";
  private: boolean;
  daily_queries: { used: number; limit: number; remaining: number };
  different_conventions: boolean;
  notice: string;
  records: { kind: "funding" | "news"; company: string; provider: string; title: string; date: string;
    amount: string; round: string; investors: string; url: string; excerpt: string;
    collected_at: string; stale: boolean; relationship: string }[];
  sources: { provider: string; label: string; company: string; relationship: string; enabled: boolean;
    status: string; last_checked: string | null; stale: boolean; rejected: number; truncated: boolean }[];
}
export interface NotifyView {
  kind: "none" | "wecom" | "feishu" | "dingtalk";
  webhook_url: string;
  weekly: boolean;
  weekday: number;
  hour: number;
  instant_major: boolean;
  ready?: boolean;
}
export interface DigestEvent {
  title: string; summary: string; impact: string; category: Category; importance: Importance;
  url: string; source: string; source_count: number; when: string;
}
export interface MonitoringProgress {
  state: "queued" | "running" | "failed" | "partial" | "unobserved" | "stale" | "paused" | "inactive" | "observed";
  label: string; empty_text: string; completed_at: string | null;
  first_result_at?: string | null;
  /** External rounds started for this project; the latest one is round N. */
  round?: number;
  source_failures: number; pending_items: number; full_coverage: false;
}
export interface DigestPending {
  id: string; name: string; state: MonitoringProgress["state"]; label: string;
}
export interface DigestProject {
  id: string; name: string; legal_name: string; tone: string; headline: string;
  major: number; notable: number; events: DigestEvent[]; more_events: number;
  doc_changes: { documents: string[]; items: string[] }[]; issues: string[];
  monitoring_progress?: MonitoringProgress;
}
export interface Digest {
  days: number; start: string; end: string; projects: DigestProject[]; quiet: string[];
  incomplete?: DigestPending[];
  totals: { projects: number; major: number; notable: number; doc_changes: number };
  markdown: string;
  attention?: Attention;
  notify: { ready: boolean; last: { at: string; sent: string[]; error: string } | null };
}
export type AttentionKind = "crosscheck" | "silence" | "watch" | "site";
export interface AttentionEntry {
  kind: AttentionKind; level: "high" | "watch" | "medium" | "info" | "low"; title: string; detail: string;
  question?: string; url?: string; at: string | null; ref: string | null; tab: string;
}
export interface AttentionProject {
  id: string; name: string; score: number; counts: Record<AttentionKind, number>; entries: AttentionEntry[];
}
export interface Attention {
  days: number; projects: AttentionProject[]; totals: Record<AttentionKind, number>; labels: Record<AttentionKind, string>;
}
export interface Fragment {
  id: string;
  ordinal: number;
  locator: string;
  text: string;
}

export interface SitePage {
  id: string; url: string; label: string; kind: string; added_by: "auto" | "user";
  status: "pending" | "ok" | "unreadable" | "robots_disallowed" | "error"; error: string;
  last_checked: string | null; last_changed: string | null;
}
export interface SiteChange {
  id: string; page_id: string; url: string; label: string; detected_at: string;
  added: string[]; removed: string[]; event_id: string | null; importance: Importance | null; impact: string | null;
}
export interface Rule { id: string; kind: "block_source" | "exclude_entity" | "exclude_term"; value: string; note: string; hits: number; created_at: string }
export interface Watchpoint { id: string; text: string; label: string; queries: string[]; criteria: string; alert: boolean; hits: number; last_hit_at: string | null; created_at: string; origin?: "human" | "ai" }
export type QuestionStatus = "progress" | "setback" | "unchanged" | "insufficient";
export interface QuestionJudgement {
  id: string; actor: "system" | "human" | "agent"; status: QuestionStatus; status_label: string;
  note: string; event_ids: string[]; created_at: string;
}
export interface QuestionEvent { id: string; title: string; summary: string; importance: string; event_at: string | null; category: string }
export interface TrackedQuestion extends Watchpoint {
  origin: "human" | "ai"; history_count: number;
  latest: Partial<Record<"system" | "human" | "agent", QuestionJudgement>>;
  events: Record<string, QuestionEvent>;
}
export interface ThreadStep { stage: string; event_id: string; date: string; title: string; url: string }
export interface Thread {
  id: string; kind: string; title: string; steps: ThreadStep[]; stages: string[]; reached: number;
  status: "open" | "stalled" | "done"; last_at: string; next_step: string; updated_at: string;
  support?: "legacy_unverified"; current_stage?: string | null; stage_conflict?: boolean;
}
export interface MonitorState {
  pages: SitePage[]; changes: SiteChange[]; rules: Rule[]; watchpoints: Watchpoint[]; threads: Thread[];
  watch_labels: Record<string, string[]>; stages: Record<string, string[]>;
  site_discovery?: SiteDiscovery;
}
/** How the round looked for an official site when the project has none on file. */
export interface SiteDiscovery {
  status?: "found" | "not_found" | "no_search" | "search_failed" | "dismissed"; checked_at?: string; query?: string;
  url?: string; matched?: string; tried?: string[];
  /** Why each candidate was not adopted: robots = the site forbids automated reading. */
  rejected?: Record<string, "robots" | "no_name" | "unreadable" | "refused">;
}
export interface CrossCheck {
  id: string; kind: "mismatch" | "undisclosed" | "off_plan"; kind_label: string; severity: "high" | "medium" | "low" | "unrated";
  topic: string; public_says: string; internal_says: string; question: string; status: "open" | "resolved" | "dismissed";
  public_refs: { id: string; title: string; url: string; source: string; date: string }[];
  internal_refs: { label: string; refs: Ref[] }[];
  updated_at: string;
}
export interface SilenceSignal { kind: string; level: "watch" | "info"; title: string; detail: string }
export interface Signals {
  crosschecks: CrossCheck[]; silence: SilenceSignal[];
  cadence: { hours: number; mode: "active" | "normal" | "quiet"; reason: string };
  latency: { median_hours: number | null; n: number };
}
export type FeedbackKind = "irrelevant" | "wrong_entity" | "bad_source" | "importance";

export class ApiError extends Error {
  operationId?: string;
  operationStatus?: string;
}

async function request<T>(method: string, url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = { method, headers: {}, signal };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const response = await fetch(url, init);
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = data?.detail;
    const message = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d: { msg: string }) => d.msg).join("；")
      : `请求失败（${response.status}）`;
    const error = new ApiError(message);
    if (data?.code === "operation_held" && typeof data.operation_id === "string") {
      error.operationId = data.operation_id;
      error.operationStatus = data.operation_status;
    }
    throw error;
  }
  return data as T;
}

const upload = (file: File) => {
  const form = new FormData();
  form.append("file", file);
  return form;
};

export type EvidenceGrade = "公示" | "数据商" | "媒体" | "公司宣传";
export interface RaceStage { index: number; label: string; date: string; grade: EvidenceGrade;
  basis: "storyline" | "attributed_fact" | "title"; title: string; url: string; outlets?: string[];
  scope?: string; context?: { indication?: string[]; region?: string[] };
  previous?: { label: string; date: string } }
export interface RacePosition { sentence: string; ranked: boolean;
  parts: { kind: "position" | "capital" | "threat" | "opening"; text: string; who?: string }[];
  chips: { label: string; value: string; note: string; tone: "" | "up" | "down" | "flat" }[] }
export interface RaceFunding { round: string; amount: string; date: string; lead: string; grade: EvidenceGrade;
  providers: string[]; title: string; url: string; stale: boolean }
export interface RaceMove { title: string; url: string; date: string; grade: EvidenceGrade; importance: Importance; impact: string; sources: number }
export interface RaceRow { name: string; note: string; relation_type?: string; is_self: boolean; stage: RaceStage | null; pace_months: number | null;
  funding: RaceFunding | null; months_since_funding: number | null; move: RaceMove | null;
  rd: { papers: number | null; patents: number | null; collected: boolean };
  recent: { stage: boolean; funding: boolean; move: boolean };
  extras?: { team: RaceExtra[]; customers: (RaceExtra & { who: string; what: string })[] } }
export interface RaceExtra { text: string; date: string; source: string; url: string; grade: EvidenceGrade }
export interface QuantityRecord { company: string; metric: string; value: string; unit: string; raw_value: string;
  scope: Record<string, string[]>; title: string; quote: string; url: string; source: string; date: string }
export interface QuantityView { ranked: false; records: QuantityRecord[];
  groups: { scope: Record<string, string[]>; metric: string; unit: string; records: QuantityRecord[] }[] }
export interface RaceView { template: string; template_label: string; steps: string[]; rows: RaceRow[]; quantities?: QuantityView;
  comparable: number; staged?: number; generated_at: string; grades: EvidenceGrade[]; position: RacePosition }

export interface RedFlag { level: "高" | "中" | "低"; who: string; is_self: boolean;
  kind: "report" | "event_state" | "site_team" | "silence" | "commitment"; title: string; date: string; grade: EvidenceGrade | "";
  source: string; url: string; note: string }
export interface RedFlagsView { company: RedFlag[]; peers: RedFlag[]; counts: { company: number; peers: number; high: number };
  window_days: number; generated_at: string }

export interface PublicCommitment { fact_id: string; subject_name: string; statement: string; target_text: string;
  due_start: string; due_end: string; status: string; status_label: string; first_said_at: string; last_said_at: string;
  statements: number; due_changed: boolean; sources: { title: string; source: string; url: string }[];
  outcome_sources: { title: string; source: string; url: string }[] }
export interface CommitmentsView { items: PublicCommitment[]; basis: string; truncated: boolean }

export interface TrackCell { key: "demand" | "capital" | "price" | "policy"; label: string; direction: string;
  tone: "good" | "bad" | "neutral" | "unknown"; value: string; sub: string; series: { label: string; value: number }[];
  evidence: { title: string; url: string; source: string; date: string; grade: EvidenceGrade }[]; for_us: string; sources: number }
export interface TrackView { industry: string; cells: TrackCell[]; summary: string; balance: string; quarters: string[]; generated_at: string }

export interface TechRoute { term: string; own: boolean; evidence?: number }
export interface TechView { routes: TechRoute[]; source: "auto" | "user"; suggested: TechRoute[]; quarters: string[];
  rows: { term: string; own: boolean; values: number[]; kinds: Record<string, number>; total: number }[]; insight: string;
  entrants: { name: string; kind: string; first: string; what: string; title: string; url: string }[]; generated_at: string }

export interface UpdateView {
  state: "unchecked" | "not_configured" | "manual_install" | "agent_required" | "no_release" | "up_to_date" | "available" | "check_failed";
  message: string;
  running_version: string;
  current_commit: string;
  target_commit: string;
  target_version: string;
  handoff: string;
}

/** Which public-research executor the running app uses; a saved switch takes effect on restart. */
export interface ExecutorView {
  executor: "dsh" | "classic" | "unavailable";
  source: "bundled" | "custom" | "environment" | "setting" | "default" | "upgrade_preserved";
  reason: string | null; reason_text: string | null;
  dsh_version: string; node_version: string | null;
  configurable: boolean; saved_enabled: boolean | null;
}

export const api = {
  updates: () => request<UpdateView>("GET", "/api/studio/updates"),
  checkUpdates: () => request<UpdateView>("POST", "/api/studio/updates/check"),
  selectCompetitor: (id: string, expectedVersion: number, name: string, decision: "include" | "exclude" | "reset", peerId?: string) =>
    request("POST", `/api/studio/projects/${id}/competitors/selection`, {
      name, peer_id: peerId ?? null, decision, expected_version: expectedVersion, idempotency_key: crypto.randomUUID(),
    }),
  locatedExcerpt: (id: string, ref: LocatedEvidence) => request<{ quote: string; locator: string; read_scope: string }>("GET",
    `/api/studio/projects/${id}/evidence/${encodeURIComponent(ref.evidence_version_id)}/parts/${ref.part_index}?start=${ref.start}&end=${ref.end}`),
  concernFeedback: (id: string, body: { concern_id: string; status: "open" | "resolved" | "dismissed"; reason: string; expected_version: number; idempotency_key: string }) =>
    request("POST", `/api/studio/projects/${id}/concern-feedback`, body),
  correctRelationship: (id: string, expectedVersion: number, row: ScopedRelationship, decision: "exclude" | "reset") =>
    request("POST", `/api/studio/projects/${id}/relationships/corrections`, {
      expected_version: expectedVersion, idempotency_key: crypto.randomUUID(),
      changes: [{ subject_id: row.subject_id, peer_id: row.peer_id,
        subject_product_id: row.subject_product_id ?? null, peer_product_id: row.peer_product_id ?? null,
        decision, relation_type: row.relation_type ?? "unknown", reason: decision === "exclude" ? "用户排除该范围的竞争关系" : "用户撤销排除" }],
    }),
  settings: () => request<SettingsView>("GET", "/api/studio/settings"),
  instanceAgentConnections: () => request<{ connection_id: string; client: 'codex' | 'workbuddy'; configuration: string; destination: string; prompt: string }[]>(
    "GET", '/api/studio/agent-connections'),
  instanceAgentConnection: (client: "codex" | "workbuddy") => request<{ connection_id: string; configuration: string; destination: string; prompt: string }>(
    "POST", '/api/studio/agent-connection', { client }),
  agentConnection: (id: string, client: "codex" | "workbuddy", allowInternal: boolean, connectionId?: string) =>
    request<{ configuration: string; destination: string; prompt: string; grant_id: string; expires_at: number; status: string }>(
      "POST", `/api/studio/projects/${id}/agent-connection`, { client, allow_internal: allowInternal, ...(connectionId ? { connection_id: connectionId } : {}) }),
  agentGrants: (id: string) => request<{ id: string; label: string; revoked_at: string | null; expires_at: number; allow_internal: boolean }[]>("GET", `/api/studio/projects/${id}/agent-grants`),
  revokeAgent: (id: string, grant: string) => request("DELETE", `/api/studio/projects/${id}/agent-grants/${grant}`),
  createAgentProject: (name: string, drafts: string[]) => request<Project>("POST", "/api/studio/projects", {
    entity: { legal_name: name }, consent: true, execution_mode: "agent", draft_ids: drafts,
  }),
  saveSettings: (patch: unknown) => request<SettingsView>("PUT", "/api/studio/settings", patch),
  executor: () => request<ExecutorView>("GET", "/api/studio/settings/executor"),
  saveExecutor: (enabled: boolean) => request<ExecutorView>("PUT", "/api/studio/settings/executor", { enabled }),
  testSettings: () =>
    request<Record<string, { ok: boolean | null; message: string }>>("POST", "/api/studio/settings/test"),
  testSetup: () =>
    request<Record<string, { ok: boolean | null; message: string }>>("POST", "/api/studio/settings/test?scope=setup"),
  retryDirectOperation: (id: string) => request<{ id: string; status: string }>("POST",
    `/api/studio/operations/${encodeURIComponent(id)}/retry`, { accept_duplicate_cost: true }),
  readAll: (scope: { project_id?: string; days?: number }) =>
    request<{ updated: number }>("POST", "/api/studio/events/read-all", scope),
  attention: (days = 7) => request<Attention>("GET", `/api/studio/attention?days=${days}`),
  digest: (days: number) => request<Digest>("GET", `/api/studio/digest?days=${days}`),
  pushDigest: (days: number) => request<{ ok: boolean; message: string }>("POST", `/api/studio/digest/push?days=${days}`),
  testNotify: () => request<{ ok: boolean; message: string }>("POST", "/api/studio/notify/test"),
  usage: () => request<{ days: { day: string; calls: number }[]; total: { calls: number; input_tokens: number; output_tokens: number;
    known_calls: number; unknown_calls: number; legacy_calls: number;
    input_known_calls: number; output_known_calls: number; cache_read_tokens: number; cache_creation_tokens: number } }>(
    "GET", "/api/studio/usage"),
  draft: (file: File) => request<{ draft_id: string; filename: string; readable: boolean; excerpt: string }>(
    "POST", "/api/studio/drafts", upload(file)),
  resolve: (name: string, draftId?: string) =>
    request<ResolveResult>("POST", "/api/studio/resolve", { name, draft_id: draftId ?? null }),
  createProject: (entity: unknown, draftIds: string[], watchpoints: string[] = [], confirmedCompanyNames?: string[]) =>
    request<Project>("POST", "/api/studio/projects", { entity, consent: true, draft_ids: draftIds, watchpoints,
      ...(confirmedCompanyNames !== undefined ? { confirmed_company_names: confirmedCompanyNames } : {}) }),
  projects: () => request<Project[]>("GET", "/api/studio/projects"),
  project: (id: string) => request<ProjectDetail>("GET", `/api/studio/projects/${id}`),
  commercial: (id: string) => request<CommercialView>("GET", `/api/studio/projects/${id}/commercial`),
  race: (id: string, signal?: AbortSignal) => request<RaceView>("GET", `/api/studio/projects/${id}/race`, undefined, signal),
  tech: (id: string) => request<TechView>("GET", `/api/studio/projects/${id}/tech`),
  saveTech: (id: string, terms: string[] | null, own: string | null) =>
    request<TechView>("PUT", `/api/studio/projects/${id}/tech`, { terms, own }),
  track: (id: string) => request<TrackView>("GET", `/api/studio/projects/${id}/track`),
  redFlags: (id: string) => request<RedFlagsView>("GET", `/api/studio/projects/${id}/red-flags`),
  commitments: (id: string) => request<CommitmentsView>("GET", `/api/studio/projects/${id}/commitments`),
  commercialFocus: (id: string, investment_status: CommercialView["investment_status"]) =>
    request("PUT", `/api/studio/projects/${id}/commercial/preference`, { investment_status }),
  patchProject: (id: string, patch: unknown) => request<Project>("PATCH", `/api/studio/projects/${id}`, patch),
  activate: (id: string) => request<Project>("POST", `/api/studio/projects/${id}/activate`),
  uploadFile: (id: string, file: File) => request<{ source_id: string; parser_status: string; analysis_status: string }>("POST", `/api/studio/projects/${id}/files`, upload(file)),
  addLink: (id: string, url: string) => request<{ source_id: string; parser_status: string; analysis_status: string }>("POST", `/api/studio/projects/${id}/links`, { url }),
  retryFile: (id: string, sourceId: string) => request("POST", `/api/studio/projects/${id}/files/${sourceId}/retry`),
  insight: (id: string) => request<InsightView>("GET", `/api/studio/projects/${id}/insight`),
  eventContext: (projectId: string, eventId: string) => request<EventContext>("GET", `/api/studio/projects/${projectId}/events/${encodeURIComponent(eventId)}/context`),
  monitoringPlan: (id: string) => request<MonitoringPlanView>("GET", `/api/studio/projects/${id}/monitoring-plan`),
  topicResearch: (id: string, watchId: string) => request<TopicResearchView>('GET',
    `/api/studio/projects/${id}/research-topic/${encodeURIComponent(watchId)}`),
  startTopicResearch: (id: string, watchId: string, version: string) => request<Job>('POST',
    `/api/studio/projects/${id}/research-topic`, { watch_id: watchId, expected_version: version }),
  discoveredSources: (id: string, offset = 0) => request<DiscoveredSourcePage>('GET', `/api/studio/projects/${id}/discovered-sources?offset=${offset}`),
  collectionSources: (id: string) => request<CollectionSourceChoices>('GET', `/api/studio/projects/${id}/collection-sources`),
  chooseCollectionSource: (id: string, adapter: string, selection: SourceSelection, reason: string, expected_version: number) =>
    request<CollectionSourceDecision>('PUT', `/api/studio/projects/${id}/collection-sources/${encodeURIComponent(adapter)}`,
      { selection, reason, expected_version }),
  addPlanItem: (id: string, item: Partial<PlanItem>) => request<PlanItem>("POST", `/api/studio/projects/${id}/monitoring-plan/items`, item),
  updatePlanItem: (watchId: string, fields: Partial<PlanItem> & { note?: string }) =>
    request<PlanItem>("PATCH", `/api/studio/monitoring-plan/items/${watchId}`, fields),
  applyPlanTemplates: (id: string, templates: string[]) => request<unknown>("POST", `/api/studio/projects/${id}/monitoring-plan/templates`, { templates }),
  addMetricPoint: (watchId: string, point: { period: string; value: number; unit?: string; note?: string; url?: string }) =>
    request<unknown>("POST", `/api/studio/monitoring-plan/items/${watchId}/points`, point),
  removeMetricPoint: (pointId: string) => request<void>("DELETE", `/api/studio/monitoring-plan/points/${pointId}`),
  confirmPlanReview: (id: string) => request<unknown>("POST", `/api/studio/projects/${id}/monitoring-plan/review`),
  acceptPlanChange: (changeId: string) => request<unknown>("POST", `/api/studio/monitoring-plan/changes/${changeId}/accept`),
  rejectPlanChange: (changeId: string) => request<unknown>("POST", `/api/studio/monitoring-plan/changes/${changeId}/reject`),
  revertPlanChange: (changeId: string) => request<PlanItem>("POST", `/api/studio/monitoring-plan/changes/${changeId}/revert`),
  dismissTask: (jobId: string) => request<void>("POST", `/api/studio/tasks/${jobId}/dismiss`),
  tasks: (projectId?: string) => request<TaskBoard>("GET", `/api/studio/tasks${projectId ? `?project_id=${projectId}` : ""}`),
  removeProject: (id: string) => request<void>("DELETE", `/api/studio/projects/${id}`),
  removedProjects: () => request<{ id: string; name: string; legal_name: string | null; industry: string | null; stage: string | null; updated_at: string | null }[]>(
    "GET", "/api/studio/removed-projects"),
  restoreProject: (id: string) => request<Project>("POST", `/api/studio/projects/${id}/restore`),
  refresh: (id: string, kind: Job["kind"]) =>
    request<Job>("POST", `/api/studio/projects/${id}/refresh`, { kind }),
  recoveryPreview: (id: string, jobId: string) =>
    request<RecoveryPreview>('GET', `/api/studio/projects/${id}/jobs/${jobId}/recovery`),
  recoveryProject: (id: string) =>
    request<{ items: RecoveryPreview[] }>('GET', `/api/studio/projects/${id}/recovery`),
  recover: (id: string, jobId: string, version: string, key: string) =>
    request<{ job_id: string; status: string; recovery_id: string }>('POST',
      `/api/studio/projects/${id}/jobs/${jobId}/recovery`, {
        expected_version: version, idempotency_key: key, accept_duplicate_cost: true,
      }),
  feed: (params: Record<string, string | number | undefined>) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && query.set(k, String(v)));
    // Feed entries are events: one row per real-world event, with every outlet that reported it.
    return request<EventDTO[]>("GET", `/api/studio/events?${query}`).then((rows) => rows.map(eventToItem));
  },
  setItem: (id: string, status: Item["status"]) => request("PATCH", `/api/studio/events/${id}`, { status }),
  fragments: (sourceId: string) => request<Fragment[]>("GET", `/api/sources/${sourceId}/fragments`),
  monitor: (id: string) => request<MonitorState>("GET", `/api/studio/projects/${id}/monitor`),
  addPage: (id: string, url: string, label = "") => request<SitePage>("POST", `/api/studio/projects/${id}/pages`, { url, label }),
  removePage: (pageId: string) => request("DELETE", `/api/studio/site-pages/${pageId}`),
  feedback: (eventId: string, kind: FeedbackKind, value = "") =>
    request<{ ok: boolean; message: string; rule: Rule | null }>("POST", `/api/studio/events/${eventId}/feedback`, { kind, value }),
  addRule: (id: string, kind: Rule["kind"], value: string) => request<Rule>("POST", `/api/studio/projects/${id}/rules`, { kind, value }),
  removeRule: (ruleId: string) => request("DELETE", `/api/studio/rules/${ruleId}`),
  addWatchpoint: (id: string, text: string, alert = true) => request<Watchpoint>("POST", `/api/studio/projects/${id}/watchpoints`, { text, alert }),
  patchWatchpoint: (watchId: string, alert: boolean) => request("PATCH", `/api/studio/watchpoints/${watchId}`, { alert }),
  removeWatchpoint: (watchId: string) => request("DELETE", `/api/studio/watchpoints/${watchId}`),
  questions: (id: string) => request<{ questions: TrackedQuestion[]; statuses: Record<QuestionStatus, string> }>("GET", `/api/studio/projects/${id}/questions`),
  questionHistory: (id: string, watchId: string) => request<QuestionJudgement[]>("GET", `/api/studio/projects/${id}/questions/${watchId}/history`),
  judgeQuestion: (id: string, watchId: string, body: { status: QuestionStatus; note: string }) =>
    request<QuestionJudgement>("POST", `/api/studio/projects/${id}/questions/${watchId}/assessments`, body),
  signals: (id: string) => request<Signals>("GET", `/api/studio/projects/${id}/signals`),
  setCrossCheck: (checkId: string, status: CrossCheck["status"]) => request("PATCH", `/api/studio/crosschecks/${checkId}`, { status }),
  watchEvents: (watchId: string) => request<EventDTO[]>("GET", `/api/studio/watchpoints/${watchId}/events`).then((rows) => rows.map(eventToItem)),
};
export interface DiscoveredSourcePage {
  items: { id: string; title: string; url: string; source: string; published_at: string | null;
    collected_at: string; observed_at: string; evidence_version_id: string; source_type: string;
    read_scope: string; read_status: string | null; reading_basis: string | null;
    body_evidence_version_id: string | null; processing_status: 'pending' | 'organized' }[];
  total: number; pending: number; offset: number; next_offset: number | null;
}
