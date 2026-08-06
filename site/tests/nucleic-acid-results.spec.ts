import AxeBuilder from '@axe-core/playwright';
import { readFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';


const base = '/bio-benchmark-atlas';
const snapshot = `${base}/nucleic-acids/snapshots/2026-08-06`;
const oldSnapshot = `${base}/nucleic-acids/snapshots/2026-08-05`;
const zhSnapshot = `${base}/zh/nucleic-acids/snapshots/2026-08-06`;
const gueProtocol = 'PROT-B01-28-DATASET-AGGREGATE-OFFICIAL-TRAIN-0DD5CD98-5E300CAE';
const largeProtocol = 'PROT-B27-OLIGOGYM-F88A0FA5C042F1EF';


test('English and Chinese nucleic-acid route families render permanent snapshot pages', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'chromium', 'The route inventory only needs one browser profile.');

  const routes: Array<[string, string]> = [
    [`${base}/nucleic-acids/`, 'Read the protocol. Then the score.'],
    [`${snapshot}/benchmarks/`, '47 benchmarks, with the protocol attached.'],
    [`${snapshot}/benchmarks/B01/`, 'Genome Understanding Evaluation (GUE)'],
    [`${oldSnapshot}/benchmarks/B01/`, 'Genome Understanding Evaluation (GUE)'],
    [`${snapshot}/tasks/`, '58 nucleic-acid tasks, all connected to a benchmark.'],
    [`${snapshot}/tasks/T01/`, 'Genomic element classification'],
    [`${snapshot}/protocols/${gueProtocol}/`, '28-dataset aggregate'],
    [`${snapshot}/participants/PART-DNABERT-3-MER/`, 'DNABERT (3-mer)'],
    [`${base}/nucleic-acids/methodology/`, 'When do two scores belong in one comparison?'],
    [`${base}/nucleic-acids/downloads/`, 'Download the snapshot or load one protocol at a time.'],
    [`${base}/zh/nucleic-acids/`, '看清口径，再看分数。'],
    [`${zhSnapshot}/benchmarks/`, '47 个 benchmark，每一个都保留口径。'],
    [`${zhSnapshot}/benchmarks/B01/`, 'Genome Understanding Evaluation (GUE)'],
    [`${zhSnapshot}/tasks/`, '58 个核酸任务，全部已连接 benchmark。'],
    [`${zhSnapshot}/tasks/T01/`, '基因组元件分类'],
    [`${zhSnapshot}/protocols/${gueProtocol}/`, '28-dataset aggregate'],
    [`${zhSnapshot}/participants/PART-DNABERT-3-MER/`, 'DNABERT (3-mer)'],
    [`${base}/zh/nucleic-acids/methodology/`, '什么时候，两个分数才能放在一起？'],
    [`${base}/zh/nucleic-acids/downloads/`, '下载快照，或按协议加载数据。'],
  ];

  for (const [path, heading] of routes) {
    const response = await page.goto(path);
    expect(response?.status(), path).toBe(200);
    await expect(page.getByRole('heading', { name: heading, exact: true }).first()).toBeVisible();
  }
});


