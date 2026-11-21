import motionCanvas from '@motion-canvas/vite-plugin';
import {defineConfig} from 'vite';

export default defineConfig({
  plugins: [motionCanvas({project: ['./src/weft-job.ts', './src/weft-batch.ts']})],
});
