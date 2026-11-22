# Development

- [Repository layout](#repository-layout)
- [Running from source](#running-from-source)
- [Running the tests](#running-the-tests)
- [How the image is built](#how-the-image-is-built)

For backend work, install Python 3.11 and
[uv](https://docs.astral.sh/uv/getting-started/installation/). Frontend work needs
Node.js 22.12 or later. Running CodeKB or its end-to-end tests also needs the AWS resources
and credentials in [Local setup](local.md#prerequisites).

---

## Repository layout

```
.
├── backend/              FastAPI service, parsers, Bedrock integration
│   ├── src/              the Python package (see "How the image is built")
│   ├── test/             unit tests (pytest)
│   ├── pyproject.toml
│   ├── constraints.txt   pinned dependency tree used by the image build
│   └── uv.lock           locked dependencies for local development
├── frontend/             React + Vite + Cloudscape UI
├── infra/
│   ├── cdk/              CDK app for the hosted deployment (see "Deploying to AWS")
│   └── cli/
│       └── setup_aws.sh  one-time AWS resource setup for the container path
├── scripts/
│   ├── run.sh            run locally from source, with reload
│   ├── run-container.sh  build the container image and run it locally
│   ├── build-image.sh    build the container image (codekb:local)
│   ├── deploy.sh         deploy to AWS with CDK (see "Deploying to AWS")
│   └── clean.sh          remove build outputs and dependency installs
├── tests/e2e/            end-to-end regression scripts
├── Dockerfile            production image; build context is the repository root
└── .env.example          configuration template, copied to .env
```

## Running from source

```bash
cp .env.example .env     # then fill it in, as in Local setup step 1
./scripts/run.sh
```

`run.sh` starts the backend on `localhost:8000` and the Vite dev server on
`localhost:5173`. Open the frontend URL: the dev server proxies `/api` and
`/mcp` to the backend, and both sides reload on save. `run.sh` syncs
`backend/.venv` against `backend/uv.lock` on Python 3.11, and reinstalls
`frontend/node_modules` whenever `frontend/package-lock.json` changes.

To check behaviour in the packaged container instead:

```bash
./scripts/run-container.sh         # or: ./scripts/run-container.sh finch
```

`run-container.sh` rebuilds the image with `./scripts/build-image.sh` on every
start (for this machine's architecture; `--arch` picks another), then
runs it in the foreground as `codekb-local` on `127.0.0.1:8000`.

Both scripts read `.env` at the repository root and nowhere else, and both bind
to the loopback interface only. They warn up front if `OTEL_LOGS_EXPORTER` or
`CODEKB_S3_BUCKET` is unset, since those two are the usual reason startup
refuses to continue. Pass `--help` to any script for its flags.

You still need the AWS resources from
[Local setup step 3](local.md#3-create-the-aws-resources)
and Bedrock model access before CodeKB can do real work.

## Running the tests

Backend unit tests and lint, from `backend/`:

```bash
cd backend
uv sync            # creates .venv, with the dev dependency group
uv run pytest
uv run ruff check .
```

The unit tests need no AWS credentials; S3 is mocked with moto.

Frontend type check and production build, from `frontend/`:

```bash
cd frontend
npm ci
npm run build
```

`tests/e2e/codekb_regression.py` runs an end-to-end regression against a
running CodeKB instance. It needs only the Python standard library; run it with
`--help` for its options.

To remove build outputs and dependency installs (`__pycache__`, `node_modules`,
`cdk.out` and so on), run `./scripts/clean.sh`. It keeps virtual environments
unless you pass `--venv`; `--dry-run` lists what it would remove.

## How the image is built

Two details are worth knowing before you change the `Dockerfile` or the backend
packaging.

**The import package is `src`.** The backend's code lives at `backend/src/`, and
that directory *is* the package. Every import inside it is relative, so the tree
has exactly one top-level name. `backend/pyproject.toml` matches:
`packages.find` uses `include = ["src*"]`, package data targets `src.core`, and
the console script points at `src.api.app:main`. The container runs
`uvicorn src.api.app:app`, and `run.sh` starts the same thing from source with
the working directory set to `backend/`. No renaming happens anywhere, so a
source run and a container run import identically. The distribution name is
separate: `aws-codekb-backend`, set by `[project] name`.

**The `Dockerfile` is multi-stage, and the build context is the repository
root**, not `backend/`. It has three stages:

1. A `node` stage builds the SPA from `frontend/`.
2. A builder stage installs the backend and its dependencies, pinned by
   `backend/constraints.txt`, into a virtual environment at `/opt/venv`. This
   stage has the compilers that the tree-sitter parsers need, and it is
   discarded.
3. The runtime stage copies in the virtual environment, the backend source, and
   the SPA. The SPA lands at `/app/src/static/dist`, where
   `backend/src/api/app.py` looks for it. The runtime has no compilers, and it
   runs as an unprivileged `codekb` user (uid 10001).

Because the SPA is a build stage, the image always serves the UI. It cannot be
built API-only, and there is no separate step to stage the frontend first.

The SPA's deploy-time values are passed as `--build-arg` and inlined into the
bundle by the `node` stage. They are public configuration, not secrets:

| Build argument | Value |
| --- | --- |
| `VITE_AUTH_ENABLED` | `true` to turn on sign-in. Default `false` |
| `VITE_COGNITO_DOMAIN` | Hosted-UI domain, e.g. `https://<prefix>.auth.<region>.amazoncognito.com` |
| `VITE_COGNITO_CLIENT_ID` | The web app client ID |
| `VITE_COGNITO_IDP_NAME` | Optional. Sends users straight to this federated provider |
| `VITE_BASE` | Path prefix, e.g. `/codekb/`. Default `/` |
| `VITE_CODEKB_VERSION` | Version shown on the Settings page |

The CDK deploy passes the Cognito values from the Auth stack outputs. The
repo-root `.dockerignore` keeps the build context small.

`ARG BASE_PLATFORM` defaults to `linux/amd64`, so a plain `docker build`
produces an amd64 image whatever your host is. The `node` build stage is not
platform-pinned, so the frontend compiles on the build host's native
architecture.
