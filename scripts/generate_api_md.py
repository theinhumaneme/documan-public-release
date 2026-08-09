#!/usr/bin/env python3
"""Generate API.md from openapi.json.

The spec is the contract and is generated from the controllers; this is a reading
of it, generated in turn, so neither can drift from the code. Editing API.md by
hand is pointless — the next run overwrites it.

    ./scripts/generate_api_md.py > documan-java21/API.md
"""
import json
import pathlib
import re
import sys

SPEC = pathlib.Path(__file__).resolve().parent.parent / "documan-java21" / "openapi.json"

# Order the resources so the document reads like the product rather than like a
# dictionary: the library first, because that is what a reader comes for.
GROUPS = [
    ("Reference data", ("/department", "/year", "/semester", "/role")),
    ("Library", ("/subject", "/folder", "/file")),
    ("Search", ("/search",)),
    ("Blog", ("/post", "/comment")),
    ("Accounts", ("/user",)),
    ("Maintainer scopes", ("/maintainer-scope",)),
]

METHODS = ("get", "post", "put", "patch", "delete")


def group_for(path):
    for name, prefixes in GROUPS:
        if any(path.startswith("/api/v1" + p) for p in prefixes):
            return name
    return "Other"


def auth_of(op):
    """The prose rule the OperationCustomizer wrote, without the raw expression."""
    if not op.get("security"):
        return "public"
    desc = op.get("description") or ""
    m = re.search(r"\*\*Authorisation:\*\* (.+?)(?:\n|$)", desc)
    return m.group(1).strip() if m else "signed in"


def params(op):
    """Required query parameters.

    `pageable` is dropped: springdoc reports Spring's Pageable as one required
    parameter, but it is three optional ones (`page`, `size`, `sort`) with
    defaults, and listing it as required tells the reader the opposite of the
    truth. Paging is described once in Conventions instead.
    """
    required = [
        p["name"]
        for p in op.get("parameters", [])
        if p.get("required") and p["name"] != "pageable"
    ]
    return ", ".join(f"`{p}`" for p in required) or "—"


def main():
    spec = json.loads(SPEC.read_text())
    info = spec["info"]

    out = []
    w = out.append

    w(f"# {info['title']} — {info['version']}\n")
    w(
        "Generated from [`openapi.json`](openapi.json), which is itself generated from the\n"
        "controller signatures. Do not edit by hand: run `scripts/generate_api_md.py`.\n"
    )

    w("## Base URL\n")
    for s in spec.get("servers", []):
        w(f"- `{s['url']}` — {s.get('description', '')}")
    w("\nEvery path below is relative to that, and already includes the `/api/v1` prefix.\n")

    w("## Authentication\n")
    scheme = spec["components"]["securitySchemes"]["bearerAuth"]
    w(scheme["description"].strip() + "\n")
    w(
        "```http\n"
        "GET /api/v1/user/me HTTP/1.1\n"
        "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...\n"
        "```\n"
    )
    w(
        "**Reading needs no token.** Every `GET` is public except those under `/user`, which\n"
        "return the directory rather than material. Everything that changes something needs one.\n"
    )

    w("## Roles\n")
    w(
        "`regular` < `maintainer` < `moderator` < `admin`, and every check asks *at least this\n"
        "rank* — so an administrator is implicitly a moderator too. A maintainer's rights are\n"
        "scoped to granted departments, years and semesters rather than to the library as a whole,\n"
        "which is why some rules below name a resource rather than a rank.\n"
    )

    w("## Conventions\n")
    w(
        "- **Identifiers are query parameters**, not path segments: `?subjectId=12`.\n"
        "- **Collections return a `PageResponse`** — `content`, `page`, `size`, `totalElements`,\n"
        "  `totalPages`, `first`, `last` — except the reference lookups, which return plain arrays.\n"
        "- **Paging** accepts `page`, `size` and `sort`. Sort by a non-unique column and pages can\n"
        "  overlap or skip; `sort=id,asc` is the safe choice when walking a whole collection.\n"
        "- **Errors are RFC 9457 problem documents** (`application/problem+json`).\n"
    )

    w("### Status codes\n")
    w("| Code | Meaning |")
    w("| --- | --- |")
    w("| `400` | Validation failed, or a rule spanning several fields was broken |")
    w("| `401` | No token, or one that does not verify |")
    w("| `403` | A valid token belonging to somebody not allowed to do this |")
    w("| `404` | No such row |")
    w("| `409` | Conflict — a duplicate, or a concurrent modification |")
    w("| `502` | Object storage failed |")
    w("| `503` | Search is unavailable; carries `Retry-After` |")
    w("")

    paths = spec["paths"]
    grouped = {}
    for path, item in paths.items():
        for method in METHODS:
            if method in item:
                grouped.setdefault(group_for(path), []).append((path, method, item[method]))

    w("## Endpoints\n")
    for name, _ in GROUPS + [("Other", ())]:
        rows = sorted(grouped.get(name, []))
        if not rows:
            continue
        w(f"### {name}\n")
        w("| Method | Path | Required parameters | Who may call it |")
        w("| --- | --- | --- | --- |")
        for path, method, op in rows:
            w(f"| `{method.upper()}` | `{path}` | {params(op)} | {auth_of(op)} |")
        w("")

    total = sum(len(v) for v in grouped.values())
    secured = sum(1 for v in grouped.values() for _, _, op in v if op.get("security"))
    w("---\n")
    w(
        f"{total} operations, {secured} of which require a token. "
        f"{total - secured} are public reads.\n"
    )
    print("\n".join(out))


if __name__ == "__main__":
    sys.exit(main())
