import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

test('home renders charts and core navigation', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/');
  await expect(page.getByRole('heading', { name: /Map the benchmark/ })).toBeVisible();
  await expect(page.locator('.plot-host svg').first()).toBeVisible();
  await expect(page.getByRole('link', { name: 'Explorer' })).toBeVisible();
});

test('explorer restores and updates URL filter state', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/?domain=protein-design&access=fully-open');
  await expect(page.locator('#domain')).toHaveValue('protein-design');
  await expect(page.locator('#access')).toHaveValue('fully-open');
  await expect(page.getByRole('link', { name: 'ProteinGym', exact: true })).toBeVisible();
  await page.locator('#q').fill('FLIP');
  await expect(page).toHaveURL(/q=FLIP/);
  await expect(page.getByRole('link', { name: 'FLIP', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'ProteinGym', exact: true })).toBeHidden();
});

test('LifeSciBench shows audited protein and binding counts', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/lifescibench/');
  await expect(page.getByText(/136 tasks use Protein and Structural Biology/)).toBeVisible();
  await expect(page.getByText(/62 are in Design, Optimization & Prediction/)).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Not reported' }).first()).toBeVisible();
  await expect(page.getByText('initial-release', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/agentic-eval · audited/)).toBeVisible();
  const run = page.locator('#lifescibench-official-full');
  await expect(run.locator('.tag.accent')).toHaveText('lifescibench-initial-release-full-official');
  await expect(run.getByText('Scope', { exact: true }).locator('..')).toContainText('full · n=750');
  await expect(run.getByText('Repeats', { exact: true }).locator('..')).toContainText('Not reported');
});

test('ProteinGym separates formal tracks, versions, conflicts, and comparable results', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/proteingym/');
  await expect(page.getByText(/v1\.3 release archive contains 66 DMS indel assay records/)).toBeVisible();
  await expect(page.getByText('Conflicted', { exact: true }).first()).toBeVisible();
  for (const name of [
    'ProteinGym DMS Substitutions',
    'ProteinGym DMS Indels',
    'ProteinGym Clinical Substitutions',
    'ProteinGym Clinical Indels',
  ]) {
    await expect(page.getByRole('link', { name, exact: true }).first()).toBeVisible();
  }

  await page.goto('/bio-benchmark-atlas/benchmarks/proteingym-dms-substitutions/');
  const run = page.locator('#proteingym-v10-dms-substitutions-zero-shot');
  await expect(run.getByText('Scope', { exact: true }).locator('..')).toContainText('full · n=217');
  await expect(page.locator('.chart-card details tbody tr')).toHaveCount(50);
  await expect(page.locator('.plot-host svg[viewBox]')).toBeVisible();
  await expect(page.locator('.plot-host svg[viewBox]').getByText('TranceptEVE L', { exact: true })).toBeVisible();
});

test('CASP separates rolling and completed rounds with track-specific protocols', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/casp/');
  await expect(page.getByText(/CASP17 is an active, rolling 2026 round/)).toBeVisible();
  await expect(page.getByText(/CASP16 is the latest completed round/)).toBeVisible();
  for (const name of [
    'CASP Protein Monomers',
    'CASP Protein Multimers',
    'CASP Protein-Ligand Prediction',
  ]) {
    await expect(page.getByRole('link', { name, exact: true }).first()).toBeVisible();
  }
  await expect(page.locator('#casp16-monomer-regular-official')).toContainText('full · n=54');
  await expect(page.locator('#casp16-multimer-phase1-regular')).toContainText('full · n=40');
  await expect(page.locator('#casp16-ligand-pose-regular')).toContainText('subset · n=229');
  await expect(page.locator('#casp16-ligand-affinity-stage1')).toContainText('subset · n=122');
  await expect(page.locator('#casp16-ligand-affinity-stage2')).toContainText('subset · n=103');

  await page.goto('/bio-benchmark-atlas/benchmarks/casp-immune-complexes/');
  await expect(page.getByText('Not reported', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/No normalized evaluation run is published yet/)).toBeVisible();
});

