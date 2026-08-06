import catalog20260805 from '../generated/nucleic-acid-results/2026-08-05/catalog.json';
import entities20260805 from '../generated/nucleic-acid-results/2026-08-05/entities.json';
import usage20260805 from '../generated/nucleic-acid-results/2026-08-05/usage-index.json';
import catalog20260806 from '../generated/nucleic-acid-results/2026-08-06/catalog.json';
import entities20260806 from '../generated/nucleic-acid-results/2026-08-06/entities.json';
import usage20260806 from '../generated/nucleic-acid-results/2026-08-06/usage-index.json';

export const NUCLEIC_SNAPSHOTS = ['2026-08-05', '2026-08-06'] as const;
export type NucleicSnapshot = typeof NUCLEIC_SNAPSHOTS[number];
export const NUCLEIC_SNAPSHOT: NucleicSnapshot = '2026-08-06';
export const NUCLEIC_SNAPSHOT_SOURCE_DATE = '2026-08-06';

export type NucleicRow = Record<string, unknown>;

export type NucleicCatalog = {
  schema_version: string;
  snapshot_date: string;
  literature_cutoff: string;
  benchmarks: NucleicRow[];
  tasks: NucleicRow[];
  tracks: NucleicRow[];
  task_benchmark: NucleicRow[];
  coverage: NucleicRow[];
  protocols: NucleicRow[];
  metrics: NucleicRow[];
  summaries: NucleicRow[];
  leaders: NucleicRow[];
  benchmark_crosswalk: NucleicRow[];
  task_crosswalk: NucleicRow[];
  track_coverage?: NucleicRow[];
};

export type NucleicEntities = {
  schema_version: string;
  snapshot_date: string;
  participants: NucleicRow[];
  configurations: NucleicRow[];
  works: NucleicRow[];
};
export type NucleicUsageIndex = {
  schema_version: string;
  snapshot_date: string;
  participant_protocols: Record<string, string[]>;
  participant_result_counts: Record<string, number>;
  configuration_protocols: Record<string, string[]>;
  protocol_result_counts: Record<string, number>;
  benchmark_protocols: Record<string, string[]>;
  task_protocols: Record<string, string[]>;
  track_protocols: Record<string, string[]>;
  work_protocols: Record<string, string[]>;
};

const SNAPSHOT_DATA = {
  '2026-08-05': { catalog: catalog20260805, entities: entities20260805, usage: usage20260805 },
  '2026-08-06': { catalog: catalog20260806, entities: entities20260806, usage: usage20260806 },
} as const;

export function nucleicDataFor(snapshot: string = NUCLEIC_SNAPSHOT) {
  const resolved = (NUCLEIC_SNAPSHOTS as readonly string[]).includes(snapshot) ? snapshot as NucleicSnapshot : NUCLEIC_SNAPSHOT;
  const data = SNAPSHOT_DATA[resolved];
  return {
    snapshot: resolved,
    catalog: data.catalog as unknown as NucleicCatalog,
    entities: data.entities as unknown as NucleicEntities,
    usage: data.usage as unknown as NucleicUsageIndex,
  };
}

export const nucleicCatalog = nucleicDataFor().catalog;
export const nucleicEntities = nucleicDataFor().entities;
export const nucleicUsageIndex = nucleicDataFor().usage;

export function text(row: NucleicRow | undefined, key: string, fallback = 'NR'): string {
  if (!row) return fallback;
  const value = row[key];
  if (value === undefined || value === null || value === '') return fallback;
  return String(value);
}

export function optionalText(row: NucleicRow | undefined, key: string): string | undefined {
  const value = row?.[key];
  if (value === undefined || value === null || value === '' || value === 'NR') return undefined;
  return String(value);
}

export function integer(row: NucleicRow | undefined, key: string): number {
  const value = Number(row?.[key] ?? 0);
  return Number.isFinite(value) ? value : 0;
}

export function bool(row: NucleicRow | undefined, key: string): boolean {
  return row?.[key] === true || String(row?.[key]).toLowerCase() === 'true';
}

export function splitList(row: NucleicRow | undefined, key: string): string[] {
  const value = optionalText(row, key);
  if (!value) return [];
  return value.split(/[;|]/).map((item) => item.trim()).filter(Boolean);
}

