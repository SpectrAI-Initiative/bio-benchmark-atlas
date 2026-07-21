import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://spectrai-initiative.github.io',
  base: '/bio-benchmark-atlas',
  output: 'static',
  trailingSlash: 'always',
  build: { format: 'directory' },
});
