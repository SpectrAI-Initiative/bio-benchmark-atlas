import catalogJson from '../generated/nucleic-acid-results/2026-08-05/catalog.json';
import entitiesJson from '../generated/nucleic-acid-results/2026-08-05/entities.json';
import usageIndexJson from '../generated/nucleic-acid-results/2026-08-05/usage-index.json';

export const NUCLEIC_SNAPSHOT = '2026-08-05';
export const NUCLEIC_SNAPSHOT_SOURCE_DATE = '2026-08-01';

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
};

export const nucleicCatalog = catalogJson as unknown as NucleicCatalog;
export const nucleicEntities = entitiesJson as unknown as {
  schema_version: string;
  snapshot_date: string;
  participants: NucleicRow[];
  configurations: NucleicRow[];
  works: NucleicRow[];
};
export const nucleicUsageIndex = usageIndexJson as unknown as {
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

export function benchmarkById(id: string): NucleicRow | undefined {
  return nucleicCatalog.benchmarks.find((row) => text(row, 'benchmark_id') === id);
}

export function taskById(id: string): NucleicRow | undefined {
  return nucleicCatalog.tasks.find((row) => text(row, 'task_id') === id);
}

export function trackById(id: string): NucleicRow | undefined {
  return nucleicCatalog.tracks.find((row) => text(row, 'track_id') === id);
}

export function metricById(id: string): NucleicRow | undefined {
  return nucleicCatalog.metrics.find((row) => text(row, 'metric_id') === id);
}

export function coverageForBenchmark(id: string): NucleicRow | undefined {
  return nucleicCatalog.coverage.find((row) => text(row, 'benchmark_id') === id);
}

export function benchmarkTaskIds(id: string): string[] {
  return [...new Set(nucleicCatalog.task_benchmark
    .filter((row) => text(row, 'benchmark_id') === id)
    .map((row) => text(row, 'task_id'))
    .filter((value) => value !== 'NR'))];
}

export function taskBenchmarkIds(id: string): string[] {
  return [...new Set(nucleicCatalog.task_benchmark
    .filter((row) => text(row, 'task_id') === id)
    .map((row) => text(row, 'benchmark_id'))
    .filter((value) => value !== 'NR'))];
}

export function protocolsForBenchmark(id: string): NucleicRow[] {
  return nucleicCatalog.protocols.filter((row) => text(row, 'benchmark_id') === id);
}

export function protocolsForTask(id: string): NucleicRow[] {
  return nucleicCatalog.protocols.filter((row) => text(row, 'task_id') === id);
}

export function summariesForProtocol(id: string): NucleicRow[] {
  return nucleicCatalog.summaries.filter((row) => text(row, 'protocol_id') === id);
}

export function leadersForProtocol(id: string): NucleicRow[] {
  return nucleicCatalog.leaders.filter((row) => text(row, 'protocol_id') === id);
}

export function summariesForBenchmark(id: string): NucleicRow[] {
  return nucleicCatalog.summaries.filter((row) => text(row, 'benchmark_id') === id);
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

export function snapshotRoot(zh = false, snapshot = NUCLEIC_SNAPSHOT): string {
  return `${nucleicRoot(zh)}/snapshots/${snapshot}`;
}
