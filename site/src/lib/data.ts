import registry from '../generated/registry.json';

export type Registry = typeof registry;
type RawBenchmark = Registry['benchmarks'][number];
type RawEvaluationRun = Registry['evaluation_runs'][number];
type RawBenchmarkUse = Registry['benchmark_uses'][number];
export type FieldStatus = {
  path: string;
  status: 'provisional' | 'conflicted';
  confidence: string;
  reason: string;
  evidence_ids: string[];
};
export type BenchmarkVersion = {
  id: string;
  label: string;
  status: 'current' | 'active' | 'superseded' | 'rolling';
  release_date: string | null;
  as_of: string | null;
  task_counts: RawBenchmark['task_counts'];
  formal_tracks: string[];
  notes: string | null;
  evidence_ids: string[];
};
export type Resource = RawBenchmark['resources'][number] & {
  id?: string;
  last_checked?: string;
  pin?: { kind: string; value: string; url?: string | null } | null;
};
export type Benchmark = Omit<RawBenchmark, 'audit' | 'field_status' | 'resources'> & {
  audit: { status: 'legacy' | 'audited' | 'audited-with-caveats'; audited_date: string | null; unresolved_fields: number; notes?: string | null };
  field_status: FieldStatus[];
  versions?: BenchmarkVersion[];
  resources: Resource[];
};
type RawWork = Registry['works'][number];
export type WorkSourceVersion = RawWork['source_versions'][number] & {
  source_access?: 'open-url' | 'submitted-pdf' | 'metadata-only';
  content_sha256?: string | null;
  content_type?: string | null;
  retrieved_at?: string | null;
};
export type Work = Omit<RawWork, 'source_versions'> & {
  source_versions: WorkSourceVersion[];
  review_provenance?: {
    method: 'automated-double-pass';
    pipeline_version: string;
    prompt_version: string;
    source_version_id: string;
    extractor_model_requested: string;
    extractor_model_resolved: string;
    verifier_model_requested: string;
    verifier_model_resolved: string;
    generated_at: string;
  };
};
export type Model = Registry['models'][number];
export type EvaluationResult = RawEvaluationRun['results'][number] & {
  status?: 'verified' | 'provisional' | 'conflicted';
  confidence?: string;
  evidence_ids?: string[];
};
export type EvaluationRun = Omit<RawEvaluationRun, 'results'> & { results: EvaluationResult[]; model_ids: string[] };
export type BenchmarkUse = RawBenchmarkUse;

export const data = registry as Omit<Registry, 'benchmarks' | 'evaluation_runs' | 'works'> & {
  benchmarks: Benchmark[];
  evaluation_runs: EvaluationRun[];
  works: Work[];
};
export const benchmarkMap = new Map(data.benchmarks.map((item) => [item.id, item]));
export const workMap = new Map(data.works.map((item) => [item.id, item]));
export const modelMap = new Map(data.models.map((item) => [item.id, item]));
export const runMap = new Map(data.evaluation_runs.map((item) => [item.id, item]));
export const benchmarkUseMap = new Map(data.benchmark_uses.map((item) => [item.id, item]));
export const scientificTaskMap = new Map(data.scientific_tasks.map((item) => [item.id, item]));

export const termMaps = Object.fromEntries(
  Object.entries(data.taxonomies).map(([axis, terms]) => [axis, new Map(terms.map((term) => [term.id, term]))]),
) as Record<string, Map<string, { id: string; label: string; label_zh: string; definition: string }>>;

export function withBase(path = ''): string {
  const base = import.meta.env.BASE_URL.replace(/\/$/, '');
  const normalized = path.startsWith('/') ? path : `/${path}`;
  return `${base}${normalized}`;
}

export function providerSlug(provider: string): string {
  return provider.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

export function aliasSlug(alias: string): string {
  return alias.toLowerCase().replace(/_/g, '-').replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '');
}

export function reported(value: { value: unknown; reporting_status: string } | undefined): string {
  if (!value || value.reporting_status === 'not_reported') return 'Not reported';
  if (value.reporting_status === 'not_applicable') return 'Not applicable';
  if (typeof value.value === 'boolean') return value.value ? 'Yes' : 'No';
  if (Array.isArray(value.value)) return value.value.join(', ');
  return String(value.value);
}

export function metricLabel(run: EvaluationRun, metricId: string): string {
  return run.metrics.find((metric) => metric.metric_id === metricId)?.source_label ?? metricId;
}

export function fieldStatus(benchmark: Benchmark, path: string): FieldStatus | undefined {
  return (benchmark.field_status as FieldStatus[]).find((item) => item.path === path);
}

export function evidenceLocator(evidence: { locator: string | { type: string; value: string; note?: string | null } }): string {
  if (typeof evidence.locator === 'string') return evidence.locator;
  return `${evidence.locator.type}: ${evidence.locator.value}${evidence.locator.note ? ` (${evidence.locator.note})` : ''}`;
}

export function evidenceSourceId(evidence: { work_id?: string; source_id?: string }): string | undefined {
  return evidence.source_id ?? evidence.work_id;
}

export function familyBenchmarks(): Benchmark[] {
  return data.benchmarks.filter((benchmark) => benchmark.parent_id === null);
}

export function rootFamily(benchmark: Benchmark): Benchmark {
  let current = benchmark;
  while (current.parent_id) current = benchmarkMap.get(current.parent_id) ?? current;
  return current;
}

export function taskDescendantIds(taskId: string): string[] {
  return [
    taskId,
    ...data.scientific_tasks.filter((item) => item.parent_id === taskId).map((item) => item.id),
  ];
}