test('overview and detail pages preserve counts, gaps, and claim semantics', async ({ page }) => {
  await page.goto(`${base}/nucleic-acids/`);
  const overview = page.locator('[data-na-overview]');
  await expect(overview).toBeVisible();
  await expect(overview.getByText('47', { exact: true }).first()).toBeVisible();
  await expect(overview.getByText('58', { exact: true }).first()).toBeVisible();
  await expect(overview.getByText('345', { exact: true }).first()).toBeVisible();
  await expect(overview.getByText('56,014', { exact: true }).first()).toBeVisible();
  await expect(overview).toContainText('0 strict cross-work SOTA claims');
  await expect(overview).toContainText('22 benchmarks with public numeric results');
  await expect(overview).toContainText('The other 25 benchmarks keep auditable gap pages');

  await page.goto(`${snapshot}/benchmarks/B01/`);
  await expect(page.getByRole('heading', { name: /10 result rows across 1 protocols/ })).toBeVisible();
  await expect(page.locator('[data-claim-type="official_baseline"]').first()).not.toContainText('No eligible claim');
  await expect(page.locator('[data-claim-type="original_table_best"]').first()).not.toContainText('No eligible claim');
  await expect(page.locator('[data-claim-type="strict_cross_work_sota"]').first()).toContainText('No eligible claim');
  await expect(page.locator('[data-claim-type="strict_cross_work_sota"]').first()).toContainText(
    'An original-table best is never substituted for cross-work SOTA.',
  );

  await page.goto(`${snapshot}/tasks/T01/`);
  await expect(page.getByRole('heading', { name: 'Genomic element classification', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Genome Understanding Evaluation (GUE)', exact: true }).first()).toBeVisible();
  await expect(page.locator('[data-protocol-id]').first()).toBeVisible();

  await page.goto(`${snapshot}/benchmarks/B07/`);
  await expect(page.getByRole('heading', { name: /result rows across/ })).toBeVisible();
  await expect(page.locator('table')).toContainText('closed_with_numeric_evidence');

  await page.goto(`${snapshot}/participants/PART-DNABERT-3-MER/`);
  await expect(page.locator('[data-participant-id="PART-DNABERT-3-MER"]')).toBeVisible();
  await expect(page.locator('main')).toContainText('No cross-task average or global rank is computed.');
  const filteredProtocolLink = page.getByRole('link', { name: gueProtocol, exact: true }).first();
  await expect(filteredProtocolLink).toHaveAttribute('href', /metric=.*fingerprint=.*participant=PART-DNABERT-3-MER/);

  await page.goto(`${base}/nucleic-acids/methodology/`);
  await expect(page.locator('main')).toContainText('Original-table best');
  await expect(page.locator('main')).toContainText('Cannot claim literature-wide SOTA.');
  await expect(page.locator('main')).toContainText('No cross-benchmark aggregate score or global model leaderboard is computed.');

  await page.goto(`${base}/nucleic-acids/downloads/`);
  await expect(page.getByRole('link', { name: /latest\.json/ })).toHaveAttribute(
    'href', `${base}/data/nucleic-acids/latest.json`,
  );
  await expect(page.getByRole('link', { name: /manifest\.json/ })).toHaveAttribute(
    'href', `${base}/data/nucleic-acids/2026-08-06/manifest.json`,
  );
  await expect(page.getByRole('link', { name: /nucleic-acid-results-2026-08-06/ })).toHaveAttribute(
    'href', 'https://github.com/SpectrAI-Initiative/bio-benchmark-atlas/releases/tag/nucleic-acid-results-2026-08-06',
  );
});


test('benchmark and task explorers restore filters into the URL', async ({ page }) => {
  await page.goto(`${snapshot}/benchmarks/?q=Genome+Understanding&status=numeric_results_available&tier=B&task=T01`);
  const benchmarkExplorer = page.locator('[data-na-benchmark-explorer]');
  await expect(benchmarkExplorer).toBeVisible();
  await expect(page.locator('#na-benchmark-q')).toHaveValue('Genome Understanding');
  await expect(page.locator('#na-benchmark-status')).toHaveValue('numeric_results_available');
  await expect(page.locator('#na-benchmark-tier')).toHaveValue('B');
  await expect(page.locator('#na-benchmark-task')).toHaveValue('T01');
  await expect(page.locator('[data-benchmark-id="B01"]')).toBeVisible();
  await expect(benchmarkExplorer.locator('[data-na-visible-count]')).toHaveText('1');
  await page.locator('#na-benchmark-q').fill('no such benchmark');
  await expect(benchmarkExplorer.locator('[data-na-empty]')).toBeVisible();
  await expect(page).toHaveURL(/q=no\+such\+benchmark/);

  await page.goto(`${snapshot}/tasks/?q=Genomic+element&domain=D1&molecule=DNA&formulation=classification`);
  const taskExplorer = page.locator('[data-na-task-explorer]');
  await expect(taskExplorer).toBeVisible();
  await expect(page.locator('#na-task-q')).toHaveValue('Genomic element');
  await expect(page.locator('#na-task-domain')).toHaveValue('D1');
  await expect(page.locator('#na-task-molecule')).toHaveValue('DNA');
  await expect(page.locator('#na-task-formulation')).toHaveValue('classification');
  await expect(page.locator('[data-task-id="T01"]')).toBeVisible();
  await expect(taskExplorer.locator('[data-na-visible-count]')).toHaveText('1');
  await page.locator('#na-task-molecule').selectOption('RNA');
  await expect(taskExplorer.locator('[data-na-empty]')).toBeVisible();
  await expect(page).toHaveURL(/molecule=RNA/);
});


test('protocol explorer gates comparison by fingerprint, paginates, restores filters, and exports CSV', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'chromium', 'The full 2,030-row protocol interaction runs once.');
  const dataResponses: string[] = [];
  page.on('response', (response) => {
    if (response.url().includes('/data/nucleic-acids/')) dataResponses.push(response.url());
  });

  const protocolPath = `${snapshot}/protocols/${largeProtocol}/`;
  await page.goto(protocolPath);
  const explorer = page.locator('[data-na-protocol-explorer]');
  await expect(explorer).toBeVisible();
  await expect(page.locator('#fingerprint')).toBeDisabled();
  await expect(page.locator('[data-na-filter-fieldset]')).toHaveAttribute('disabled', '');
  await expect(page.locator('[data-na-download-csv]')).toBeDisabled();
  await expect(page.locator('[data-na-results-status]')).toHaveText('Choose a metric and fingerprint to load results.');
  expect(dataResponses, 'No result asset should load before the metric gate is fixed.').toEqual([]);

  await page.locator('#metric').selectOption('M-OLIGOGYM-R2');
  await expect(page.locator('#fingerprint')).toBeEnabled();
  await expect(page.locator('[data-na-results-status]')).toHaveText(
    'Loaded 2030 rows; 406 match the current filters.',
    { timeout: 30_000 },
  );
  await expect(page.locator('[data-na-filter-fieldset]')).not.toHaveAttribute('disabled', '');
  await expect(page.locator('[data-na-result-table] tbody tr')).toHaveCount(100);
  await expect(page.locator('[data-na-pagination]')).toBeVisible();
  await expect(page.locator('[data-na-page-label]')).toHaveText('Page 1 of 5 · 406 results');
  await expect(page.locator('[data-na-result-chart] svg[viewBox]').last()).toBeVisible();
  await expect(page.locator('[data-na-download-csv]')).toBeEnabled();

  const selectedFingerprint = await page.locator('#fingerprint').inputValue();
  const selectedUrl = new URL(page.url());
  expect(selectedUrl.searchParams.get('metric')).toBe('M-OLIGOGYM-R2');
  expect(selectedUrl.searchParams.get('fingerprint')).toBe(selectedFingerprint);

  const scores = await page.locator('[data-na-result-table] tbody tr td:nth-child(3) .small').evaluateAll(
    (nodes) => nodes.slice(0, 20).map((node) => Number.parseFloat(node.textContent ?? 'NaN')),
  );
  expect(scores.every(Number.isFinite)).toBe(true);
  expect(scores).toEqual([...scores].sort((left, right) => right - left));

  await page.locator('#baseline-role').selectOption('classical_method');
  await expect(page.locator('[data-na-results-status]')).toHaveText('Loaded 2030 rows; 184 match the current filters.');
  await expect(page).toHaveURL(/baseline-role=classical_method/);

  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export filtered CSV' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(`${largeProtocol}-M-OLIGOGYM-R2.csv`);
  const downloadPath = await download.path();
  expect(downloadPath).not.toBeNull();
  const csv = await readFile(downloadPath!, 'utf-8');
  expect(csv.startsWith('\uFEFF"result_id","participant_id","configuration_id"')).toBe(true);
  expect(csv).toContain(`"${selectedFingerprint}"`);
  expect(csv.trimEnd().split('\r\n')).toHaveLength(185);

  const checkboxes = page.locator('[data-na-result-table] tbody input[type="checkbox"]');
  await checkboxes.nth(0).check();
  await checkboxes.nth(1).check();
  await expect(page.locator('[data-na-compare-count]')).toHaveText('2');
  await page.getByRole('button', { name: 'Compare same-fingerprint results' }).click();
  await expect(page.locator('[data-na-compare-panel]')).toBeVisible();
  await expect(page.locator('[data-na-compare-body] tr')).toHaveCount(2);
  await expect(page.locator('[data-na-compare-bar]')).toContainText('cross-fingerprint comparison is disabled');

  const restoredUrl = page.url();
  await page.goto(restoredUrl);
  await expect(page.locator('#metric')).toHaveValue('M-OLIGOGYM-R2');
  await expect(page.locator('#fingerprint')).toHaveValue(selectedFingerprint);
  await expect(page.locator('#baseline-role')).toHaveValue('classical_method');
  await expect(page.locator('[data-na-results-status]')).toHaveText(
    'Loaded 2030 rows; 184 match the current filters.',
    { timeout: 30_000 },
  );
});


test('public shard budgets keep large evidence and unrelated protocols off the initial interaction path', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'chromium', 'Network budgets only need one browser profile.');
  const manifestUrl = `${base}/data/nucleic-acids/2026-08-06/manifest.json`;
  const manifestResponse = await page.request.get(manifestUrl);
  expect(manifestResponse.ok()).toBe(true);
  const manifestText = await manifestResponse.text();
  const manifest = JSON.parse(manifestText) as {
    counts: { benchmarks: number; tasks: number; protocols: number; results: number };
    assets: Record<string, { compressedBytes: number; path: string }>;
    protocolChunks: Record<string, { compressedBytes: number; path: string }>;
  };
  expect(manifest.counts).toMatchObject({ benchmarks: 47, tasks: 58, protocols: 345, results: 56_014 });
  expect(new TextEncoder().encode(manifestText).byteLength).toBeLessThanOrEqual(150_000);
  expect(manifest.assets.catalog.compressedBytes).toBeLessThanOrEqual(200_000);
  expect(Math.max(...Object.values(manifest.protocolChunks).map((chunk) => chunk.compressedBytes)))
    .toBeLessThanOrEqual(550_000);

  const loadedAssets: string[] = [];
  page.on('response', (response) => {
    const url = response.url();
    if (url.includes('/data/nucleic-acids/2026-08-06/')) loadedAssets.push(url);
  });
  await page.goto(`${snapshot}/protocols/${largeProtocol}/`);
  await page.locator('#metric').selectOption('M-OLIGOGYM-R2');
  await expect(page.locator('[data-na-results-status]')).toContainText('Loaded 2030 rows', { timeout: 30_000 });

  expect(loadedAssets.some((url) => url.endsWith('/manifest.json'))).toBe(true);
  expect(loadedAssets.some((url) => url.includes('/protocols/'))).toBe(true);
  expect(loadedAssets.some((url) => url.includes(manifest.assets.entities.path))).toBe(true);
  expect(loadedAssets.some((url) => url.includes(manifest.assets.evidence.path))).toBe(false);
  expect(loadedAssets.some((url) => url.includes(manifest.assets.catalog.path))).toBe(false);
  expect(loadedAssets.some((url) => url.includes(manifest.assets.usage_index.path))).toBe(false);
  expect(loadedAssets.filter((url) => url.includes('/protocols/'))).toHaveLength(1);

  const eagerCompressedBytes = new TextEncoder().encode(manifestText).byteLength
    + manifest.assets.entities.compressedBytes
    + manifest.protocolChunks[largeProtocol].compressedBytes;
  expect(eagerCompressedBytes).toBeLessThanOrEqual(800_000);
  const pageOverflows = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(pageOverflows).toBe(false);
});


