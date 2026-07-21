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
  await expect(page.getByRole('link', { name: 'FLIP' })).toBeVisible();
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

test('BioMysteryBench separates the human subsets and repeats', async ({ page }) => {
  await page.goto('/bio-benchmark-atlas/benchmarks/biomysterybench/');
  await expect(page.getByRole('cell', { name: 'Human-solvable' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '76' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Human-difficult' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '23' })).toBeVisible();
  await expect(page.getByText('5', { exact: true })).toBeVisible();
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
