# Documan

A library and student blog for a college: every subject has an address —
department, year, semester — and everything filed under it lives there.

This repository is the deployment. The two applications are submodules:

| | |
| --- | --- |
| [`documan-java21`](documan-java21) | Java 25 / Spring Boot 4 REST API — PostgreSQL, Redis, Meilisearch, Cloudflare R2 |
| [`documan-angular-21`](documan-angular-21) | Angular 21 single-page client |

Each has its own README covering its architecture and configuration. The HTTP
contract is [`documan-java21/API.md`](documan-java21/API.md) — every endpoint,
what it requires, and who may call it — generated from
[`openapi.json`](documan-java21/openapi.json), which is generated from the
controllers. This README covers running the two applications together.

## Running it

`docker compose up` on its own gets you five running containers and an
application that does nothing: the schema is created empty, so there are no
departments to browse and no `admin` role to promote yourself into. The whole
sequence is below, in order. Read [What you need first](#what-you-need-first)
before starting — five of the twelve required values come from third-party
accounts.

```bash
# 1. Clone with both submodules. Without them the app directories are empty
#    and the build has nothing to compile.
git clone --recurse-submodules https://github.com/theinhumaneme/documan-public-release.git
cd documan-public-release
#    Already cloned without them?
#    git submodule update --init --recursive

# 2. Configure. Twelve values are mandatory; see the table below.
cp .env.example .env

# 3. Start. Five containers: web, api, postgres, redis, meilisearch.
docker compose up -d --build

# 4. Load the reference data. `ddl-auto: update` creates the tables and nothing
#    else, so role, department, year and semester are all empty until this runs.
#    Nothing works before it: the front page is a department picker.
set -a && . ./.env && set +a
for f in role year semester department; do
  docker compose exec -T postgres \
    psql -U "$DB_USERNAME" -d "${DB_NAME:-documan}" \
    < "documan-java21/SQL/$f.sql"
done

# 5. Open it, and sign in through Clerk. Signing in is what creates your row —
#    there is no registration endpoint, the API provisions an account on your
#    first authenticated request. Step 6 has nothing to promote until you have.
open "http://localhost:${WEB_PORT:-8080}"

# 6. Make yourself an administrator. See Roles below for why this is by hand.
```

Do not use `documan-java21/SQL/load-data.sh` for this. It hardcodes
`localhost`, `root` and `password`, which are the credentials of the *other*
development stack, not the ones you just put in `.env`.

Steps 4 and 6 are not idempotent — the seed scripts insert unconditionally, so
running step 4 twice gives you duplicate departments.

### What you need first

- **A Clerk instance**, and a **JWT template** in it named `documan` emitting
  `email`, `given_name` and `family_name`. See [Identity](#identity) — the API
  cannot create an account for a first-time reader without the email claim.
- **A Cloudflare R2 bucket** and an access key pair for it. Uploads and downloads
  fail without these; nothing else does.
- Docker, and roughly 2 GB of RAM for the five containers (`web`, `api`, `postgres`, `redis`, `meilisearch`).

### The twelve values that must be set

`docker compose` fails at startup on any of these rather than starting something
that half works, so this is the complete blocking list. Everything else in
`.env.example` has a working default.

| | |
| --- | --- |
| `DB_USERNAME`, `DB_PASSWORD` | Postgres. Any values you like; the container is created with them. |
| `REDIS_PASSWORD` | Redis. Same. |
| `MEILI_MASTER_KEY` | Meilisearch. Same — treat it as a password, not an identifier. |
| `CLERK_PUBLISHABLE_KEY` | From the Clerk dashboard. `pk_test_…` or `pk_live_…`. |
| `CLERK_ISSUER_URI`, `CLERK_JWK_SET_URI` | Copy from your instance's `<frontend-api>/.well-known/openid-configuration` rather than assembling them. The frontend API host is base64-encoded inside the publishable key. |
| `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_FILES_BUCKET`, `R2_FILES_PUBLIC_URL` | Cloudflare R2. The public URL is the bucket's public or custom-domain address, with a trailing slash. |

**Do not set `CLERK_SECRET_KEY`.** Neither application uses it — see
[Identity](#identity).

## The three topologies

All three compose files live here, so how this is run and how it is deployed are
both reviewable in one place. Pick by what you are doing:

| File | Starts | Use it when |
| --- | --- | --- |
| `docker-compose.yml` | postgres, redis, meilisearch, api, web — **built from source** | You want the whole thing running locally. Start here. |
| `docker-compose.datastores.yml` | postgres, redis, meilisearch | You are working on an application and want to run it from an IDE or Maven, restarting it freely. |
| `docker-compose.remote.yml` | api, web — **prebuilt images**, loopback-bound, memory-limited | You are deploying to a server whose datastores are already up behind a TLS terminator. |

`docker-compose.datastores.yml` used to be `documan-java21/redis-database.yml`.
Its bind mounts are now under `./persist` here rather than inside the submodule,
so existing data does not move with it — `mv documan-java21/persist persist` if
you want to keep it.

### Everything, locally

```bash
docker compose up -d --build          # build and start all five
docker compose ps                     # health of each
docker compose logs -f api            # follow the API
docker compose exec -T postgres psql -U "$DB_USERNAME" -d "${DB_NAME:-documan}"
docker compose down                   # stop, keep the data
docker compose down -v                # stop and delete the data
```

### Datastores only, application from source

```bash
docker compose -f docker-compose.datastores.yml up -d
docker compose -f docker-compose.datastores.yml ps      # wait for three healthy

cd documan-java21 && make dev         # API from source, `dev` profile
cd documan-angular-21 && npm install && npm start        # client on :4200
```

`make up` and `make down` in `documan-java21` drive the same datastore file. The
API expects `src/main/resources/application-dev.yml`, which is gitignored — copy
`application-dev.yml.example` and fill it in.

### Deploying to a server

The server needs Docker, a `.env`, and this repository's compose files. It does
not need the source: images are pulled, not built, so a 2 vCPU box is not
compiling Angular.

```bash
# Once, on the server
git clone --recurse-submodules https://github.com/theinhumaneme/documan-public-release.git
cd documan-public-release
cp .env.example .env                  # plus API_TAG and WEB_TAG
docker compose -f docker-compose.datastores.yml up -d

# Every deploy
docker compose -f docker-compose.remote.yml pull
docker compose -f docker-compose.remote.yml up -d
docker compose -f docker-compose.remote.yml ps
```

Then point a TLS terminator at `127.0.0.1:8081`. Nothing in the remote topology
publishes beyond loopback, so that terminator is the entire attack surface.

### Building and publishing images

The images are built from the submodules and pushed to a registry. **Build for the
architecture of the server, not of your laptop** — an arm64 Mac produces images an
amd64 server cannot run, and the failure is a container that will not start:

```bash
docker buildx build --platform linux/amd64 \
  --build-arg ENV=production \
  -t "$IMAGE_REPO:$API_TAG" --push ./documan-java21

docker buildx build --platform linux/amd64 \
  --build-arg ENV=production \
  --build-arg CLERK_PUBLISHABLE_KEY="$CLERK_PUBLISHABLE_KEY" \
  --build-arg CLERK_JWT_TEMPLATE="${CLERK_JWT_TEMPLATE:-documan}" \
  -t "$IMAGE_REPO:$WEB_TAG" --push ./documan-angular-21
```

`ENV=production` is not optional on either. Without it the Dockerfile falls
through to a development build: unminified, and with unhashed asset filenames that
a CDN will then cache for three days, so a style change appears not to deploy.

The web image has the Clerk publishable key compiled in, which is why it takes it
as a build argument. **One web image per Clerk instance.**

### Useful checks

```bash
# Is the API up, and which Clerk instance is it validating against?
curl -s localhost:8080/actuator/health
docker compose exec -T api sh -c 'echo $CLERK_ISSUER_URI'

# Row counts, after a deploy or an import
docker compose exec -T postgres psql -U "$DB_USERNAME" -d "${DB_NAME:-documan}" \
  -c 'select (select count(*) from subject) subjects,
             (select count(*) from file) files,
             (select count(*) from documan_user) users'

# Is search in step with the database?
curl -s -H "Authorization: Bearer $MEILI_MASTER_KEY" \
  "localhost:7700/indexes/${MEILI_INDEX_PREFIX:-documan_prod_}files/stats"
```

### Regenerating the API contract

```bash
cd documan-java21 && ./extract-openapi-json.sh          # needs Docker
cd documan-java21 && ./extract-openapi-json.sh --check  # what CI runs
python3 scripts/generate_api_md.py > documan-java21/API.md
```

The browser only ever talks to `web`. It serves the built application and
proxies `/api` to `api` on the internal network, so there is one origin, no CORS,
and the API is not exposed outside the compose network at all — `api`,
`postgres`, `redis` and `meilisearch` publish no ports. Put a TLS terminator in
front of `web` and that is the whole attack surface.

Nothing in `docker-compose.yml` has a default for a credential: a missing
password stops the stack at startup rather than starting something that half
works. Non-secret settings do have defaults, because a deployment that does not
care should not have to state them.

## Identity

Clerk. The API is a resource server — it validates bearer tokens against Clerk's
published signing keys and mints nothing, so **Clerk's secret key is not used by
either application and must not be configured**. Only the publishable key and the
issuer and JWK set URIs are needed, and all three come from the Clerk instance
itself.

Two settings are one setting split across two services, and they have to move
together:

- `CLERK_PUBLISHABLE_KEY` is compiled into the client at build time, because a
  single-page application carries its configuration in the bundle the browser
  downloads. Changing it means rebuilding the `web` image.
- `CLERK_ISSUER_URI` and `CLERK_JWK_SET_URI` configure the API at runtime.

Point them at different Clerk instances and the browser will present tokens the
server refuses to validate. Take both from the instance's own discovery document
at `<frontend-api>/.well-known/openid-configuration` rather than assembling them
by hand — a development instance publishes on `<slug>.clerk.accounts.dev` and a
production one on your own domain.

The API also expects a Clerk **JWT template** named by `CLERK_JWT_TEMPLATE`
(default `documan`). Clerk's default session token carries no email, and without
one the API cannot provision a row for a new reader. Templates are per-instance
and do not migrate, so moving from a development to a production instance means
recreating it. Its default lifetime is 60 seconds, which is fine for a browser
and painful for anything scripted.

## Roles

`regular` < `maintainer` < `moderator` < `admin`, and every check asks "at least
this rank". A maintainer's right is scoped to granted departments, years and
semesters rather than to the library as a whole. The full table is in the API's
[Security model](documan-java21/README.md#security-model).

**The first administrator has to be made by hand.** Promotion requires an
existing administrator, so a fresh database cannot produce its first one through
the API.

**Sign in through the web interface first.** There is no registration endpoint —
`CurrentUser` provisions your row on your first authenticated request — so until
you have signed in once there is no row to promote.

Promote **by email**, not by lowest id. An earlier version of this recipe used
`WHERE id = (SELECT MIN(id) FROM documan_user)`, which promotes a sample row on
any database that was seeded with `user.sql`, and silently matches nothing on a
database nobody has signed into yet:

```bash
set -a && . ./.env && set +a
docker compose exec -T postgres psql -U "$DB_USERNAME" -d "${DB_NAME:-documan}" <<'SQL'
UPDATE documan_user
   SET role_id = (SELECT id FROM role WHERE name = 'admin'),
       can_post = true, can_comment = true, is_verified = true
 WHERE email = 'you@example.com';
SQL
```

`psql` reports `UPDATE 1`. **`UPDATE 0` means it did nothing** — either the email
does not match the one Clerk holds for you, or you have not signed in yet.

Then clear the cached user. A direct `UPDATE` does not pass through the
application, so the cache still holds your old role and `/user/me` will keep
reporting `regular`:

```bash
docker compose exec -T redis redis-cli --scan --pattern 'documan:users*' \
  | xargs -r -n1 docker compose exec -T redis redis-cli DEL
```

## What is not in here

`scripts/` holds one-off tooling rather than part of either application:

| | |
| --- | --- |
| `load_library.py` | Bulk-import a Google Drive export into the library, through the HTTP API. Resumable. |
| `export_library_metadata.sh` | Dump subjects, folders and files so another database can adopt them, without re-uploading the objects. |
| `loadtest-read.js`, `loadtest-rate.js` | k6 read-only load tests. |
| `generate_api_md.py` | Render `API.md` from `openapi.json`. |

## Secrets

`.env` is gitignored and so is `documan-java21/src/main/resources/application-dev.yml`;
the tracked `application-documan.yml` is a template of placeholders. Neither
submodule has ever committed a real credential — the copy of `application-dev.yml`
that briefly appeared in history used `dev-access-key` and `example.r2.cloudflarestorage.com`.

`*.key` and `*.pem` are ignored here, but a key kept in `~/.ssh` and referenced
with `ssh -i` cannot be committed by accident at all, which is better than being
ignored.

## Licensing

**MIT, both applications.** One licence across the deployment, so shipping the two
images together raises no question about how one licence reaches the other.

The client was GPL-3.0 until it was relicensed to match the API. That was the
owner's to do — `git log` on that submodule shows a single author, Kalyan Mudumby,
so there was no other copyright holder whose agreement was needed.

Nothing in the dependency tree forces copyleft. The one copyleft package that
appears at all is `rpc-websockets` (LGPL-3.0-only), reached transitively through
`@clerk/clerk-js` → `@solana/wallet-adapter-base` → `@solana/web3.js` as a *peer*
dependency of Clerk's Web3 wallet support. This application does not enable Web3
wallets, and a search of all 40 chunks of the built production bundle finds no
trace of it — so it is not distributed, and its terms never engage. Worth
re-checking if Clerk's dependencies change or Web3 sign-in is ever switched on.

None of the above is legal advice.

## Contributing

There is no contribution process yet: no `CONTRIBUTING.md`, no branch or review
convention, and no CI in this repository. `documan-java21` has `make format`
(Spotless with Google Java Format) and a test suite that needs Docker; run both
before proposing a change.
