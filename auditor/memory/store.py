#!/usr/bin/env python3
"""Where the claim ledger lives: the storage layer behind self-improvement.

Memory is an optional layer, in the same way the model and the notifications are
optional layers. No credentials means no memory: the audit runs exactly as it
always did, and nothing is written anywhere.

**Nothing is ever stored in the repository or the image.** The published image
carries no ledger, so one project's history can never become another project's
priors, and a consumer's history never lands in a commit. The ledger lives
wherever the operator's credentials point, and that is the only place it lives.

    AUDITOR_MEMORY_URL=s3://my-bucket/contract-auditor
    AUDITOR_MEMORY_KEY_ID=...        AUDITOR_MEMORY_SECRET=...

Five backends, selected by the URL scheme:

    s3://bucket/prefix              any S3-compatible store: AWS, R2, MinIO,
                                    Spaces, B2. `?endpoint=` for non-AWS
    https://host/path/ledger.jsonl  any HTTP store that accepts GET and PUT,
                                    including a presigned URL
    cloudinary://cloud/public_id    Cloudinary raw storage
    ipfs://name                     IPFS via a Pinata-compatible pinning API
    file:///abs/path                local file, for development only

Two write strategies, chosen by what the backend can do rather than by taste.
Stores that can list (S3, IPFS) get one immutable object per write, so two CI
jobs running at once cannot overwrite each other. Stores that cannot list
(HTTP, Cloudinary, file) read, merge and write back, which is last-writer-wins
and documented as such.

Every failure here is a warning, never an exception that reaches the audit. A
store that is unreachable costs the next run its priors; it must not cost anyone
their findings.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
ENV_FILE = HERE.parents[1] / ".env"

# How many past shards to read. A ledger is small - a run of sixteen cases
# produced twenty-six rows - but an unbounded read on a store with years of
# history would slow every audit for priors that are long since stale.
MAX_SHARDS = 200
TIMEOUT = 60

CREDENTIAL_KEYS = (
    "AUDITOR_MEMORY_URL", "AUDITOR_MEMORY_KEY_ID", "AUDITOR_MEMORY_SECRET",
    "AUDITOR_MEMORY_TOKEN", "AUDITOR_MEMORY_REGION",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_DEFAULT_REGION",
    "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET", "PINATA_JWT",
)


class StoreError(RuntimeError):
    pass


def load_env(path=ENV_FILE):
    """Credentials from .env, with the real environment winning.

    Same shape as the model client's loader, and the same rule: values stay in
    this process. Nothing here is ever written to a report, a log line or a
    findings file.
    """
    values = {}
    path = pathlib.Path(path)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in CREDENTIAL_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def warn(message):
    # `::warning::` is the annotation GitHub renders on the run page. Outside
    # Actions it is still a readable line, which is why it is used everywhere.
    print(f"::warning::{message}", flush=True)


def curl(args, data=None, timeout=TIMEOUT):
    """One HTTP call, through curl for the same reason the model client uses it:
    no dependencies, and no LibreSSL stall on the system Python."""
    command = ["curl", "-sS", "-m", str(timeout), "-w", "\n%{http_code}"] + list(args)
    completed = subprocess.run(command, input=data, capture_output=True)
    if completed.returncode != 0:
        raise StoreError(completed.stderr.decode(errors="replace").strip()
                         or f"curl exited {completed.returncode}")
    body, _, status = completed.stdout.rpartition(b"\n")
    try:
        code = int(status.decode().strip())
    except ValueError:
        code = 0
    return code, body


# ---------------------------------------------------------------------------
# AWS Signature Version 4. Sixty lines of hashing beats a dependency, and it is
# what makes every S3-compatible store reachable with no SDK installed.
# ---------------------------------------------------------------------------

def _sha256(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def _sign(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def canonical_request(method, path, query, headers, payload_hash):
    """The exact string AWS hashes. Written out separately because this is where
    signing bugs live, and a bug here is a 403 with no explanation of which of
    the eight rules was broken."""
    canonical_headers = "".join(f"{k.lower()}:{str(v).strip()}\n"
                                for k, v in sorted(headers.items(), key=lambda kv: kv[0].lower()))
    signed_headers = ";".join(sorted(k.lower() for k in headers))
    canonical_query = "&".join(
        f"{urllib.parse.quote(str(k), safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
        for k, v in sorted(query.items()))
    return "\n".join([method, path, canonical_query, canonical_headers,
                      signed_headers, payload_hash]), signed_headers


def sigv4_headers(method, host, path, query, payload, key_id, secret, region,
                  service="s3", now=None):
    now = now or datetime.datetime.utcnow()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    payload_hash = _sha256(payload or b"")

    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": stamp}
    canonical, signed_headers = canonical_request(method, path, query, headers, payload_hash)

    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, _sha256(canonical)])

    signing_key = _sign(_sign(_sign(_sign(f"AWS4{secret}".encode(), day),
                                    region), service), "aws4_request")
    signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()

    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, "
                                f"SignedHeaders={signed_headers}, Signature={signature}")
    return headers


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class Store:
    """A ledger that lives somewhere. Two operations, both failure-tolerant."""

    name = "store"
    listable = False

    def load(self):
        """Every ledger line held, oldest first. [] on any failure."""
        raise NotImplementedError

    def append(self, lines):
        """Add lines. Returns True when they are known to have landed."""
        raise NotImplementedError

    def describe(self):
        return self.name


class NullStore(Store):
    """No credentials, no memory. The audit is unaffected: this is the same
    arrangement as running with no model key, where the deterministic layer
    still does its work."""

    name = "none"

    def load(self):
        return []

    def append(self, lines):
        return False

    def describe(self):
        return "disabled (no memory url configured)"


class FileStore(Store):
    """A local file. Development and the project's own demonstration only.

    Deliberately not the default: a ledger inside the working tree is one
    `git add` away from being published, and a published ledger is one
    project's statistics imposed on everyone who pulls the image.
    """

    name = "file"

    def __init__(self, path):
        self.path = pathlib.Path(path).expanduser()

    def load(self):
        try:
            return self.path.read_text().splitlines() if self.path.exists() else []
        except OSError as exc:
            warn(f"could not read the ledger at {self.path}: {exc}")
            return []

    def append(self, lines):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                for line in lines:
                    f.write(line + "\n")
            return True
        except OSError as exc:
            warn(f"could not write the ledger at {self.path}: {exc}")
            return False

    def describe(self):
        return f"file {self.path}"


class S3Store(Store):
    """Any S3-compatible object store, signed with SigV4 and no SDK.

    Writes one object per call under `<prefix>/runs/`, which is what makes
    concurrent CI jobs safe: two runs write two objects rather than racing to
    overwrite one. Reads list the prefix and fetch the most recent shards.
    """

    name = "s3"
    listable = True

    def __init__(self, bucket, prefix, key_id, secret, region="us-east-1", endpoint=""):
        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.key_id, self.secret, self.region = key_id, secret, region
        if endpoint:
            parsed = urllib.parse.urlparse(endpoint if "://" in endpoint
                                           else f"https://{endpoint}")
            self.scheme, self.host = parsed.scheme, parsed.netloc
            self.root = f"/{bucket}"  # path style: what MinIO and R2 accept
        else:
            self.scheme, self.host = "https", f"{bucket}.s3.{region}.amazonaws.com"
            self.root = ""

    def _request(self, method, path, query=None, payload=b""):
        query = query or {}
        headers = sigv4_headers(method, self.host, path, query, payload,
                                self.key_id, self.secret, self.region)
        url = f"{self.scheme}://{self.host}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(sorted(query.items()))
        args = ["-X", method, url]
        for key, value in headers.items():
            args += ["-H", f"{key}: {value}"]
        if payload:
            args += ["--data-binary", "@-"]
        return curl(args, data=payload if payload else None)

    def _keys(self):
        code, body = self._request("GET", f"{self.root}/",
                                   {"list-type": "2", "prefix": f"{self.prefix}/runs/"})
        if code != 200:
            raise StoreError(f"list returned {code}: {body[:200].decode(errors='replace')}")
        # Regex rather than an XML parser: the response is machine-generated,
        # the element is unambiguous, and this avoids a namespace dance for one
        # tag name.
        return sorted(re.findall(r"<Key>([^<]+)</Key>", body.decode(errors="replace")))

    def load(self):
        try:
            keys = self._keys()[-MAX_SHARDS:]
        except StoreError as exc:
            warn(f"could not list the ledger in s3://{self.bucket}/{self.prefix}: {exc}")
            return []
        lines = []
        for key in keys:
            try:
                code, body = self._request("GET", f"{self.root}/{urllib.parse.quote(key)}")
                if code == 200:
                    lines.extend(body.decode(errors="replace").splitlines())
                else:
                    warn(f"ledger shard {key} returned {code}")
            except StoreError as exc:
                warn(f"could not read ledger shard {key}: {exc}")
        return lines

    def append(self, lines):
        if not lines:
            return True
        key = f"{self.prefix}/runs/{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{os.getpid()}.jsonl"
        payload = ("\n".join(lines) + "\n").encode()
        try:
            code, body = self._request("PUT", f"{self.root}/{urllib.parse.quote(key)}",
                                       payload=payload)
        except StoreError as exc:
            warn(f"could not write the ledger to s3://{self.bucket}/{key}: {exc}")
            return False
        if code not in (200, 201, 204):
            warn(f"ledger write to s3://{self.bucket}/{key} returned {code}: "
                 f"{body[:200].decode(errors='replace')}")
            return False
        return True

    def describe(self):
        return f"s3://{self.bucket}/{self.prefix} ({self.host})"


class HTTPStore(Store):
    """Any endpoint that answers GET and PUT: a presigned URL, a bucket behind a
    proxy, an internal service. Read, merge, write back - so a store with two
    audits running at once keeps only the last writer's rows."""

    name = "http"

    def __init__(self, url, token=""):
        self.url, self.token = url, token

    def _headers(self):
        return ["-H", f"Authorization: Bearer {self.token}"] if self.token else []

    def load(self):
        try:
            code, body = curl(["-X", "GET", self.url] + self._headers())
        except StoreError as exc:
            warn(f"could not read the ledger at {self.url}: {exc}")
            return []
        if code == 404:
            return []  # nothing stored yet: a first run, not a failure
        if code != 200:
            warn(f"ledger read returned {code}")
            return []
        return body.decode(errors="replace").splitlines()

    def append(self, lines):
        if not lines:
            return True
        merged = self.load() + list(lines)
        payload = ("\n".join(merged) + "\n").encode()
        try:
            code, body = curl(["-X", "PUT", self.url, "--data-binary", "@-",
                               "-H", "Content-Type: application/x-ndjson"] + self._headers(),
                              data=payload)
        except StoreError as exc:
            warn(f"could not write the ledger to {self.url}: {exc}")
            return False
        if code not in (200, 201, 204):
            warn(f"ledger write returned {code}: {body[:200].decode(errors='replace')}")
            return False
        return True

    def describe(self):
        return f"http {self.url}"