test('CAMEO shows rolling scope, bounded study counts, and exact evaluated systems', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/cameo/');
  await expect(page.getByText(/current platform contains one category/)).toBeVisible();
  await expect(page.getByText(/bounded 2024 creator-paper snapshot contains 7,150 selected targets/)).toBeVisible();
  await expect(page.getByText('current-complex-3d', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('cell').filter({ hasText: /2024-complex-study/ }).first()).toBeVisible();
  await expect(page.locator('#cameo-2024-ligand-baseline-common')).toContainText('subset · n=2584');
  await expect(page.locator('#cameo-2024-ligand-baseline-common')).toContainText('AlphaFold 3 v3.0.1');
  await expect(page.locator('#cameo-2024-ppi-three-server-common')).toContainText('subset · n=392');
  await expect(page.locator('#cameo-2024-antibody-three-server-common')).toContainText('subset · n=83');

  await page.goto('/bio-benchmark-atlas/models/cameo-swissmodel-glide/');
  await expect(page.getByRole('heading', { name: 'SWISS-MODEL + Schrödinger Glide' })).toBeVisible();
  await expect(page.getByText('cameo-2024-ligand-baseline-common', { exact: true })).toBeVisible();
  await expect(page.getByText(/no normalized numeric result is published/i)).toBeVisible();
});

test('FLIP separates task and sample units and isolates every split comparison', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/flip/');
  await expect(page.getByText(/original FLIP contains 15 dataset×split tasks/)).toBeVisible();
  await expect(page.getByText(/Thirteen splits are active for performance comparison/)).toBeVisible();
  for (const name of ['FLIP AAV', 'FLIP GB1', 'FLIP Meltome Thermostability']) {
    await expect(page.getByRole('link', { name, exact: true }).first()).toBeVisible();
  }
  await expect(page.locator('#flip-aav-mut-des')).toContainText('subset · n=201426');
  await expect(page.locator('#flip-gb1-one-vs-rest')).toContainText('subset · n=8704');
  await expect(page.locator('#flip-meltome-human-cell')).toContainText('subset · n=1366');

  await page.goto('/bio-benchmark-atlas/benchmarks/flip-meltome/');
  await expect(page.getByText(/pinned official CSV contains 7,158 rows/)).toBeVisible();
  await expect(page.getByRole('cell', { name: '7158' }).first()).toBeVisible();
  await expect(page.locator('.plot-host svg[viewBox]').first()).toBeVisible();
});

