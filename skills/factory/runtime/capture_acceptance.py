"""Capture the foreman's first GitHub read for offline Factory acceptance."""
import argparse
import json
import subprocess
import sys

import factory


FIELDS = """url title body createdAt updatedAt lastEditedAt
    timelineItems(itemTypes: [RENAMED_TITLE_EVENT], last: 1) { nodes { ... on RenamedTitleEvent { createdAt } } }"""
COMMENTS = """comments(last: 20) {
    nodes { url fullDatabaseId authorAssociation author { login ... on User { databaseId } } updatedAt body }
}"""


def capture(args):
    refs = factory.acceptance_issues({"acceptance": args.urls})
    if len(refs) != len(args.urls) or any(ref[0] != url for ref, url in zip(refs, args.urls)):
        raise factory.FactoryError("Supply ordered github.com issue or pull request URLs")
    if args.comments_for is not None and args.comments_for not in args.urls:
        raise factory.FactoryError("--comments-for must name a supplied issue or pull request URL")
    if args.requirements is not None and not args.requirements.strip():
        raise factory.FactoryError("Supply a nonempty requirements label")

    variables, selections, fields = [], [], []
    for index, ref in enumerate(refs, 1):
        url = ref[0]
        name = f"url{index}"
        variables.append(f"${name}: URI!")
        evidence = FIELDS + (" " + COMMENTS if url == args.comments_for else "")
        kind = "Issue"
        if url.endswith("/pull/" + ref[3]):
            kind = "PullRequest"
            evidence += " headRefOid"
        selections.append(f"ref{index}: resource(url: ${name}) {{ ... on {kind} {{ {evidence} }} }}")
        fields.extend(["-f", f"{name}={url}"])
    query = "query(" + ", ".join(variables) + ") { " + " ".join(selections) + " }"
    argv = ["gh", "api", "--hostname", "github.com", "graphql", "-f", "query=" + query, *fields]
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL) as gh:
        assert gh.stdout is not None
        raw = gh.stdout.read(factory.ACCEPTANCE_SNAPSHOT_LIMIT + 1)
        if len(raw) > factory.ACCEPTANCE_SNAPSHOT_LIMIT:
            gh.kill()
            raise factory.FactoryError("GitHub response exceeds the 1 MiB acceptance limit")
        if gh.wait():
            raise factory.FactoryError("gh api graphql failed; check foreman gh access and report missing evidence")
    try:
        response = factory.decode_json(raw.decode("utf-8"))
    except ValueError:
        raise factory.FactoryError("gh returned invalid UTF-8 JSON") from None
    if not isinstance(response, dict) or "errors" in response or not isinstance(response.get("data"), dict):
        raise factory.FactoryError("GitHub returned an incomplete GraphQL response")
    data = response["data"]
    first = data.get("ref1")
    if len(data) != len(refs) or not isinstance(first, dict):
        raise factory.FactoryError("GitHub returned incomplete evidence for the requested references")
    revision = args.requirements
    if revision is None:
        owner, repo, number = refs[0].groups()
        if refs[0][0].endswith("/pull/" + number):
            revision = f"PR{number}@{first.get('headRefOid')}"
        else:
            revision = f"{owner}/{repo}#{number}@{first.get('updatedAt')}"
    factory.acceptance_evidence(refs, revision, data, None)
    for index, ref in enumerate(refs, 1):
        if ref[0] == args.comments_for:
            comments = data[f"ref{index}"].get("comments")
            if not isinstance(comments, dict) or not isinstance(comments.get("nodes"), list):
                raise factory.FactoryError("GitHub omitted the requested comments page")
    result = json.dumps(dict(schema_version=1, requirements=revision, data=data), ensure_ascii=False,
                        separators=(",", ":")).encode("utf-8") + b"\n"
    if len(result) > factory.ACCEPTANCE_SNAPSHOT_LIMIT:
        raise factory.FactoryError("Acceptance snapshot exceeds the 1 MiB limit")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", help="exact operator-selected revision label")
    parser.add_argument("--comments-for", metavar="URL", help="include recent comments on this reference")
    parser.add_argument("urls", metavar="URL", nargs="+")
    args = parser.parse_args()
    try:
        sys.stdout.buffer.write(capture(args))
    except factory.FactoryError as exc:
        print(exc, file=sys.stderr)
        return 1
    except UnicodeError:
        print("GitHub acceptance contains invalid Unicode", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Cannot run gh or write the acceptance snapshot: {exc.strerror}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
