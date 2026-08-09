# Pre-release changes

A record of one working session across both submodules: UI work on the Angular
app, an over-engineering audit and trim of the Java backend, and moving the
student blog off browser storage and onto the API.

Read the **Open items** section before picking this up again — three things are
unfinished, and one of them means the API will not start until Tailscale is
running.

---

## Where the work is

| Repo | Branch | Commits | Net |
| --- | --- | --- | --- |
| `documan-java21` | `release-preview` | 5 (`b82d849` … `ba81b23`) | 101 files, −3,042 |
| `documan-angular-22` | `trim/pre-release` | 2 (`a47b0a3`, `bf6576a`) | 10 files, −115 |

Both start with a baseline commit of the working tree as found. The Angular app
was almost entirely untracked — only `README.md` had ever been committed — so
`a47b0a3` puts 74 files under version control for the first time. `b82d849`
does the same for 52 files of in-progress backend work.

The outer folder is not a repo and holds ~13.6 GB of Drive downloads with no
`.gitignore`. Nothing was committed there, and nothing should be.

---

## 1. UI work on the subject and blog pages

### View and Download are now separate controls

Subject file rows had a single **Download** button pointed at the R2 object
URL. It never downloaded anything: a link's `download` attribute is ignored
cross-origin and the bucket is a different origin, so it opened the file inline
under a mangled object-key name. Library management already knew this and
routed through the API.

Each row now has a filled **View** (object URL, new tab) and an outlined
**Download** (`/api/v1/file/download`, which 302s to a signed URL carrying
`Content-Disposition: attachment` and the real filename). The shared helper
moved to `api.ts` as `downloadUrl(fileId)` so both screens use one definition.

**Still to do:** the search results (`finder.html`) and favourites
(`blog/favourites.ts`) lists have the same broken single button.

### Post-page moderator buttons

*Close comments* and *Delete* are solid rather than outlined, matching the
library's consequence-coloured row actions.

This needed a fix at the source: PrimeNG's `danger` resolved to Tailwind red
(`#ef4444`) while the app hand-writes `#a4262c` in six places. The preset's
`red` ramp is rebuilt around the app's red and `--alarm` added to `:root`, so
every `severity="danger"` control now matches. **This is a global visual
change** — it also affects the console and moderation screens.

### Account menu on narrow screens

Could not reproduce the reported overflow at 375px, where it fits with room to
spare. Fixed the mechanism that would cause it at any width: the popup carries
a `12.5rem` min-width and PrimeNG keeps it on screen by shifting it left *by its
own width*, which stops working once that width exceeds the screen — it then
hangs off both edges. It is now bounded to `100vw` minus the page gutter, hugs
its longest label (182px rather than stretching), and wraps instead of clipping.

If it still happens, the viewport width or device would pin it down.

### Post editor field hints

The worked example moved into each field as a placeholder, with a one-line rule
underneath, so a one-line field no longer carries a two-sentence paragraph.

### Announcements

`/announcements` was a hardcoded editorial list unconnected to the blog. An
admin-only **Post this as an announcement** checkbox now appears in the post
editor, and the page lists flagged posts above the hand-written notices. A
flagged post stays a normal post — it keeps its thread and votes.

The flag is read through the permission gate in the store rather than from the
form, and a moderator saving an edit preserves it rather than clearing it.

---

## 2. Backend trim

Driven by a repo-wide over-engineering audit. The Java itself was found to be
lean — `Votable`, `DefaultFolder`, `VoteDelta`, `ConditionalOnSearchEnabled` and
`SearchOutboxEntry` all earn their keep and say why. The waste was whole
features nothing called, and repo artefacts.

### Deleted

| What | Why |
| --- | --- |
| `openapi.yaml` (1,260 lines) | Hand-maintained duplicate of `openapi.json`, which is generated from the controller signatures and verified in CI. The README admitted the yaml could drift. |
| `APIs/` (60 files) | Bruno collection duplicating that same spec. |
| `k6-scripts/` | Load test aimed at `/api/v1/user`, which nothing calls. |
| `HELP.md` | Spring Initializr boilerplate linking Boot 3.3.1 docs. |
| `.gitlab-ci.yml` | Pipeline for a GitLab remote this repo no longer has. |
| `SQL/migrate-legacy-vote-tables.sql` | One-off migration for databases predating the vote rewrite. |
| `RequestFilter` | Logged every request header and query parameter at INFO — it would have logged bearer tokens the moment auth arrived. Tomcat's access log does this. |
| by-id GETs on department/year/semester | The client only ever reads each small table whole. Their orphaned service methods went too. |
| `user-bucket` key | Dead config; no avatar feature exists. |

`WebSecurityConfig` was **kept** and documented as the seam Azure AD attaches
to. `micrometer-registry-prometheus` was **kept**, as requested.

### Search indexes reduced to files

Posts, comments and subjects were indexed and never queried — the application
makes one search call, over file names. An unread index still costs a document
build and a push on every write to the entity behind it, and voting is the
hottest write path in the service.

