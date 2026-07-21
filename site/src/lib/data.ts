import registry from '../generated/registry.json';

export type Registry = typeof registry;
export type Benchmark = Registry['benchmarks'][number];
export type Work = Registry['works'][number];
export type Model = Registry['models'][number];
export type EvaluationRun = Registry['evaluation_runs'][number];

export const data = registry;
export const benchmarkMap = new Map(data.benchmarks.map((item) => [item.id, item]));
export const workMap = new Map(data.works.map((item) => [item.id, item]));
export const modelMap = new Map(data.models.map((item) => [item.id, item]));
export const runMap = new Map(data.evaluation_runs.map((item) => [item.id, item]));

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

export function familyBenchmarks(): Benchmark[] {
  return data.benchmarks.filter((benchmark) => benchmark.parent_id === null);
}
