# Animations

Motion Canvas projects that explain the job system.
The rendered GIFs in `media/` are embedded in the top-level README and docs.

| Project | Source | Output |
|---------|--------|--------|
| `forge-job` | `src/scenes/forge-job.tsx` | `media/job-walkthrough.gif`, embedded in the README |
| `forge-batch` | `src/scenes/forge-batch.tsx` | not rendered, a sketch for a later README section |

Solid boxes are built, and a dashed box is a part that is not built yet.
`forge-job` has no dashed box, so it has no legend.

`forge-batch` shows 3 workflows, each run as a batch of 100,000 jobs with random seeds, followed by one aggregate-only job.
Its results index is dashed, because the load CLI that builds an aggregate-only job's index from a batch is not built yet.
The scene draws one aggregate-only job over all three batches, while the load CLI design builds one per batch, so treat the scene as a sketch and do not render it until that is settled.

## Render

Run these from `docs/animations/`, inside `nix develop`, which provides Node and npm.

```sh
nix develop -c npm install
nix develop -c npm run dev
```

1. Open `http://localhost:9100/src/<project>` and check the preview.
2. Click **Render** in the Video Settings panel.
   The project meta renders at 15 fps and half scale, as PNG frames into `output/<project>/` (gitignored).
3. Encode the frames with ffmpeg, which the dev shell does not provide, so take it from nixpkgs:

```sh
nix shell nixpkgs#ffmpeg -c ffmpeg -y -framerate 15 -i output/forge-job/%06d.png \
  -vf "split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
  -loop 0 media/job-walkthrough.gif
```

`job-walkthrough.gif` is about 230 KB, so keep each committed GIF that small.
Commit the GIF and the sources, never `output/`.
Change the animation text when the design changes, so the GIF never drifts from `docs/architecture/`.

## Add an animation

1. Build the scene from the boxes and colours of `src/scenes/forge-kit.tsx`, and pass `designed: true` to draw a part that is not built yet as a dashed box.
2. Add the scene as `src/scenes/<project>.tsx`.
3. Add its project file `src/<project>.ts`, copied from `src/forge-job.ts`, and its meta `src/<project>.meta`, copied from `src/forge-job.meta` so it renders at the same rate and scale.
4. Add `./src/<project>.ts` to the `project` list in `vite.config.ts`, and a row to the table above.
5. Render it as above, into `media/<name>.gif`.

## Embed

Link the GIF with a relative path from the page that shows it, and give it a one-sentence caption.
From the top-level README:

```md
This animation follows one job from submission to completion.

![One job, end to end](docs/animations/media/job-walkthrough.gif)
```

From a page under `docs/`, the path starts at `animations/media/`.

## Animation or diagram

Use an animation when the point is an order of steps or a flow over time, such as a job moving through the system.
Use a draw.io diagram, as [docs/DIAGRAMS.md](../DIAGRAMS.md) describes, for structure that holds still, such as components and their links.
An animation is harder to change and to review, so prefer a diagram when a static picture says enough.
