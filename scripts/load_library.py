#!/usr/bin/env python3
"""
Rebuild the library: subjects and folders for every department, files for two.

Source layout, merged across the seven split-archive parts (the same subject
appears in several parts holding different subfolders, so everything is unioned):

    <part>/<DEPT>/<YEAR>/SEMESTER - <n>/<Name>_<CODE>_<isTheory>_<isLab>/<UNIT - n|Lab>/*
    <part>/<DEPT>/<YEAR>/SEMESTER - <n>/CourseFile/<Theory|Lab>/<Name>.pdf

All six departments get their subjects and folders, so the whole catalogue is
browsable. Only the departments in FILE_DEPARTMENTS get their files uploaded;
the rest are structure with nothing in it yet, which is the shape a subject is
in anyway before anyone files something into it.

Everything goes through the HTTP API so provisioning, validation and search
indexing run exactly as they would for a real caller.

Uploads post a folder and nothing else — the API takes `folderId` alone now, and
the folder already says which subject a file belongs to.

Resumable: subjects are matched on (dept, year, semester, code) and files on
(folder, name), so a rerun after an interruption tops up instead of duplicating.
There is no uniqueness constraint on subject, so a blind rerun would otherwise
create a second copy of every one.

Needs a Clerk session token in DOCUMAN_TOKEN. Every write here is a POST, and
since the Clerk migration the filter chain answers 401 to an unauthenticated one
— when this script was first written the whole API was open. Get one from a
signed-in browser tab:

    await window.Clerk.session.getToken({ template: 'documan' })

Clerk session tokens are short-lived (about a minute by default), so a run long
enough to outlive one needs a template with a longer lifetime, or a rerun — which
is safe, being resumable.
"""
import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = os.environ.get("DOCUMAN_API", "http://localhost:8080/api/v1")
# Matches SQL/load-data.sh. The compose stack names the database and user
# differently (DB_NAME/DB_USERNAME in .env), so override with DOCUMAN_DSN there.
DSN = os.environ.get(
    "DOCUMAN_DSN", "host=localhost port=5432 dbname=postgres user=root password=password"
)
TOKEN = os.environ.get("DOCUMAN_TOKEN", "").strip()
# Only used by --reset-files, to drop the documents for the rows it deletes.
MEILI_HOST = os.environ.get("MEILI_HOST", "http://localhost:7700")
MEILI_KEY = os.environ.get("MEILI_MASTER_KEY", "masterKey")
MEILI_INDEX_PREFIX = os.environ.get("MEILI_INDEX_PREFIX", "documan_dev_")
DEPARTMENTS = {
    "CSE": "Computer Science Engineering",
    "ECE": "Electronics and Communication Engineering",
    "IT": "Information Technology",
    "MECH": "Mechanical Engineering",
    "EEE": "Electrical Engineering",
    "CIVIL": "Civil Engineering",
}
# Which departments' files are uploaded. The others are scanned and get their
# subjects and folders like any other, and are then left out of the upload plan.
FILE_DEPARTMENTS = {"CSE", "ECE"}
# Insertion order, which must stay as it is: these ids come from an identity
# column and SQL/department.sql seeds them in this order, so reordering here
# would renumber departments in a database seeded by one and not the other.
ALL_DEPARTMENTS = [
    "Computer Science Engineering",
    "Electronics and Communication Engineering",
    "Information Technology",
    "Mechanical Engineering",
    "Electrical Engineering",
    "Civil Engineering",
]
YEARS = ["I", "II", "III", "IV"]
SEMESTERS = ["I", "II"]
SEM_DIR = {"SEMESTER - 1": "I", "SEMESTER - 2": "II"}
SUBJECT_DIR = re.compile(r"^(.+)_([^_]+)_([YN])_([YN])$")
UNIT_DIR = re.compile(r"^UNIT\s*-\s*([1-9])$")

LOCK = threading.Lock()
LOG = None


def say(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    with LOCK:
        print(line, flush=True)
        if LOG:
            LOG.write(line + "\n")
            LOG.flush()


def api(method, path, body=None, params=None):
    url = API + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    # Sent on reads too. They are public, but a GET that carries the token is what
    # provisions the caller's row on their first request, so /user/me below can
    # report who this run will be attributed to.
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw[:300].decode(errors="replace")}
    except urllib.error.URLError as e:
        # No response at all — the API is down or the host is wrong. Reported as a
        # status so one unreachable moment logs a failure like any other rather
        # than ending the run in a traceback. Must follow HTTPError, which is a
        # subclass of this.
        return 0, {"detail": "cannot reach %s: %s" % (API, e.reason)}


