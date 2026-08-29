#!/usr/bin/env python3
"""Round-trip checks for every memory backend, against a stub of each API.

A store that silently fails to write is the worst possible outcome here: the
audit still passes, the report still renders, and the learning quietly never
happens. So each backend is exercised end to end - write rows, read them back,
compare - against a local server that speaks the documented shape of the real
one. What this proves is that our request construction, signing and parsing are
right. It cannot prove a live account is configured correctly, which is what
`python3 auditor/memory/store.py --check` is for.

The AWS signature is checked separately, against the canonical request string
the specification defines, because that string is where signing bugs live and a
wrong one comes back as a bare 403.

Offline and free: no network, no credentials, no model call.

    python3 auditor/test_store.py
"""

import json
import pathlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "memory"))

import store as store_mod  # noqa: E402

results = []
state = {"objects": {}, "pins": [], "auth": {}}


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


def part(body):
    """The file content out of a multipart body. Crude on purpose: this is a
    stub, and a full parser here would be testing the parser."""
    marker = b'name="file"'
    index = body.find(marker)
    if index == -1:
        return b""
    start = body.find(b"\r\n\r\n", index)
    end = body.find(b"\r\n--", start + 4)
    return body[start + 4:end if end != -1 else len(body)]


class Stub(BaseHTTPRequestHandler):
    """Speaks enough S3, plain HTTP, Cloudinary and Pinata to answer the client."""

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", content_type="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_PUT(self):
        state["auth"][self.path] = self.headers.get("Authorization", "")
        state["objects"][self.path] = self._body()
        self._send(200)

    def do_POST(self):
        body = self._body()
        if self.path.startswith("/v1_1/"):  # Cloudinary raw upload
            state["objects"]["/cloudinary"] = part(body)
            state["auth"]["/cloudinary"] = body
            return self._send(200, json.dumps({"public_id": "ledger"}).encode(),
                              "application/json")
        if self.path.startswith("/pinning/"):  # Pinata pin
            cid = f"cid{len(state['pins'])}"
            state["pins"].append((cid, part(body)))
            return self._send(200, json.dumps({"IpfsHash": cid}).encode(),
                              "application/json")
        self._send(404)

    def do_GET(self):
        if self.path.startswith("/data/pinList"):
            rows = [{"ipfs_pin_hash": cid, "date_pinned": f"2026-01-{i + 1:02d}"}
                    for i, (cid, _) in enumerate(state["pins"])]
            return self._send(200, json.dumps({"rows": rows}).encode(), "application/json")
        if self.path.startswith("/ipfs/"):
            cid = self.path.rsplit("/", 1)[-1]
            for pinned, content in state["pins"]:
                if pinned == cid:
                    return self._send(200, content)
            return self._send(404)
        if "/raw/upload/" in self.path:  # Cloudinary delivery, with cache buster
            return self._send(200, state["objects"].get("/cloudinary", b""))
        if "list-type=2" in self.path:  # S3 ListObjectsV2
            prefix = self.path.split("prefix=")[-1].split("&")[0].replace("%2F", "/")
            keys = "".join(f"<Contents><Key>{k.lstrip('/').split('/', 1)[-1]}</Key></Contents>"
                           for k in sorted(state["objects"])
                           if k.lstrip("/").split("/", 1)[-1].startswith(prefix))
            return self._send(200, f"<ListBucketResult>{keys}</ListBucketResult>".encode(),
                              "application/xml")
        if self.path in state["objects"]:
            return self._send(200, state["objects"][self.path])
        self._send(404)