test('ProteinLMBench shows released option counts and complete creator evaluation', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/proteinlmbench/');
  await expect(page.getByText(/only 871 have six choices/)).toBeVisible();
  await expect(page.getByText(/remaining 73 have 2–5, 7, 8, or 10 choices/)).toBeVisible();
  await expect(page.getByText('Conflicted · high', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Six-choice questions' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '871' }).first()).toBeVisible();
  const run = page.locator('#proteinlmbench-creator-full');
  await expect(run).toContainText('full · n=944');
  await expect(run).toContainText('20 generated tokens per question');
  await expect(run).toContainText('0.1');
  await expect(run).toContainText('InternLM2-Protein-7B');
  await expect(page.locator('.chart-card details tbody tr')).toHaveCount(36);
});

test('Biology-Instructions separates formal tasks, sample splits, and prompt groups', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/bioinstruction/');
  await expect(page.getByText(/21 formal tasks—6 DNA, 6 RNA, 5 protein, and 4 multi-molecule/)).toBeVisible();
  await expect(page.getByText(/task rows sum to 243,227 test examples/)).toBeVisible();
  await expect(page.getByText(/Stage-3 workbook has 8,002 rows/)).toBeVisible();
  await expect(page.getByText('63 normalized runs', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Biology-Instructions Antibody-Antigen Neutralization', exact: true })).toBeVisible();
  await expect(page.getByText(/Evaluation settings and results are published on the child-track pages/)).toBeVisible();
  await expect(page.locator('.run-card')).toHaveCount(0);

  await page.goto('/bio-benchmark-atlas/benchmarks/bioinstruction-aan/');
  await expect(page.locator('dt').filter({ hasText: /^Total$/ }).locator('..')).toContainText('26902');
  await expect(page.getByRole('cell').filter({ hasText: 'Test split' }).first()).toBeVisible();
  await expect(page.getByRole('cell', { name: '3301' }).first()).toBeVisible();
  await expect(page.locator('#bioinstruction-aan-open-baselines')).toContainText('subset · n=3301');
  await expect(page.locator('#bioinstruction-aan-closed-baselines')).toContainText('GPT-4o');
  await expect(page.locator('#bioinstruction-aan-creator-systems')).toContainText('ChatMultiOmics');
  await expect(page.locator('.chart-card')).toHaveCount(3);
  await expect(page.locator('.chart-card details tbody tr')).toHaveCount(16);
});

test('LAB-Bench separates category, task-file, split, and provider evaluation settings', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/lab-bench/');
  await expect(page.getByText(/contains 2,457 questions across 8 broad categories/)).toBeVisible();
  await expect(page.getByText(/1,967 public and 490 private questions across 31 task files/)).toBeVisible();
  await expect(page.getByText(/official README instead says 30 narrower subtasks/)).toBeVisible();
  await expect(page.getByText('Conflicted · high', { exact: true }).first()).toBeVisible();
  for (const name of [
    'LAB-Bench LitQA2', 'LAB-Bench SuppQA', 'LAB-Bench FigQA', 'LAB-Bench TableQA',
    'LAB-Bench DbQA', 'LAB-Bench ProtocolQA', 'LAB-Bench SeqQA', 'LAB-Bench CloningScenarios',
  ]) {
    await expect(page.getByRole('link', { name, exact: true }).first()).toBeVisible();
  }

  await page.goto('/bio-benchmark-atlas/benchmarks/lab-bench-dbqa/');
  await expect(page.locator('dt').filter({ hasText: /^Total$/ }).locator('..')).toContainText('650');
  await expect(page.getByRole('link', { name: 'LAB-Bench DbQA — Viral protein–protein interactions', exact: true })).toBeVisible();

  await page.goto('/bio-benchmark-atlas/benchmarks/lab-bench-seqqa/');
  await expect(page.locator('dt').filter({ hasText: /^Total$/ }).locator('..')).toContainText('750');
  await expect(page.getByText('15 normalized runs', { exact: true })).toBeVisible();

  await page.goto('/bio-benchmark-atlas/benchmarks/lab-bench-dbqa-viral-ppi/');
  await expect(page.locator('dt').filter({ hasText: /^Total$/ }).locator('..')).toContainText('50');
  await expect(page.locator('#lab-bench-dbqa-viral-ppi-creator-mcq')).toContainText('full · n=50');
  await expect(page.locator('#lab-bench-dbqa-viral-ppi-creator-mcq')).toContainText('GPT-4o');

  await page.goto('/bio-benchmark-atlas/benchmarks/lab-bench-protocolqa/');
  const protocolRun = page.locator('#lab-bench-protocolqa-anthropic-sonnet45-system-card');
  await expect(protocolRun.getByText('Scope', { exact: true }).locator('..')).toContainText('track');
  await expect(protocolRun.getByText('Shots', { exact: true }).locator('..')).toContainText('10');
  await expect(protocolRun.getByText('Repeats', { exact: true }).locator('..')).toContainText('Not reported');

  await page.goto('/bio-benchmark-atlas/benchmarks/lab-bench-figqa/');
  await expect(page.locator('#lab-bench-figqa-no-tools')).toContainText('adaptive thinking at max effort');
  await expect(page.locator('#lab-bench-figqa-crop-tool')).toContainText('crop tool');
});

test('GeneBench-Pro separates release strata, effort settings, repeats, and all reported configurations', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/genebench-pro/');
  await expect(page.getByText(/partitioned into 10 public, 50 Artificial Analysis, and 69 internal-holdout/)).toBeVisible();
  await expect(page.getByText(/All 60 reported model configurations are normalized across 13 effort\/repeat groups/)).toBeVisible();
  await expect(page.getByText(/dataset card says CC BY 4\.0, but its root LICENSE says MIT/)).toBeVisible();
  await expect(page.getByText('Conflicted · high', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Public release subset' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Artificial Analysis reporting subset' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Internal holdout' })).toBeVisible();

  const xhigh = page.locator('#genebench-pro-official');
  await expect(xhigh.getByText('Scope', { exact: true }).locator('..')).toContainText('full · n=129');
  await expect(xhigh.getByText('Repeats', { exact: true }).locator('..')).toContainText('10');
  await expect(xhigh).toContainText('GPT-5.5');
  await expect(xhigh).toContainText('12');

  const max = page.locator('#genebench-pro-standard-max');
  await expect(max).toContainText('GPT-5.6 Sol');
  await expect(max).toContainText('28.7');

  const pro = page.locator('#genebench-pro-pro-mode');
  await expect(pro.getByText('Repeats', { exact: true }).locator('..')).toContainText('5');
  await expect(pro).toContainText('GPT-5.6 Sol Pro (Extended)');
  await expect(pro).toContainText('31.5');
  await expect(page.locator('.run-card')).toHaveCount(13);
  await expect(page.locator('.chart-card')).toHaveCount(13);
});

test('BioMysteryBench separates the human subsets and repeats', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/biomysterybench/');
  await expect(page.getByText('the current v11 release has 90 problems')).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Human-solvable (v11)' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '73', exact: true })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Human-hard (v11)' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '17', exact: true })).toBeVisible();
  await expect(page.locator('#biomysterybench-official-run')).toContainText('full · n=99');
  await expect(page.locator('#biomysterybench-v8-human-solvable')).toContainText('subset · n=76');
  await expect(page.locator('#biomysterybench-v8-human-solvable')).toContainText('82.6');
  await expect(page.locator('#biomysterybench-v8-human-difficult')).toContainText('subset · n=23');
  await expect(page.locator('#biomysterybench-v8-human-difficult')).toContainText('29.6');
  await expect(page.locator('.run-card')).toHaveCount(3);
  await expect(page.locator('.chart-card')).toHaveCount(2);
});