Removing them took the three index definitions, their documents, the
`DocumentFactory` branches, `/search/{subjects,posts,comments}`, the capture
calls in `VoteService` and `FavouriteService`, the username fan-out in
`UserService`, and three of four reconcile statements. `VoteService` no longer
depends on the search layer at all.

`SearchSyncIntegrationTest` was rewritten around files rather than deleted — the
outbox mechanisms it proves (capture, durability, rollback safety, reconcile)
are aggregate-agnostic.

The README was brought in step: the deleted sections removed, the search section
rewritten around the single index, and the codebase-size figures recounted
(they were already stale — 12 controllers and 64 mappings, not 9 and 54).

---

## 3. The blog moved off localStorage

`community.ts` was a browser-held stand-in for endpoints that already existed.
It now reads over `/post/all` and `/comment/all` and writes through `/post`,
`/comment` and their vote and favourite endpoints, following the split
`LibraryApi` already used: `httpResource` for state a screen watches, promises
for commands it issues. Every write refreshes; nothing is optimistic, because
the server owns the tallies.

**The wire shape forced two changes.** `PostResponse` carries vote and favourite
*counts*, not the arrays of account ids the stand-in held. So `scoreOf`
subtracts counts, `voteOf` is gone, and whether *you* voted comes from your own
lists at `/user/votes/*` and `/user/favourites/posts` — that is what
`myVoteOnPost`, `myVoteOnComment` and `hasFavourited` read. Posts also carry
`authorUsername`, so a feed row needs no account lookup.

**Two columns were added to the backend.** `commentsClosed` and `announcement`
existed only in browser storage; the server had nowhere to put them. Without
them, closing a thread and the announcements feature would have disappeared
rather than moved.

**Deleted:** the subject coursefile stand-in, whose links were inert `#`
anchors, and the `hasObject` guard that only existed to grey them out.

### Verified

- `/post/all` and `/comment/all` return 200 in the browser; the blog renders its
  empty state.
- Create → read → delete round-trip through the real API (test post removed).
- `tsc` clean on app and spec; production build clean; Prettier clean.

---

## Open items

### 1. Tailscale is stopped — the API cannot start

`100.109.97.102` hosts Postgres, Redis and Meilisearch, and it is unreachable.
The API was killed to pick up the two new columns and cannot come back until the
network is up:

```bash
tailscale up
```

Then start the API; `ddl-auto: update` adds `comments_closed` and `announcement`
on boot.

### 2. `test-compile` corrupts the MapStruct output

Reproducible:

```bash
mvn clean compile   # CommentMapperImpl → interfaces: 1  (correct)
mvn test-compile    # CommentMapperImpl → interfaces: 0  (implements nothing)
```

`test-compile` silently replaces every `*MapperImpl` in `target/classes` with a
version implementing nothing, so no mapper registers as a bean and all
container-backed tests die on `No qualifying bean of type
'com.documan.mapper.CommentMapper'`. On a clean build the suite is 68 tests /
48 errors; the same 48 fail before and after this session's work.

The application itself is unaffected — this only bites after a test compile.

Ruled out: annotation processing during the test compile (`proc=none` changes
nothing, confirmed in the effective config) and incremental compilation
(disabling it changes nothing). The file's SHA changes while its mtime does
not. This points at the Lombok 1.18.46 / MapStruct 1.6.3 / JDK 25 / Boot 4.1 /
compiler-plugin 3.15 combination rather than project code. Next thing to try:
pin the compiler plugin, or drop `lombok-mapstruct-binding`.

### 3. `openapi.json` is stale

It still lists the six deleted paths and lacks the two new post fields. Both
routes to regenerate are shut: `OpenApiExportTest` needs a Spring context
(item 2) and the live `/v3/api-docs` needs a running app (item 1). The Angular
types are generated from this file (`npm run api:types`).

### 4. Accounts and the server disagree about identity

The browser directory has six accounts, ids 1–6, with id 1 = `theinhumaneme`
(admin). The database has one user: id 1 = `priya.iyer`, role regular. A post
written while signed in as `theinhumaneme` is attributed to `priya.iyer`, and
ids 2–6 will 404 on write.

This is the seam left by keeping accounts local, and Azure AD closes it. Until
then the blog is only usable as database user 1.

### 5. The blog is empty

The three seeded posts and their comments were localStorage fixtures and were
never in the database. Correct, but it will look like data loss.

### 6. Two file lists still have the broken Download

`finder.html` and `blog/favourites.ts`, as described in section 1.

---

## Notes

- `documan-angular-22/.claude/launch.json` is unchanged; a `documan-web-4321`
  entry was added to the **outer** `.claude/launch.json` so a second dev server
  could run alongside another session's.
- `application-dev.yml` is gitignored and was already staged for removal from
  tracking; that removal is committed. The claim that real R2 credentials remain
  in earlier history turned out to be wrong: the copy of `application-dev.yml`
  that was committed used `dev-access-key` and `example.r2.cloudflarestorage.com`,
  and `git log -S` across all 135 commits finds no real credential. The live
  values exist only in the untracked working copy.
- Nothing in this session authorised or secured anything server-side. The
  announcement flag is offered to admins by the client, but the service
  authorises nothing until Azure AD lands.