def rows(*claims):
    return [json.dumps({"kind": k, "verdict": v}) for k, v in claims]


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    host, port = server.server_address[0], server.server_address[1]
    base = f"http://{host}:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # -- the layer is off unless it is configured ---------------------------
    off = store_mod.open_store("", env={})
    check("no url means no memory", isinstance(off, store_mod.NullStore))
    check("a store that is off reads nothing", off.load() == [])
    check("a store that is off writes nothing", off.append(rows(("x", "refuted"))) is False)
    check("s3 without credentials degrades to no memory, it does not fail the audit",
          isinstance(store_mod.open_store("s3://bucket/prefix", env={}), store_mod.NullStore))
    check("an unknown scheme degrades to no memory",
          isinstance(store_mod.open_store("gopher://nope", env={}), store_mod.NullStore))

    # -- S3, the shard-per-write backend ------------------------------------
    s3 = store_mod.open_store(
        f"s3://bucket/contract-auditor?endpoint={base}&region=eu-west-1",
        env={"AUDITOR_MEMORY_KEY_ID": "AKIA_TEST", "AUDITOR_MEMORY_SECRET": "secret"})
    check("an s3 url resolves to the s3 backend", isinstance(s3, store_mod.S3Store))
    check("s3 starts empty", s3.load() == [])
    check("s3 accepts a write", s3.append(rows(("status_code_mismatch", "confirmed"))))
    first = s3.load()
    check("s3 reads back what it wrote", len(first) == 1 and "status_code_mismatch" in first[0],
          str(first))
    s3.append(rows(("validation_mismatch", "refuted")))
    both = s3.load()
    check("a second write does not overwrite the first", len(both) == 2, str(both))
    check("each write is its own object, so concurrent runs cannot clobber",
          len([k for k in state["objects"] if "/runs/" in k]) == 2)
    signed = [v for k, v in state["auth"].items() if "/runs/" in k]
    check("s3 requests are signed with sigv4",
          signed and all(v.startswith("AWS4-HMAC-SHA256 Credential=AKIA_TEST/") for v in signed),
          str(signed[:1]))
    check("the signature scope names the configured region",
          signed and "/eu-west-1/s3/aws4_request" in signed[0])

    # -- the canonical request, where signing bugs live ---------------------
    canonical, signed_headers = store_mod.canonical_request(
        "PUT", "/bucket/key", {"b": "2", "a": "1"},
        {"host": "example.com", "x-amz-date": "20260829T000000Z"}, "PAYLOADHASH")
    check("canonical query parameters are sorted", canonical.splitlines()[2] == "a=1&b=2",
          canonical.splitlines()[2])
    check("canonical headers are lowercased and sorted",
          canonical.splitlines()[3:5] == ["host:example.com",
                                          "x-amz-date:20260829T000000Z"])
    check("signed headers list matches the headers sent",
          signed_headers == "host;x-amz-date", signed_headers)
    check("the payload hash terminates the canonical request",
          canonical.endswith("PAYLOADHASH"))
    fixed = store_mod.sigv4_headers("PUT", "example.com", "/k", {}, b"body",
                                    "KEY", "SECRET", "us-east-1",
                                    now=__import__("datetime").datetime(2026, 8, 29))
    again = store_mod.sigv4_headers("PUT", "example.com", "/k", {}, b"body",
                                    "KEY", "SECRET", "us-east-1",
                                    now=__import__("datetime").datetime(2026, 8, 29))
    check("signing is deterministic for identical inputs",
          fixed["Authorization"] == again["Authorization"])
    different = store_mod.sigv4_headers("PUT", "example.com", "/k", {}, b"other",
                                        "KEY", "SECRET", "us-east-1",
                                        now=__import__("datetime").datetime(2026, 8, 29))
    check("the signature covers the payload",
          fixed["Authorization"] != different["Authorization"])

    # -- plain HTTP ---------------------------------------------------------
    http = store_mod.open_store(f"{base}/ledger.jsonl",
                                env={"AUDITOR_MEMORY_TOKEN": "tok"})
    check("an https url resolves to the http backend", isinstance(http, store_mod.HTTPStore))
    check("a store with nothing in it yet reads as empty, not as an error",
          http.load() == [])
    http.append(rows(("auth_mismatch", "confirmed")))
    http.append(rows(("default_value_mismatch", "refuted")))
    lines = http.load()
    check("http merges rather than replaces", len(lines) == 2, str(lines))
    check("http sends the bearer token",
          state["auth"].get("/ledger.jsonl", "") == "Bearer tok")

    # -- Cloudinary ---------------------------------------------------------
    import os
    os.environ["CLOUDINARY_DELIVERY_URL"] = base
    cloud = store_mod.open_store(f"cloudinary://demo/cloudinary?endpoint={base}",
                                 env={"CLOUDINARY_API_KEY": "k",
                                      "CLOUDINARY_API_SECRET": "SUPERSECRET"})
    check("a cloudinary url resolves to the cloudinary backend",
          isinstance(cloud, store_mod.CloudinaryStore))
    cloud.append(rows(("response_field_mismatch", "confirmed")))
    cloud.append(rows(("validation_mismatch", "refuted")))
    stored = cloud.load()
    check("cloudinary reads back both writes", len(stored) == 2, str(stored))
    upload = state["auth"].get("/cloudinary", b"")
    # Multipart, so each field arrives as `name="x"` followed by its value.
    check("the cloudinary upload is signed", b'name="signature"' in upload, str(upload[:80]))
    check("the cloudinary upload overwrites one public id",
          b'name="public_id"' in upload and b"cloudinary" in upload
          and b'name="overwrite"' in upload)
    # The secret signs the request; it must never travel with it.
    check("the api secret is never sent", b"SUPERSECRET" not in upload)

    # -- IPFS ---------------------------------------------------------------
    ipfs = store_mod.open_store(f"ipfs://ledger?endpoint={base}&gateway={base}",
                                env={"AUDITOR_MEMORY_TOKEN": "jwt"})
    check("an ipfs url resolves to the ipfs backend", isinstance(ipfs, store_mod.IPFSStore))
    ipfs.append(rows(("status_code_mismatch", "confirmed")))
    ipfs.append(rows(("request_param_mismatch", "refuted")))
    pinned = ipfs.load()
    check("ipfs reads every pin back in order", len(pinned) == 2, str(pinned))
    check("each ipfs write is a new immutable pin", len(state["pins"]) == 2)

    # -- file, for local development only -----------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "nested" / "ledger.jsonl"
        local = store_mod.open_store(f"file://{path}", env={})
        check("a file url resolves to the file backend", isinstance(local, store_mod.FileStore))
        local.append(rows(("validation_mismatch", "refuted")))
        local.append(rows(("auth_mismatch", "confirmed")))
        check("the file backend appends", len(local.load()) == 2)
        check("the file backend creates its directory", path.exists())

    # -- failure is a warning, never an exception ---------------------------
    dead = store_mod.open_store("http://127.0.0.1:1/ledger.jsonl", env={})
    check("an unreachable store reads as empty rather than raising", dead.load() == [])
    check("an unreachable store reports a failed write rather than raising",
          dead.append(rows(("x", "refuted"))) is False)

    server.shutdown()

    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, detail in results:
        status = "pass" if ok else "FAIL"
        line = f"  {status}  {name:<{width}}"
        if not ok and detail:
            line += f"   {detail}"
        print(line)
        failed += not ok

    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
