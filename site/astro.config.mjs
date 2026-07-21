import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://wang422003.github.io',
  base: '/bio-benchmark-atlas',
  output: 'static',
  trailingSlash: 'always',
  build: { format: 'directory' },
});