class CloudinaryStore(Store):
    """Cloudinary raw storage. Uploads overwrite one public id, so this is also
    read, merge, write back."""

    name = "cloudinary"

    def __init__(self, cloud, public_id, api_key, api_secret, base=""):
        self.cloud, self.public_id = cloud, public_id
        self.api_key, self.api_secret = api_key, api_secret
        self.base = (base or "https://api.cloudinary.com").rstrip("/")
        self.delivery = os.environ.get("CLOUDINARY_DELIVERY_URL",
                                       "https://res.cloudinary.com").rstrip("/")

    def _signature(self, params):
        # Cloudinary signs the alphabetised parameters with the api secret. The
        # signature covers everything except api_key, file and the signature.
        payload = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return hashlib.sha1(f"{payload}{self.api_secret}".encode()).hexdigest()

    def load(self):
        # A cache buster: Cloudinary serves raw files through a CDN, and a run
        # that read a cached ledger would silently learn from stale history.
        url = (f"{self.delivery}/{self.cloud}/raw/upload/{self.public_id}"
               f"?_={int(time.time())}")
        try:
            code, body = curl(["-X", "GET", "-L", url])
        except StoreError as exc:
            warn(f"could not read the ledger from cloudinary: {exc}")
            return []
        if code == 404:
            return []
        if code != 200:
            warn(f"cloudinary ledger read returned {code}")
            return []
        return body.decode(errors="replace").splitlines()

    def append(self, lines):
        if not lines:
            return True
        merged = self.load() + list(lines)
        params = {"public_id": self.public_id, "overwrite": "true",
                  "invalidate": "true", "timestamp": str(int(time.time()))}
        args = ["-X", "POST", f"{self.base}/v1_1/{self.cloud}/raw/upload",
                "-F", f"file=@-;filename={self.public_id}",
                "-F", f"api_key={self.api_key}",
                "-F", f"signature={self._signature(params)}"]
        for key, value in params.items():
            args += ["-F", f"{key}={value}"]
        try:
            code, body = curl(args, data=("\n".join(merged) + "\n").encode())
        except StoreError as exc:
            warn(f"could not write the ledger to cloudinary: {exc}")
            return False
        if code not in (200, 201):
            warn(f"cloudinary ledger write returned {code}: "
                 f"{body[:200].decode(errors='replace')}")
            return False
        return True

    def describe(self):
        return f"cloudinary {self.cloud}/{self.public_id}"