def folder_slug(name):
    unit = UNIT_DIR.match(name)
    if unit:
        return "unit-%s" % unit.group(1)
    return "lab" if name == "Lab" else None


def normalise(text):
    text = re.sub(r"\b(and|of|the|using|for)\b", " ", text.lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def match_coursefile(stem, candidates):
    """Attribute a CourseFile PDF to a subject in the same semester.

    Four passes, most to least certain: exact name; a normalisation forgiving
    dropped conjunctions and capitalisation ("Theory Of Computation"); the
    subject code, which is how lab workbooks are named ("DAA Lab WorkBook.pdf");
    then containment. What still does not match is an elective with no subject
    directory that semester, and is reported rather than guessed at.
    """
    for rec in candidates:
        if stem == rec["name"]:
            return rec, "exact"
    target = normalise(stem)
    for rec in candidates:
        if target == normalise(rec["name"]):
            return rec, "normalised"
    tokens = set(re.findall(r"[A-Za-z0-9]+", stem.upper()))
    for rec in candidates:
        if rec["code"].upper() in tokens:
            return rec, "code"
    for rec in candidates:
        other = normalise(rec["name"])
        if other and (other in target or target in other):
            return rec, "substring"
    return None, "unmatched"


def scan(parts):
    subjects, coursefiles = {}, []
    for part in parts:
        for dept in DEPARTMENTS:
            dept_dir = os.path.join(part, dept)
            if not os.path.isdir(dept_dir):
                continue
            for year in sorted(os.listdir(dept_dir)):
                year_dir = os.path.join(dept_dir, year)
                if not os.path.isdir(year_dir) or year not in YEARS:
                    continue
                for sem_name in sorted(os.listdir(year_dir)):
                    sem_dir = os.path.join(year_dir, sem_name)
                    if not os.path.isdir(sem_dir) or sem_name not in SEM_DIR:
                        continue
                    sem = SEM_DIR[sem_name]
                    for entry in sorted(os.listdir(sem_dir)):
                        entry_dir = os.path.join(sem_dir, entry)
                        if not os.path.isdir(entry_dir):
                            continue
                        if entry == "CourseFile":
                            for kind in sorted(os.listdir(entry_dir)):
                                kind_dir = os.path.join(entry_dir, kind)
                                if os.path.isdir(kind_dir):
                                    for f in sorted(os.listdir(kind_dir)):
                                        p = os.path.join(kind_dir, f)
                                        if os.path.isfile(p):
                                            coursefiles.append((dept, year, sem, f, p))
                            continue
                        m = SUBJECT_DIR.match(entry)
                        if not m:
                            say("  ! unparseable subject dir: %s" % entry_dir)
                            continue
                        rec = subjects.setdefault(
                            (dept, year, sem, entry),
                            dict(dept=dept, year=year, sem=sem, name=m.group(1),
                                 code=m.group(2), theory=m.group(3) == "Y",
                                 lab=m.group(4) == "Y", files=collections.defaultdict(list)),
                        )
                        for sub in sorted(os.listdir(entry_dir)):
                            sub_dir = os.path.join(entry_dir, sub)
                            if not os.path.isdir(sub_dir):
                                continue
                            slug = folder_slug(sub)
                            if slug is None:
                                say("  ! unknown folder: %s" % sub_dir)
                                continue
                            rec["files"].setdefault(slug, [])
                            for f in sorted(os.listdir(sub_dir)):
                                p = os.path.join(sub_dir, f)
                                if os.path.isfile(p):
                                    rec["files"][slug].append(p)
    return subjects, coursefiles


def seed_reference():
    """Idempotent: only inserts what is not already there, so reruns are safe."""
    import psycopg
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        for name in ALL_DEPARTMENTS:
            cur.execute("insert into department (name) select %s where not exists "
                        "(select 1 from department where name = %s)", (name, name))
        for v in YEARS:
            cur.execute("insert into year (year) select %s where not exists "
                        "(select 1 from year where year = %s)", (v, v))
        for n in SEMESTERS:
            cur.execute("insert into semester (name) select %s where not exists "
                        "(select 1 from semester where name = %s)", (n, n))
        c.commit()
    return (
        {d["name"]: d["id"] for d in api("GET", "/department/all")[1]},
        {y["value"]: y["id"] for y in api("GET", "/year/all")[1]},
        {s["name"]: s["id"] for s in api("GET", "/semester/all")[1]},
    )


def existing_subjects():
    import psycopg
    short = {v: k for k, v in DEPARTMENTS.items()}
    out = {}
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("""select s.id, d.name, y.year, sm.name, s.code from subject s
                       join department d on d.id = s.department_id
                       join year y on y.id = s.year_id
                       join semester sm on sm.id = s.semester_id""")
        for sid, dept, year, sem, code in cur.fetchall():
            out[(short.get(dept, dept), year, sem, code)] = sid
    return out


def existing_files():
    import psycopg
    out = set()
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("select folder_id, name from file")
        for fid, name in cur.fetchall():
            out.add((fid, name))
    return out


def send(item):
    cmd = [
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "900",
        "-X", "POST", API + "/file",
        # The filename is quoted inside the -F value: curl reads a bare comma in
        # @path as a separator between several files, so "notes( a, b).pdf" is
        # taken as two paths that do not exist and the request never goes out.
        "-F", 'file=@"%s"' % item["path"],
        "-F", "folderId=%d" % item["folderId"],
    ]
    if TOKEN:
        cmd += ["-H", "Authorization: Bearer " + TOKEN]
    try:
        code = subprocess.run(cmd, capture_output=True, text=True, timeout=960).stdout.strip()
    except subprocess.TimeoutExpired:
        code = "timeout"
    return item, code


def pages(path, params=None, size=100):
    """Every element of a paginated endpoint, one page at a time.

    Sorted by id, and that is not cosmetic. These endpoints declare no default
    sort, so the underlying query has no ORDER BY and Postgres may return rows
    in a different order for each LIMIT/OFFSET window — which means a row can
    sit past the boundary on one page and before it on the next, and never be
    seen. That is not hypothetical: reading /subject/all this way missed two
    subjects, so the resume believed they were absent and created them a second
    time. Any total order fixes it; id is the one every row has.
    """
    page = 0
    while True:
        query = dict(params or {}, page=page, size=size, sort="id,asc")
        status, body = api("GET", path, params=query)
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError("%s answered %s %s" % (path, status, body))
        for row in body.get("content", []):
            yield row
        if body.get("last", True):
            return
        page += 1


def reference_ids_via_api():
    """The reference rows, read rather than seeded.

    The no-db counterpart of seed_reference. It cannot insert, so a missing row
    is fatal here rather than something to fix in passing — filing a subject
    under a department that does not exist is not a thing to guess at, and a
    running deployment has these already.
    """
    dept = {d["name"]: d["id"] for d in api("GET", "/department/all")[1]}
    year = {y["value"]: y["id"] for y in api("GET", "/year/all")[1]}
    sem = {s["name"]: s["id"] for s in api("GET", "/semester/all")[1]}
    missing = ([("department", n) for n in ALL_DEPARTMENTS if n not in dept]
               + [("year", v) for v in YEARS if v not in year]
               + [("semester", n) for n in SEMESTERS if n not in sem])
    if missing:
        sys.exit("These reference rows are absent from %s and --no-db cannot create them:\n  %s"
                 % (API, "\n  ".join("%s '%s'" % m for m in missing)))
    return dept, year, sem


def existing_subjects_via_api():
    short = {v: k for k, v in DEPARTMENTS.items()}
    out = {}
    for s in pages("/subject/all"):
        dept = s["department"]["name"]
        out[(short.get(dept, dept), s["year"]["value"], s["semester"]["name"], s["code"])] = s["id"]
    return out


def existing_files_via_api(subject_ids):
    """The (folder, name) pairs already filed, asked subject by subject.

    Only the subjects that were already there are worth asking about — one just
    created by this run is empty by construction. So a first run against an empty
    deployment makes no calls at all, and only a resume pays for this.
    """
    done = set()
    for sid in subject_ids:
        for f in pages("/file/subject", params={"subjectId": sid}):
            done.add((f["folderId"], f["name"]))
    return done


def clear_search_index():
    """Empty the files index, or say why it could not be.

    Search is optional — a deployment with it switched off has no Meilisearch to
    talk to — so failing to reach it is reported and stepped over rather than
    ending the run. A stale index is worth a warning; it is not worth refusing to
    load the library.
    """
    url = "%s/indexes/%sfiles/documents" % (MEILI_HOST.rstrip("/"), MEILI_INDEX_PREFIX)
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", "Bearer " + MEILI_KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            json.loads(r.read() or b"{}")
        say("RESET cleared search index %sfiles" % MEILI_INDEX_PREFIX)
    except Exception as e:
        say("  WARN could not clear search index at %s (%s). Documents for the deleted files "
            "will linger until the reconcile job runs." % (url, e))


def reset_files():
    """Drop every file row, for a rerun that follows an emptied bucket.

    Resumability is keyed on (folder, name), which is what makes a rerun cheap —
    and what makes it wrong after the bucket is wiped. The rows still name objects
    that no longer exist, so the plan skips all of them and the library is left
    pointing at nothing. Emptying R2 and emptying this table are one act.

    favourite_file goes first: it is the only thing referencing file, and it is a
    reader's bookmark of an object that no longer exists either.

    The search index has to be emptied in the same breath. These deletes are raw
    SQL, so Hibernate never loads the rows, SearchEntityListener never fires, and
    nothing is enqueued to the outbox — the documents would simply stay, and a
    search would keep answering with files that exist in neither the database nor
    the bucket. Clearing the whole index is right rather than heavy-handed: every
    row that could be indexed is being deleted, and the upload that follows
    reindexes each file as it lands.
    """
    import psycopg
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("select count(*) from file")
        before = cur.fetchone()[0]
        cur.execute("delete from favourite_file")
        favourites = cur.rowcount
        cur.execute("delete from file")
        c.commit()
    say("RESET deleted %d file rows and %d favourites" % (before, favourites))
    clear_search_index()


def preflight():
    """Refuse to start rather than fail 1,039 uploads in.

    A write with no token is a 401 and a write with a stale one is a 401 too, and
    either way the run does nothing but log failures for an hour. /user/me is the
    cheapest question that proves the token verifies, and its answer names the
    account every uploaded file will be attributed to.
    """
    if not TOKEN:
        sys.exit(
            "DOCUMAN_TOKEN is not set. Every write here is a POST and the API now\n"
            "answers 401 to an unauthenticated one. From a signed-in browser tab:\n"
            "    await window.Clerk.session.getToken({ template: 'documan' })"
        )
    status, body = api("GET", "/user/me")
    if status != 200:
        sys.exit("Token rejected by %s/user/me: %s %s" % (API, status, body))
    say("AUTH ok as %s (id %s, role %s)"
        % (body.get("username"), body.get("id"), body.get("role")))


def main():
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--log")
    ap.add_argument("--reset-files", action="store_true",
                    help="delete every file row first; use when the R2 bucket has been emptied, "
                         "because otherwise the resumable plan skips rows whose objects are gone")
    ap.add_argument("--no-db", action="store_true",
                    help="use only the HTTP API, never a direct database connection. For a remote "
                         "deployment whose Postgres is not reachable and whose credentials this "
                         "machine should not hold")
    args = ap.parse_args()
    if args.log:
        LOG = open(args.log, "a")
    if args.no_db and args.reset_files:
        # Nothing in the API deletes files in bulk, and doing it one DELETE at a
        # time is a different, slower and far more dangerous operation than the
        # one --reset-files names. Refusing is honest; quietly doing less is not.
        sys.exit("--reset-files needs a database connection and cannot run under --no-db. "
                 "Clear the target's file table and search index yourself first.")

    preflight()
    if args.reset_files:
        reset_files()

    parts = sorted(glob.glob(os.path.join(args.root, "drive-download-*")))
    subjects, coursefiles = scan(parts)

    by_sem = collections.defaultdict(list)
    for rec in subjects.values():
        by_sem[(rec["dept"], rec["year"], rec["sem"])].append(rec)
    how, unmatched = collections.Counter(), []
    for dept, year, sem, name, path in coursefiles:
        rec, method = match_coursefile(os.path.splitext(name)[0], by_sem[(dept, year, sem)])
        how[method] += 1
        if rec is None:
            unmatched.append((dept, year, sem, name))
        else:
            rec["files"].setdefault("coursefiles", []).append(path)
    say("SCAN %d parts | %d subjects | %d coursefiles %s"
        % (len(parts), len(subjects), len(coursefiles), dict(how)))

    dept_id, year_id, sem_id = reference_ids_via_api() if args.no_db else seed_reference()
    known = existing_subjects_via_api() if args.no_db else existing_subjects()
    preexisting = set(known.values())
    created = reused = custom_lab = 0
    folder_ids = {}
    for key, rec in sorted(subjects.items()):
        ident = (rec["dept"], rec["year"], rec["sem"], rec["code"])
        sid = known.get(ident)
        if sid is None:
            wanted = sorted(s for s in rec["files"] if s != "lab" or rec["lab"])
            status, body = api("POST", "/subject", {
                "name": rec["name"], "code": rec["code"],
                "lab": rec["lab"], "theory": rec["theory"],
                "departmentId": dept_id[DEPARTMENTS[rec["dept"]]],
                "yearId": year_id[rec["year"]],
                "semesterId": sem_id[rec["sem"]],
                "defaultFolders": wanted,
            })
            if status != 201:
                say("  FAIL subject %s -> %s %s" % (rec["name"], status, body))
                continue
            created += 1
            sid = body["id"]
            # The name is authoritative for is_lab, so a subject that says lab=N
            # but holds Lab material gets the folder as a custom one instead.
            if "lab" in rec["files"] and not rec["lab"]:
                st, _ = api("POST", "/folder", {"name": "Lab"}, {"subjectId": sid})
                if st == 201:
                    custom_lab += 1
        else:
            reused += 1
        rec["subject_id"] = sid
        folder_ids[sid] = {f["slug"]: f["id"]
                           for f in api("GET", "/folder", params={"subjectId": sid})[1]}
        if (created + reused) % 25 == 0:
            say("STRUCTURE %d/%d subjects" % (created + reused, len(subjects)))
    say("STRUCTURE done: %d created, %d already present, %d custom Lab folders"
        % (created, reused, custom_lab))
    if unmatched:
        say("SKIPPED %d coursefiles with no matching subject: %s"
            % (len(unmatched), "; ".join("%s %s %s %s" % u for u in unmatched[:6])))

    done = existing_files_via_api(preexisting) if args.no_db else existing_files()
    plan, already, skipped_depts = [], 0, collections.Counter()
    for key, rec in sorted(subjects.items()):
        sid = rec.get("subject_id")
        if sid is None:
            continue
        # Structure-only. The subject and its folders were created above; the
        # material stays on disk until someone asks for that department.
        if rec["dept"] not in FILE_DEPARTMENTS:
            skipped_depts[rec["dept"]] += sum(len(p) for p in rec["files"].values())
            continue
        for slug, paths in sorted(rec["files"].items()):
            fid = folder_ids.get(sid, {}).get(slug)
            if fid is None:
                continue
            for path in paths:
                if (fid, os.path.basename(path)) in done:
                    already += 1
                    continue
                plan.append({"path": path, "folderId": fid, "size": os.path.getsize(path)})

    if skipped_depts:
        say("STRUCTURE-ONLY %d files left on disk in %s"
            % (sum(skipped_depts.values()),
               ", ".join("%s=%d" % kv for kv in sorted(skipped_depts.items()))))

    total = len(plan)
    total_bytes = sum(p["size"] for p in plan)
    say("START %d uploads, %.2f GB, %d workers (%d already present)"
        % (total, total_bytes / 1073741824, args.workers, already))
    if not total:
        say("DONE ok=0 fail=0 — nothing to upload")
        return

    state = {"n": 0, "ok": 0, "fail": 0, "bytes": 0}
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for item, code in pool.map(send, plan):
            with LOCK:
                state["n"] += 1
                state["bytes"] += item["size"]
                state["ok" if code == "201" else "fail"] += 1
                n, ok, fail, b = state["n"], state["ok"], state["fail"], state["bytes"]
            # A 401 mid-run is the token expiring, not this file being bad, and
            # every upload still queued will fail the same way. Stopping at the
            # first one keeps the log readable and the rerun obvious; what has
            # already landed is recorded, so resuming picks up from here.
            if code == "401":
                say("  ABORT token expired or rejected after %d uploads — rerun with a fresh "
                    "DOCUMAN_TOKEN (without --reset-files) to continue" % n)
                break
            if code != "201":
                say("  FAIL %s %s" % (code, os.path.basename(item["path"])))
            if n % 50 == 0 or n == total:
                elapsed = max(time.time() - started, 0.001)
                rate = b / elapsed
                say("PROGRESS %d/%d | %.2f/%.2f GB | %.1f MB/s | ~%.0fm left | ok=%d fail=%d"
                    % (n, total, b / 1073741824, total_bytes / 1073741824,
                       rate / 1048576, (total_bytes - b) / max(rate, 1) / 60, ok, fail))
    say("DONE ok=%d fail=%d in %.0fs" % (state["ok"], state["fail"], time.time() - started))


if __name__ == "__main__":
    main()
