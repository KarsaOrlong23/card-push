"""Card push — knowledge auto-injection before each agent turn.

Extracts keywords from the latest user message, queries a QMD search index
(BM25), and injects the top matching documents into context — push instead
of pull, so knowledge delivery does not depend on the agent remembering to
look it up. Fail-silent on timeout. Sensitive documents (matching a
configurable regex) are never injected. Receipt log per profile.

Tenancy/collection isolation: by default, only the configured base
collections are searched. An additional collection can be temporarily
released via a release file (CARD_PUSH_RELEASE_FILE) containing one line
with a collection slug — this implements a "tenant push" pattern where
only the currently-active collection is accessible, and all others remain
locked. Empty or missing release file = only base collections.

Configuration (all via environment variables, see plugin.yaml):
  CARD_PUSH_QMD                Path to qmd binary (else `qmd` on PATH)
  CARD_PUSH_COLLECTIONS        Comma-separated base collections
  CARD_PUSH_COLLECTIONS_FILE   File with base collections (one per line)
  CARD_PUSH_INDEX              XDG_CACHE_HOME for qmd
  CARD_PUSH_RELEASE_FILE       Release file; one line = additional collection
  CARD_PUSH_STOPWORDS          Comma-separated stop-words to filter
  CARD_PUSH_SENSITIVE          Regex for sensitive doc IDs (never injected)
  CARD_PUSH_MAX_HITS           Max results to inject (default: 5)
  CARD_PUSH_TIMEOUT            Search timeout seconds (default: 4)
  CARD_PUSH_INJECT_FORMAT      Format template for injected context
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
LOG = HOME / "logs" / "card-push.jsonl"

# Defaults — no hardcoded tenant/collection references (neutral tool)
DEFAULT_TIMEOUT_S = 4.0
DEFAULT_MAX_HITS = 5
DEFAULT_SENSITIVE_RE = r"(?<![0-9a-z])b[-_ ]?(71|72|100|101|102)(?![0-9])"
DEFAULT_STOPWORDS = (
    "a an the and or but in on at to for of with by from is are was were be been being "
    "this that these those he she it we they you your their his her its our their "
    "and for with what which but oh do does did have has had not no nor so if then "
    "than too very can could will would should may might must shall "
    "aber alle allem allen aller alles auch auf aus bei bin bis bitte dann das "
    "dass dein deine dem den der des die dies diese mit nach nicht noch nun oder "
    "sehr sein sind soll sollen und uns unser von vor war was welche welcher "
    "welches wenn wer wie wir wird wo wollen wurde zum zur zu über antworte "
    "sage zeig zeige mach mache the and for with what which"
)
DEFAULT_INJECT_FORMAT = (
    "KNOWLEDGE INJECTION (auto, {timestamp}, search: '{query}')\n"
    "{body}\n"
    "{tail}"
)
DEFAULT_TAIL = (
    "Use these sources to inform your response. "
    "If none are relevant, proceed without them."
)

_seen = {}  # session_id -> last query hash


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _base_collections():
    """Return the list of base collections to search by default."""
    # Explicit env var takes priority
    raw = os.environ.get("CARD_PUSH_COLLECTIONS")
    if raw:
        return [c.strip() for c in raw.split(",") if c.strip()]
    # Fallback: read from file
    fpath = os.environ.get("CARD_PUSH_COLLECTIONS_FILE")
    if fpath:
        try:
            lines = Path(fpath).read_text(encoding="utf-8").splitlines()
            return [l.strip() for l in lines if l.strip()]
        except Exception:
            return []
    # No default — caller must configure explicitly
    return []


def _released_collection():
    """Read an additional collection slug from the release file.

    Returns the slug if present and valid, otherwise None (locked).
    A valid slug matches ^[a-z][a-z0-9-]+$ to prevent path injection.
    """
    path = os.environ.get("CARD_PUSH_RELEASE_FILE")
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not text:
        return None
    # Accept only a single clean slug on the first non-empty line
    m = re.match(r"^([a-z][a-z0-9-]+)$", text.splitlines()[0].strip())
    return m.group(1) if m else None


def _allowed_collections():
    """Base collections + optionally one released collection (tenant isolation)."""
    colls = list(_base_collections())
    rel = _released_collection()
    if rel and rel not in colls:
        colls.append(rel)
    return colls


def _qmd():
    exe = os.environ.get("CARD_PUSH_QMD")
    if exe:
        return exe
    return shutil.which("qmd") or "qmd"


def _stopwords():
    raw = os.environ.get("CARD_PUSH_STOPWORDS")
    if raw:
        return set(w.strip().lower() for w in raw.split(","))
    return set(DEFAULT_STOPWORDS.split())


def _keywords(text):
    words = re.findall(r"[0-9A-Za-zÄÖÜäöüß][0-9A-Za-zÄÖÜäöüß\-]{2,}", text)
    stop = _stopwords()
    out, seen = [], set()
    for w in words:
        lw = w.lower()
        if lw in stop or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
    return out[:8]


def _run_qmd(query, collections):
    timeout = _env_float("CARD_PUSH_TIMEOUT", DEFAULT_TIMEOUT_S)
    env = dict(os.environ)
    idx = os.environ.get("CARD_PUSH_INDEX")
    if idx:
        env["XDG_CACHE_HOME"] = idx
    cmd = [_qmd(), "search", query, "-n", "12", "--format", "json"]
    cmd += [a for c in collections for a in ("-c", c)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        raise
    return json.loads(out.decode("utf-8", "replace") or "[]")


def _snippet(s):
    lines = [l for l in (s or "").splitlines() if not l.startswith("@@")]
    t = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return t[:160]


def _sensitive_re():
    raw = os.environ.get("CARD_PUSH_SENSITIVE")
    if raw:
        try:
            return re.compile(raw, re.I)
        except re.error:
            pass
    return re.compile(DEFAULT_SENSITIVE_RE, re.I)


def _inject_format():
    fmt = os.environ.get("CARD_PUSH_INJECT_FORMAT")
    if fmt:
        return fmt
    return DEFAULT_INJECT_FORMAT


def _log(rec):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _hook(user_message=None, is_first_turn=False, session_id="", **kwargs):
    try:
        if not isinstance(user_message, str):
            return {"context": ""}
        msg = user_message.strip()
        if len(msg) < (8 if is_first_turn else 41):
            return {"context": ""}
        qhash = hashlib.sha1(msg.encode("utf-8")).hexdigest()[:10]
        if _seen.get(session_id) == qhash:
            return {"context": ""}
        _seen[session_id] = qhash

        kws = _keywords(msg)
        if not kws:
            return {"context": ""}

        colls = _allowed_collections()
        if not colls:
            # No collections configured — silently do nothing
            return {"context": ""}

        query = " ".join(kws)
        t0 = time.time()
        hits, err = [], None
        try:
            raw = _run_qmd(query, colls)
            for n in (3, 2):
                if raw or len(kws) <= n:
                    continue
                query = " ".join(sorted(kws, key=len, reverse=True)[:n])
                raw = _run_qmd(query, colls)
        except Exception as e:
            raw, err = [], type(e).__name__

        ms = int((time.time() - t0) * 1000)
        dropped = 0
        max_hits = _env_int("CARD_PUSH_MAX_HITS", DEFAULT_MAX_HITS)
        sens = _sensitive_re()

        for r in raw:
            # Only keep results from allowed collections
            if not any(str(r.get("file", "")).startswith(f"qmd://{c}/") for c in colls):
                dropped += 1
                continue
            blob = " ".join(str(r.get(k, "")) for k in ("file", "title", "snippet", "docid"))
            if sens.search(blob):
                dropped += 1
                continue
            hits.append(r)
            if len(hits) >= max_hits:
                break

        _log({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session": str(session_id)[:24],
            "query_hash": qhash,
            "query_words": len(kws),
            "collections": colls,
            "hits": [str(h.get("file", "")) for h in hits],
            "docids": [h.get("docid") for h in hits],
            "dropped_sensitive": dropped,
            "ms": ms,
            "error": err,
        })

        if err or not hits:
            return {"context": ""}

        fmt = _inject_format()
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        head = f"KNOWLEDGE INJECTION (auto, {ts}, search: '{query}')"
        body = "\n".join(
            f"- {str(h.get('file','')).replace('qmd://','')} | {h.get('title','')} | {_snippet(h.get('snippet'))}"
            for h in hits
        )
        tail = DEFAULT_TAIL
        return {"context": fmt.format(head=head, body=body, tail=tail, timestamp=ts, query=query)}
    except Exception:
        return {"context": ""}


def register(ctx):
    ctx.register_hook("pre_llm_call", _hook)