test('mobile protocol results keep wide data inside local scroll regions', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'This regression is specific to the narrow protocol layout.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${zhSnapshot}/protocols/${gueProtocol}/`);
  await expect(page.locator('[data-na-results-status]')).toContainText('已加载 10 条', { timeout: 30_000 });

  const tableWrap = page.locator('[data-na-table-wrap]');
  const chart = page.locator('[data-na-result-chart]');
  await expect(tableWrap).toBeVisible();
  await expect(chart.locator('svg[viewBox]').last()).toBeVisible();

  const localScroll = await page.evaluate(() => {
    const table = document.querySelector<HTMLElement>('[data-na-table-wrap]');
    const plot = document.querySelector<HTMLElement>('[data-na-result-chart]');
    if (!table || !plot) throw new Error('Expected protocol table and plot scroll regions.');
    return {
      viewportWidth: document.documentElement.clientWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      tableClientWidth: table.clientWidth,
      tableScrollWidth: table.scrollWidth,
      tableRight: table.getBoundingClientRect().right,
      plotClientWidth: plot.clientWidth,
      plotScrollWidth: plot.scrollWidth,
      plotRight: plot.getBoundingClientRect().right,
    };
  });

  expect(localScroll.documentScrollWidth).toBeLessThanOrEqual(localScroll.viewportWidth);
  expect(localScroll.bodyScrollWidth).toBeLessThanOrEqual(localScroll.viewportWidth);
  expect(localScroll.tableRight).toBeLessThanOrEqual(localScroll.viewportWidth);
  expect(localScroll.plotRight).toBeLessThanOrEqual(localScroll.viewportWidth);
  expect(localScroll.tableScrollWidth).toBeGreaterThan(localScroll.tableClientWidth);
  expect(localScroll.plotScrollWidth).toBeGreaterThan(localScroll.plotClientWidth);
});


test('nucleic-acid routes have no serious accessibility violations', async ({ page }, testInfo) => {
  const paths = testInfo.project.name === 'mobile'
    ? [
        `${base}/nucleic-acids/`,
        `${snapshot}/benchmarks/`,
        `${snapshot}/tasks/`,
        `${base}/zh/nucleic-acids/`,
      ]
    : [
        `${base}/nucleic-acids/`,
        `${snapshot}/benchmarks/`,
        `${snapshot}/benchmarks/B01/`,
        `${snapshot}/tasks/T01/`,
        `${snapshot}/participants/PART-DNABERT-3-MER/`,
        `${base}/nucleic-acids/methodology/`,
        `${base}/nucleic-acids/downloads/`,
      ];

  for (const path of paths) {
    await page.goto(path);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(overflow, path).toBe(false);
    const results = await new AxeBuilder({ page }).analyze();
    const severe = results.violations.filter(
      (item) => item.impact === 'serious' || item.impact === 'critical',
    );
    expect(severe, `${path}: ${JSON.stringify(severe, null, 2)}`).toEqual([]);
  }
});