test('CompBioBench separates access, agents, repeats, and hardest subset settings', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/compbiobench/');
  await expect(page.getByText(/v1 contains 100 tasks/)).toBeVisible();
  await expect(page.getByText(/only one Structure task uses a protein PDB/)).toBeVisible();
  await expect(page.getByText(/answer key and grader backend are private/)).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Domain — Structure' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Contributor difficulty — Levels 4–5' })).toBeVisible();

  const codex = page.locator('#compbiobench-creator-full');
  await expect(codex).toContainText('full · n=100');
  await expect(codex.getByText('Repeats', { exact: true }).locator('..')).toContainText('3');
  await expect(codex.getByText('Container', { exact: true }).locator('..')).toContainText('No');
  await expect(codex).toContainText('83.3');
  await expect(codex).toContainText('679');

  const hardest = page.locator('#compbiobench-opus-hardest');
  await expect(hardest).toContainText('subset · n=17');
  await expect(hardest).toContainText('69');

  const baseline = page.locator('#compbiobench-nonagentic-baselines');
  await expect(baseline).toContainText('single-turn API call');
  await expect(baseline).toContainText('5.3');
  await expect(baseline).toContainText('3.7');
  await expect(page.locator('.run-card')).toHaveCount(9);
  await expect(page.locator('.chart-card')).toHaveCount(17);
});

test('work, domain, archive, and Chinese guide routes render', async ({ page }) => {
  for (const [path, heading] of [
    ['/bio-benchmark-atlas/works/lifescibench-preprint/', 'LifeSciBench'],
    ['/bio-benchmark-atlas/domains/protein-design/', 'Protein design'],
    ['/bio-benchmark-atlas/archive/', 'The registry remembers what changed.'],
    ['/bio-benchmark-atlas/zh/', '先弄清怎么测，再比较分数。'],
  ]) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: heading, exact: false }).first()).toBeVisible();
  }
});

test('alias redirects to permanent benchmark id', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/protein-gym/');
  await expect(page).toHaveURL(/\/benchmarks\/proteingym\/$/);
});

test('dark mode and mobile navigation remain usable', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/');
  await page.getByRole('button', { name: 'Toggle color theme' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.getByRole('link', { name: 'Explorer' })).toBeVisible();
  await page.goto('/bio-benchmark-atlas/benchmarks/biomysterybench/');
  const hasPageOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(hasPageOverflow).toBe(false);

  await page.goto('/bio-benchmark-atlas/benchmarks/proteingym-dms-substitutions/');
  const proteinGymOverflow = await page.evaluate(() => ({
    page: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    resourceLabelsWrap: [...document.querySelectorAll('.sidebar .small')]
      .filter((node) => node.textContent?.includes('sha256'))
      .every((node) => getComputedStyle(node).overflowWrap === 'anywhere'),
  }));
  expect(proteinGymOverflow.page).toBe(false);
  expect(proteinGymOverflow.resourceLabelsWrap).toBe(true);
});

test('critical pages have no serious or critical axe violations', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === 'mobile', 'One desktop accessibility sweep is sufficient.');
  for (const path of [
    '/bio-benchmark-atlas/',
    '/bio-benchmark-atlas/benchmarks/',
    '/bio-benchmark-atlas/benchmarks/lifescibench/',
    '/bio-benchmark-atlas/methodology/',
  ]) {
    await page.goto(path);
    const results = await new AxeBuilder({ page }).analyze();
    const severe = results.violations.filter((item) => item.impact === 'serious' || item.impact === 'critical');
    expect(severe, `${path}: ${JSON.stringify(severe, null, 2)}`).toEqual([]);
  }
});
