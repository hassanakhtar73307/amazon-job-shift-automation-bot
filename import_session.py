"""Import session headers from a browser's Copy as cURL (bash) request."""

import argparse
import math
from pathlib import Path
import shlex
import time

from amazon_jobs import API_URL, BASE_DIR
from session_manager import token_expiry, save_headers


def parse_request(content: str) -> dict[str, str]:
    content = content.replace("\\\r\n", " ").replace("\\\n", " ")
    lexer = shlex.shlex(content, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    headers = {}
    url = None
    try:
        if next(lexer, "").lower() not in ("curl", "curl.exe"):
            raise ValueError("Copy the successful request using Copy as cURL (bash).")
        for token in lexer:
            # Browser copies put the body last. It is not needed for importing
            # headers, and may use Bash quoting that shlex does not understand.
            if token in ("--data-raw", "--data-binary", "--data", "--json", "-d"):
                break
            option, separator, inline = token.partition("=")
            if option in ("-H", "--header", "-b", "--cookie", "--url"):
                value = inline if separator else next(lexer, None)
                if value is None:
                    raise ValueError("The copied request is incomplete.")
                if option == "--url":
                    url = value
                elif option in ("-b", "--cookie"):
                    if "=" not in value or value.startswith("@"):
                        raise ValueError("Copy inline browser cookies, rather than a cookie filename.")
                    headers["cookie"] = value
                else:
                    name, colon, value = value.partition(":")
                    if not colon or not name.strip():
                        raise ValueError("The copied request contains an invalid header.")
                    headers[name.strip().lower()] = value.strip()
            elif token.startswith(("https://", "http://")) and url is None:
                url = token
    except ValueError:
        raise ValueError("Cannot read this cURL request. Copy the complete successful graphql request as cURL (bash).") from None
    if url != API_URL:
        raise ValueError("Copy the request to https://www.jobsatamazon.co.uk/graphql.")
    if not headers.get("authorization") or not headers.get("cookie"):
        raise ValueError("The copied request must include both authorization and cookie headers.")
    if any("\n" in key + value or "\r" in key + value for key, value in headers.items()):
        raise ValueError("Request headers cannot contain newline characters.")
    return headers


def import_session(source: Path, output: Path) -> float | None:
    headers = parse_request(source.read_text(encoding="utf-8-sig"))
    expires = token_expiry(headers)
    if expires is not None and expires <= time.time():
        raise ValueError("This session token is already expired. Reload the Amazon job search and copy a new successful request.")
    save_headers(output, headers)
    return expires


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", nargs="?", type=Path, default=BASE_DIR / "request.curl", help="saved cURL (bash) request (default: request.curl next to this script)")
    parser.add_argument("--output", type=Path, default=BASE_DIR / "headers.json", help="headers JSON destination")
    args = parser.parse_args()
    try:
        expires = import_session(args.request, args.output)
    except (OSError, ValueError) as error:
        print(f"Session import failed: {error}")
        return 1
    print(f"Session headers saved to {args.output.resolve()}.")
    if expires is not None:
        print(f"The token declares expiry in approximately {max(0, math.ceil((expires - time.time()) / 60))} minutes.")
    print("Restart the recorder: python amazon_jobs.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