export function benchmarkById(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow | undefined {
  return nucleicDataFor(snapshot).catalog.benchmarks.find((row) => text(row, 'benchmark_id') === id);
}

export function taskById(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow | undefined {
  return nucleicDataFor(snapshot).catalog.tasks.find((row) => text(row, 'task_id') === id);
}

export function trackById(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow | undefined {
  return nucleicDataFor(snapshot).catalog.tracks.find((row) => text(row, 'track_id') === id);
}

export function metricById(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow | undefined {
  return nucleicDataFor(snapshot).catalog.metrics.find((row) => text(row, 'metric_id') === id);
}

export function coverageForBenchmark(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow | undefined {
  return nucleicDataFor(snapshot).catalog.coverage.find((row) => text(row, 'benchmark_id') === id);
}

export function benchmarkTaskIds(id: string, snapshot: string = NUCLEIC_SNAPSHOT): string[] {
  return [...new Set(nucleicDataFor(snapshot).catalog.task_benchmark
    .filter((row) => text(row, 'benchmark_id') === id)
    .map((row) => text(row, 'task_id'))
    .filter((value) => value !== 'NR'))];
}

export function taskBenchmarkIds(id: string, snapshot: string = NUCLEIC_SNAPSHOT): string[] {
  return [...new Set(nucleicDataFor(snapshot).catalog.task_benchmark
    .filter((row) => text(row, 'task_id') === id)
    .map((row) => text(row, 'benchmark_id'))
    .filter((value) => value !== 'NR'))];
}

export function protocolsForBenchmark(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow[] {
  return nucleicDataFor(snapshot).catalog.protocols.filter((row) => text(row, 'benchmark_id') === id);
}

export function protocolsForTask(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow[] {
  return nucleicDataFor(snapshot).catalog.protocols.filter((row) => text(row, 'task_id') === id);
}

export function summariesForProtocol(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow[] {
  return nucleicDataFor(snapshot).catalog.summaries.filter((row) => text(row, 'protocol_id') === id);
}

export function leadersForProtocol(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow[] {
  return nucleicDataFor(snapshot).catalog.leaders.filter((row) => text(row, 'protocol_id') === id);
}

export function summariesForBenchmark(id: string, snapshot: string = NUCLEIC_SNAPSHOT): NucleicRow[] {
  return nucleicDataFor(snapshot).catalog.summaries.filter((row) => text(row, 'benchmark_id') === id);
}

export function safeExternalUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || value === '' || value === 'NR') return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.toString() : undefined;
  } catch {
    return undefined;
  }
}

export function formatScore(value: unknown): string {
  if (value === undefined || value === null || value === '' || value === 'NR') return 'NR';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return new Intl.NumberFormat('en-US', { maximumSignificantDigits: 8 }).format(numeric);
}

export function claimLabel(claim: string, zh = false): string {
  const labels: Record<string, [string, string]> = {
    official_baseline: ['Official baseline', '官方 baseline'],
    original_table_best: ['Original-table best', '原论文表内最佳'],
    official_board_or_challenge_leader: ['Official board / challenge leader', '官方榜单 / 挑战领先者'],
    strict_cross_work_sota: ['Strict cross-work SOTA', '严格跨工作 SOTA'],
    single_reported_result: ['Single reported result', '单一报告结果'],
    NR: ['Not reported', '未报告'],
  };
  return labels[claim]?.[zh ? 1 : 0] ?? claim.replaceAll('_', ' ');
}

export function statusLabel(status: string, zh = false): string {
  const labels: Record<string, [string, string]> = {
    numeric_results_available: ['Numeric results available', '有公开数值结果'],
    no_public_numeric_result: ['No public numeric result', '无公开数值结果'],
    no_standard_numeric_protocol: ['No standard numeric protocol', '无标准数值协议'],
    protocol_not_reconstructable: ['Protocol not reconstructable', '协议无法完整重建'],
    restricted: ['Restricted', '访问受限'],
  };
  return labels[status]?.[zh ? 1 : 0] ?? status.replaceAll('_', ' ');
}

export function languagePeer(pathname: string, zh: boolean): string {
  const path = pathname.startsWith('/') ? pathname : `/${pathname}`;
  if (zh) return path.replace(/^\/zh(?=\/)/, '') || '/';
  return `/zh${path}`.replace(/\/+/g, '/');
}

export function nucleicRoot(zh = false): string {
  return `${zh ? '/zh' : ''}/nucleic-acids`;
}

export function snapshotRoot(zh = false, snapshot: string = NUCLEIC_SNAPSHOT): string {
  return `${nucleicRoot(zh)}/snapshots/${snapshot}`;
}
