# Animations

Motion Canvas projects that explain the job system.
The rendered GIFs in `media/` are embedded in the top-level README and docs.

| Project | Source | Output |
|---------|--------|--------|
| `disyard-job` | `src/scenes/disyard-job.tsx` | `media/job-walkthrough.gif`, embedded in the README |
| `disyard-batch` | `src/scenes/disyard-batch.tsx` | not rendered, a sketch for a later README section |

Solid boxes are built, and a dashed box is a part that is not built yet.
`disyard-job` has no dashed box, so it has no legend.

`disyard-batch` shows 3 workflows, each run as a batch of 100,000 jobs with random seeds, followed by one aggregate-only job.
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
3. Encode the frames with ffmpeg, which the dev shell does not provide, so take it from nixpkgs (see below).

An agent without a screen clicks **Render** with Playwright, through `playwright-axi`.
Start the dev server in the background, then, from a directory outside the repository, because `playwright-axi` writes its snapshots to `.playwright-cli/` in the current directory:

```sh
npx -y playwright-axi@latest open http://localhost:9100/src/disyard-job --query render
```

The filtered snapshot shows the button and its ref, such as `button "Render" [ref=e105]`.
Click that ref, then wait until no new frame lands in `output/<project>/` for 10 seconds, which takes about 25 seconds and 768 frames for `disyard-job`:

```sh
npx -y playwright-axi@latest click e105
npx -y playwright-axi@latest close
```

The page needs no focus, so the commands run unattended.
Playwright needs no npm dependency in this project, and `playwright-axi install` fetches its Chromium if none is installed.
The `chrome-devtools-axi` browser tool is the alternative, with the same `open`, `snapshot` and `click` steps, when its bridge starts.
Its bridge can fail with `BRIDGE_NOT_READY`, as it did on every try when this was written, so Playwright is the dependable route.

Encode the frames:

```sh
nix shell nixpkgs#ffmpeg -c ffmpeg -y -framerate 15 -i output/disyard-job/%06d.png \
  -vf "split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
  -loop 0 media/job-walkthrough.gif
```

`job-walkthrough.gif` is about 230 KB, so keep each committed GIF that small.
Commit the GIF and the sources, never `output/`.
Change the animation text when the design changes, so the GIF never drifts from `docs/architecture/`.

## When to re-render

`docs/architecture/` is the source of truth, and the animation must match it.
Re-render a GIF when one of these changes what it shows:

- a component is built or removed,
- a dashed box becomes solid, or a solid box becomes dashed,
- a service, queue or label shown on screen is renamed,
- the flow of the job changes.

A rename of identifiers that never appear on screen does not need a re-render.
If a re-render gives a GIF identical to the committed one, commit nothing.

## Add an animation

1. Build the scene from the boxes and colours of `src/scenes/disyard-kit.tsx`, and pass `designed: true` to draw a part that is not built yet as a dashed box.
2. Add the scene as `src/scenes/<project>.tsx`.
3. Add its project file `src/<project>.ts`, copied from `src/disyard-job.ts`, and its meta `src/<project>.meta`, copied from `src/disyard-job.meta` so it renders at the same rate and scale.
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