class IPFSStore(Store):
    """IPFS through a Pinata-compatible pinning API.

    Content addressing suits this well: every write is a new immutable object,
    which is the same shape as the S3 shards and safe for concurrent runs. The
    pin metadata name is what ties a run's shards together, since a CID cannot
    be looked up by name.
    """

    name = "ipfs"
    listable = True

    def __init__(self, label, token, api="", gateway=""):
        self.label, self.token = label, token
        self.api = (api or os.environ.get("PINATA_API_URL")
                    or "https://api.pinata.cloud").rstrip("/")
        self.gateway = (gateway or os.environ.get("PINATA_GATEWAY_URL")
                        or "https://gateway.pinata.cloud").rstrip("/")

    def _auth(self):
        return ["-H", f"Authorization: Bearer {self.token}"]

    def _cids(self):
        url = (f"{self.api}/data/pinList?status=pinned"
               f"&metadata[name]={urllib.parse.quote(self.label)}&pageLimit={MAX_SHARDS}")
        code, body = curl(["-X", "GET", url] + self._auth())
        if code != 200:
            raise StoreError(f"pin list returned {code}")
        try:
            rows = json.loads(body.decode(errors="replace")).get("rows", [])
        except json.JSONDecodeError:
            raise StoreError("pin list was not JSON")
        return [r.get("ipfs_pin_hash") for r in
                sorted(rows, key=lambda r: str(r.get("date_pinned", ""))) if r.get("ipfs_pin_hash")]

    def load(self):
        try:
            cids = self._cids()
        except StoreError as exc:
            warn(f"could not list ledger pins for {self.label}: {exc}")
            return []
        lines = []
        for cid in cids:
            try:
                code, body = curl(["-X", "GET", "-L", f"{self.gateway}/ipfs/{cid}"])
                if code == 200:
                    lines.extend(body.decode(errors="replace").splitlines())
                else:
                    warn(f"ledger pin {cid} returned {code}")
            except StoreError as exc:
                warn(f"could not read ledger pin {cid}: {exc}")
        return lines

    def append(self, lines):
        if not lines:
            return True
        metadata = json.dumps({"name": self.label})
        args = ["-X", "POST", f"{self.api}/pinning/pinFileToIPFS",
                "-F", f"file=@-;filename={self.label}.jsonl",
                "-F", f"pinataMetadata={metadata}"] + self._auth()
        try:
            code, body = curl(args, data=("\n".join(lines) + "\n").encode())
        except StoreError as exc:
            warn(f"could not pin the ledger: {exc}")
            return False
        if code not in (200, 201):
            warn(f"ledger pin returned {code}: {body[:200].decode(errors='replace')}")
            return False
        return True

    def describe(self):
        return f"ipfs {self.label} ({self.api})"


