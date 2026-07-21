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
  await expect(page.getByRole('link', { name: 'ProteinGym' })).toBeVisible();
  await page.locator('#q').fill('FLIP');
  await expect(page).toHaveURL(/q=FLIP/);
  await expect(page.getByRole('link', { name: 'FLIP' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'ProteinGym' })).toBeHidden();
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
