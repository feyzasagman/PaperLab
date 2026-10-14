# Contributing to PaperLab

PaperLab is a research reading tool that runs on your own machine. It is built by people who read papers for a
living, and the most useful contributions usually come from your own reading, not from the issue tracker.

Everything here assumes you have the stack running. If you get stuck at any step, open an issue — a confusing
setup is a bug.

## Ways to help, smallest first

**1. Add a question to the retrieval eval.** This is the single most useful ten minutes you can spend, and you
don't need to write any application code. `backend/evals/questions.yaml` holds questions and the passages a good
answer must retrieve; `backend/evals/answers.yaml` holds questions we score answers against. Add one from your own
field — a question your papers *should* be able to answer — and we find out where retrieval breaks outside the
fields already covered. Open a pull request with just that entry.

**2. Add a paper source.** PaperLab searches arXiv, Crossref, CORE, Unpaywall, Semantic Scholar and OpenAlex.
If your field lives somewhere else — PubMed, HAL, bioRxiv, DBLP, SSRN — that source is a self-contained adapter in
`backend/app/providers/`. Look at an existing one and follow its shape. Issues labelled `paper-source` describe
what each needs.

**3. Report a bug from your own PDFs.** Extraction, chunking and highlight anchoring all meet strange documents in
the wild: two-column layouts, scanned scans, non-Latin scripts, papers with no DOI. Tell us which paper (a DOI or
link is enough) and what went wrong. These reports are worth more than they look.

**4. Improve the docs.** If the Quick start didn't work on your machine, say so, and say what you ran.

**5. Pick an issue labelled `good first issue`.**

## Running PaperLab locally

You need [Docker](https://www.docker.com/) and [Ollama](https://ollama.com/).

```sh
ollama pull qwen3:8b                  # the default chat model
docker compose up -d --build          # db (:5433), redis, api (:8000), worker, ingest-worker, frontend (:5180)
open http://localhost:5180
```

The first upload is slow: the worker downloads the embedding model once and caches it.

## The desktop app

`desktop/` is an Electron app that runs the release stack (`desktop/docker-compose.yml`) behind a window. To work on it
against your dev stack, with Docker left alone:

```sh
cd desktop && npm ci
unset ELECTRON_RUN_AS_NODE                          # some editors' terminals export it; Electron then runs as plain node and no window opens
PAPERLAB_URL=http://localhost:5180 npm start      # the window, on the dev stack
npm test && npm run typecheck                      # unit tests and types
npm run e2e -- --project=stub                      # the window against a stub docker: no Docker needed
```

`PAPERLAB_DATA_DIR` points the app at another data folder (its settings, log and backups). `PAPERLAB_DOCKER` names the
docker to use, and `PAPERLAB_DOCKER_PATHS` replaces the places it looks for one; the tests use both. `npm run icons`
redraws `build/`'s icons from the favicon.

The `stub` project fixes `PATH` to `/usr/bin:/bin` for every test (`desktop/e2e/stub.spec.ts`), so no real docker answers
in place of the stub (`desktop/e2e/stub-docker.sh`) unless a test names it. That holds on macOS, where Docker Desktop's
CLI lives elsewhere; on Linux, a distribution that installs `docker` straight into `/usr/bin` puts it on that `PATH`
too, and the tests that expect no docker at all find yours instead. `PATH` is fixed in the spec file, not read from
your shell, so run the suite somewhere `docker` isn't on `/usr/bin` or `/bin` if that happens.

## Tests

Run the suites that cover what you touched. All of them together is fine too.

```sh
cd backend  && uv run pytest && uv run ruff check .      # needs the compose db running
cd frontend && npx tsc -b && npm test                    # types + unit tests
cd frontend && npm run gen:api                           # regenerate API types after changing a route (api must be up)
```

End-to-end tests run against the real stack with no mocked backend, using a fake model that always answers the
same way:

```sh
cd frontend && npx playwright install chromium           # once
scripts/e2e-stack.sh up                                  # its own stack (paperlab-e2e), fake providers, your library untouched
cd frontend && npm run typecheck:e2e && npm run e2e
scripts/e2e-stack.sh down
```

Two things that will waste your afternoon if nobody tells you:

- The E2E suite drives a real browser against `:5280`. Wait for that proxy to be serving before you start, and run
  it with nothing else heavy on the machine — a loaded laptop produces timeouts that look like real failures.
- Restarting the API kills a running eval. `docker compose exec api python -m evals.answers` takes a long time on a
  local model; leave the stack alone until it finishes.

Retrieval eval, for changes to chunking or retrieval:

```sh
docker compose exec api python -m evals.run              # recall@k over backend/evals/questions.yaml
```

Run it twice after a re-ingest before comparing numbers.

## UI changes

Follow `frontend/design-system/MASTER.md`: shadcn/ui components, Tailwind tokens, both light and dark themes, and
the stable test hooks listed there. A screenshot in both themes makes a UI pull request much easier to review.

## Opening a pull request

- One change per pull request. A small one that lands beats a big one that stalls.
- Say what you ran to check it. If you changed retrieval, include the eval numbers before and after.
- Commit messages: `type: description`, where type is one of `feat`, `fix`, `refactor`, `docs`, `test`, `chore`,
  `perf`, `ci`.
- New code needs a test that fails without it.

## What won't be merged

PaperLab has a few rules it doesn't bend, and a pull request that breaks one will be turned down however good the
code is:

- **Nothing that sends your papers or notes anywhere by default.** Local-first is the product. Any new network call
  is off until the user turns it on, and it has to be documented in the Principles section of the README, including
  exactly what leaves the machine.
- **Nothing that gets past a paywall.** PaperLab links to a paper you can't access; it does not fetch it for you.
- **Nothing that blurs what you wrote and what a model wrote.** AI text is stored separately, keeps the model and
  prompt version that produced it, and is always badged in the interface.
- **No accounts, no telemetry, no analytics.**

## Licence

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE), the same
licence as the project.