# ---------------------------------------------------------------------------

def open_store(url="", env=None):
    """Resolve a memory URL and its credentials into a store.

    Returns a NullStore when no URL is configured, which is the default and a
    perfectly good state: the auditor works without memory, it simply does not
    accumulate any. A URL that is malformed or missing its credentials warns and
    also returns NullStore - a broken memory configuration must never be the
    reason an audit does not run.
    """
    env = env if env is not None else load_env()
    url = (url or env.get("AUDITOR_MEMORY_URL") or "").strip()
    if not url:
        return NullStore()

    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    query = dict(urllib.parse.parse_qsl(parsed.query))

    key_id = env.get("AUDITOR_MEMORY_KEY_ID") or env.get("AWS_ACCESS_KEY_ID") or ""
    secret = env.get("AUDITOR_MEMORY_SECRET") or env.get("AWS_SECRET_ACCESS_KEY") or ""
    token = env.get("AUDITOR_MEMORY_TOKEN") or ""

    def missing(what):
        warn(f"memory url {scheme}:// needs {what}; continuing without memory")
        return NullStore()

    if scheme == "s3":
        if not (key_id and secret):
            return missing("AUDITOR_MEMORY_KEY_ID and AUDITOR_MEMORY_SECRET")
        region = (query.get("region") or env.get("AUDITOR_MEMORY_REGION")
                  or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1")
        prefix = parsed.path.strip("/") or "contract-auditor"
        return S3Store(parsed.netloc, prefix, key_id, secret, region,
                       query.get("endpoint", ""))

    if scheme in ("http", "https"):
        return HTTPStore(url, token or env.get("AUDITOR_MEMORY_SECRET", ""))

    if scheme == "cloudinary":
        api_key = key_id or env.get("CLOUDINARY_API_KEY", "")
        api_secret = secret or env.get("CLOUDINARY_API_SECRET", "")
        if not (api_key and api_secret):
            return missing("CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET")
        public_id = parsed.path.strip("/") or "contract-auditor-ledger"
        return CloudinaryStore(parsed.netloc, public_id, api_key, api_secret,
                               query.get("endpoint", ""))

    if scheme in ("ipfs", "pinata"):
        jwt = token or env.get("PINATA_JWT", "")
        if not jwt:
            return missing("AUDITOR_MEMORY_TOKEN (a Pinata JWT)")
        label = (parsed.netloc + parsed.path).strip("/") or "contract-auditor-ledger"
        return IPFSStore(label, jwt, query.get("endpoint", ""), query.get("gateway", ""))

    if scheme == "file":
        return FileStore(urllib.parse.unquote(parsed.path))

    if not scheme:
        return FileStore(url)

    warn(f"unknown memory url scheme {scheme!r}; continuing without memory")
    return NullStore()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--memory-url", default="", help="overrides AUDITOR_MEMORY_URL")
    parser.add_argument("--check", action="store_true",
                        help="write a probe row, read it back, and report")
    args = parser.parse_args()

    store = open_store(args.memory_url)
    print(f"memory store: {store.describe()}")
    if isinstance(store, NullStore):
        print("\nSelf-improvement is off. Set AUDITOR_MEMORY_URL and its credentials "
              "to turn it on; nothing is stored in this repository either way.")
        return

    rows = store.load()
    print(f"holds {len(rows)} ledger line(s)")

    if args.check:
        probe = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "kind": "_probe", "verdict": "unsupported",
                            "note": "written by store.py --check"})
        ok = store.append([probe])
        print(f"write: {'ok' if ok else 'failed'}")
        after = store.load()
        print(f"read back: {len(after)} line(s) "
              f"({'probe found' if any('_probe' in r for r in after) else 'probe NOT found'})")


if __name__ == "__main__":
    main()
